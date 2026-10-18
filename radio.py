#!/usr/bin/env python3
"""Phone-friendly radio player for Raspberry Pi (Akashvani + your own channels).

On every start it:
  1. Re-downloads the Akashvani page (~/live.html) to get all current channels
     (falls back to the saved copy if the download fails).
  2. Reads your own channels from NonAIRchannels.txt (next to this script).
     If that file does not exist, a sample is created.
  3. Syncs favourites.json: each favourite keeps its stream URL, and the URL
     is updated automatically if a channel's address changed.

Streams are played with mpv on the Pi (through its default audio output).

Run:   python3 radio.py
Open:  http://<PI_IP>:8000
"""
import html
import json
import os
import re
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# When started from cron or in the background, the audio session variables
# are missing, so mpv and wpctl cannot find PipeWire. Set them here.
_UID = os.getuid()
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{_UID}")
os.environ.setdefault(
    "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{_UID}/bus"
)

BASE = Path(__file__).resolve().parent
PAGE_FILE = Path.home() / "live.html"
SOURCE_URL = "https://akashvani.gov.in/radio/live.php"
FAV_FILE = BASE / "favorites.json"
CUSTOM_FILE = BASE / "NonAIRchannels.txt"
PORT = 8000
DEAD_HOSTS = ("air.pc.cdn.bitgravity.com",)  # these returned 404
PATTERN = re.compile(
    r"name:\s*'([^']*)',\s*image:\s*'[^']*',\s*live_url:\s*'([^']*)'"
)
CUSTOM_LINE = re.compile(r'^"([^"]+)"\s+(\S+)\s*$')

SAMPLE = '''# Non-AIR radio channels
# One channel per line:  "channel name" stream-url
# Lines starting with # are ignored. Remove the # to enable a line.
# The URL must start with http:// or https://
# After editing, restart radio.py or tap the refresh button on the page.
#
# "My Jazz Station" https://example.com/jazz/stream.m3u8
# "Morning Talk" https://example.com/talk.mp3
'''

STATIONS = []
FAVS = {}  # channel name -> stream URL
LOCK = threading.Lock()
FAV_LOCK = threading.Lock()
PROC = None
CURRENT = None


# ---------------------------------------------------------------- stations
def download_page():
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    if not PATTERN.search(raw.decode("utf-8", errors="ignore")):
        raise ValueError("downloaded page has no station list")
    PAGE_FILE.write_bytes(raw)


def read_air_stations(refresh):
    if refresh:
        try:
            download_page()
            print("Akashvani station page updated.")
        except Exception as e:
            print(f"Could not update the station page ({e}); using saved copy.")
    if not PAGE_FILE.exists():
        raise RuntimeError("No station page available (download failed, no saved copy).")
    text = PAGE_FILE.read_text(encoding="utf-8", errors="ignore")
    seen, out = set(), []
    for name, url in PATTERN.findall(text):
        name = html.unescape(name).strip()
        if not name or not url or name in seen:
            continue
        if any(h in url for h in DEAD_HOSTS):
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        seen.add(name)
        out.append({"name": name, "url": url})
    return out


def read_custom_stations():
    if not CUSTOM_FILE.exists():
        try:
            CUSTOM_FILE.write_text(SAMPLE, encoding="utf-8")
            print(f"Created sample {CUSTOM_FILE.name}; add your channels to it.")
        except OSError as e:
            print(f"Could not create {CUSTOM_FILE.name}: {e}")
        return []
    out = []
    text = CUSTOM_FILE.read_text(encoding="utf-8-sig", errors="ignore")
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip().replace("\u201c", '"').replace("\u201d", '"')
        if not line or line.startswith("#"):
            continue
        m = CUSTOM_LINE.match(line)
        if m:
            name, url = m.groups()
        else:  # also accept: channel name url  (no quotes)
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                print(f"{CUSTOM_FILE.name} line {n}: skipped (need name and URL)")
                continue
            name, url = parts[0].strip().strip('"'), parts[1]
        if not url.lower().startswith(("http://", "https://")):
            print(f"{CUSTOM_FILE.name} line {n}: skipped (URL must start with http)")
            continue
        out.append({"name": name.strip(), "url": url})
    return out


def load_stations(refresh=True):
    global STATIONS
    air = read_air_stations(refresh)
    names = {s["name"] for s in air}
    custom = []
    for s in read_custom_stations():
        name = s["name"]
        if name in names:
            name += " (custom)"
        names.add(name)
        custom.append({"name": name, "url": s["url"]})
    stations = sorted(air + custom, key=lambda s: s["name"].lower())
    for i, s in enumerate(stations):
        s["id"] = i
    STATIONS = stations
    print(f"Loaded {len(air)} Akashvani + {len(custom)} custom channels.")


# --------------------------------------------------------------- favourites
def load_favs():
    global FAVS
    try:
        data = json.loads(FAV_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if isinstance(data, list):  # older format: just names
        data = {n: "" for n in data}
    FAVS = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_favs():
    FAV_FILE.write_text(
        json.dumps(FAVS, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def sync_favs():
    """Keep each favourite's saved URL in step with the current station list."""
    by_name = {s["name"]: s for s in STATIONS}
    changed = False
    with FAV_LOCK:
        for name, old_url in list(FAVS.items()):
            s = by_name.get(name)
            if s is None:
                print(f"Favourite not in the channel list now (kept): {name}")
                continue
            if old_url != s["url"]:
                if old_url:
                    print(f"Favourite URL updated: {name}\n  old: {old_url}\n  new: {s['url']}")
                FAVS[name] = s["url"]
                changed = True
        if changed:
            save_favs()


def toggle_fav(station_id):
    """Flip favourite state; returns the new state, or None for a bad id."""
    if not 0 <= station_id < len(STATIONS):
        return None
    s = STATIONS[station_id]
    with FAV_LOCK:
        if s["name"] in FAVS:
            del FAVS[s["name"]]
        else:
            FAVS[s["name"]] = s["url"]
        save_favs()
        return s["name"] in FAVS


# ------------------------------------------------------------------ player
def stop_player():
    global PROC, CURRENT
    if PROC and PROC.poll() is None:
        PROC.terminate()
        try:
            PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            PROC.kill()
    PROC = None
    CURRENT = None


def play(station_id):
    global PROC, CURRENT
    if not 0 <= station_id < len(STATIONS):
        return False
    s = STATIONS[station_id]
    with LOCK:
        stop_player()
        PROC = subprocess.Popen(
            ["mpv", "--no-video", "--no-terminal", "--cache=yes",
             "--cache-secs=20", s["url"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        CURRENT = s["name"]
    return True


def status():
    running = PROC is not None and PROC.poll() is None
    return {"playing": CURRENT if running else None}


def change_volume(direction):
    step = "5%+" if direction == "up" else "5%-"
    subprocess.run(
        ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", step],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --------------------------------------------------------------------- web
PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Radio</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #121212;
         color: #eee; padding-bottom: 130px; }
  header { position: sticky; top: 0; background: #121212; padding: 12px; z-index: 2;
           display: flex; gap: 8px; }
  input { flex: 1; min-width: 0; padding: 12px; font-size: 16px; border-radius: 10px;
          border: 1px solid #444; background: #1e1e1e; color: #eee; }
  ul { list-style: none; margin: 0; padding: 0 12px; }
  li { display: flex; align-items: center; padding: 14px 12px; margin: 6px 0;
       background: #1e1e1e; border-radius: 10px; font-size: 16px; cursor: pointer; }
  li.active { background: #2e5d34; }
  li.sec { background: none; cursor: default; padding: 10px 4px 2px; font-size: 13px;
           color: #999; text-transform: uppercase; letter-spacing: 1px; }
  .name { flex: 1; }
  .star { font-size: 26px; padding: 0 8px; color: #777; }
  .star.on { color: #f5c542; }
  footer { position: fixed; bottom: 0; left: 0; right: 0; background: #1b1b1b;
           padding: 12px; border-top: 1px solid #333; }
  #now { margin-bottom: 8px; font-size: 14px; color: #bbb; min-height: 20px; }
  .row { display: flex; gap: 8px; }
  button { flex: 1; padding: 14px; font-size: 16px; border: 0; border-radius: 10px;
           background: #333; color: #eee; }
  button.stop { background: #8b2b2b; }
  #refresh { flex: 0 0 52px; padding: 0; font-size: 22px; }
</style></head>
<body>
<header>
  <input id="q" placeholder="Search stations..." autocomplete="off">
  <button id="refresh" title="Reload channel list">&#8635;</button>
</header>
<ul id="list"></ul>
<footer>
  <div id="now">Nothing playing</div>
  <div class="row">
    <button id="vdown">Vol -</button>
    <button id="stop" class="stop">Stop</button>
    <button id="vup">Vol +</button>
  </div>
</footer>
<script>
let stations = [], playing = null;
const list = document.getElementById('list');
const now = document.getElementById('now');

function heading(text) {
  const li = document.createElement('li');
  li.className = 'sec';
  li.textContent = text;
  return li;
}
function row(s) {
  const li = document.createElement('li');
  if (s.name === playing) li.className = 'active';
  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = s.name;
  const star = document.createElement('span');
  star.className = 'star' + (s.fav ? ' on' : '');
  star.textContent = s.fav ? '★' : '☆';
  star.onclick = async (e) => {
    e.stopPropagation();
    const r = await (await fetch('/api/fav/' + s.id, {method: 'POST'})).json();
    s.fav = r.fav;
    render();
  };
  li.onclick = async () => {
    now.textContent = 'Starting ' + s.name + '...';
    await fetch('/api/play/' + s.id, {method: 'POST'});
    refreshStatus();
  };
  li.append(name, star);
  return li;
}
function render() {
  const q = document.getElementById('q').value.toLowerCase();
  list.innerHTML = '';
  const shown = stations.filter(s => s.name.toLowerCase().includes(q));
  const favs = shown.filter(s => s.fav);
  const rest = shown.filter(s => !s.fav);
  if (favs.length) {
    list.appendChild(heading('Favourites'));
    favs.forEach(s => list.appendChild(row(s)));
  }
  if (rest.length) {
    if (favs.length) list.appendChild(heading('All stations'));
    rest.forEach(s => list.appendChild(row(s)));
  }
}
async function loadStations() {
  stations = await (await fetch('/api/stations')).json();
}
async function refreshStatus() {
  const r = await (await fetch('/api/status')).json();
  playing = r.playing;
  now.textContent = playing ? 'Playing: ' + playing : 'Nothing playing';
  render();
}
async function post(path) { await fetch(path, {method: 'POST'}); refreshStatus(); }
document.getElementById('stop').onclick = () => post('/api/stop');
document.getElementById('vup').onclick = () => post('/api/vol/up');
document.getElementById('vdown').onclick = () => post('/api/vol/down');
document.getElementById('q').oninput = render;
document.getElementById('refresh').onclick = async () => {
  now.textContent = 'Reloading channel list...';
  await fetch('/api/refresh', {method: 'POST'});
  await loadStations();
  refreshStatus();
};

(async () => {
  await loadStations();
  refreshStatus();
  setInterval(refreshStatus, 5000);
})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.send(200, PAGE, "text/html")
        elif self.path == "/api/stations":
            self.send(200, json.dumps(
                [{"id": s["id"], "name": s["name"], "fav": s["name"] in FAVS}
                 for s in STATIONS]))
        elif self.path == "/api/status":
            self.send(200, json.dumps(status()))
        else:
            self.send(404, "{}")

    def do_POST(self):
        p = self.path
        if p.startswith("/api/play/"):
            try:
                ok = play(int(p.rsplit("/", 1)[1]))
            except ValueError:
                ok = False
            self.send(200 if ok else 400, json.dumps({"ok": ok}))
        elif p.startswith("/api/fav/"):
            try:
                state = toggle_fav(int(p.rsplit("/", 1)[1]))
            except ValueError:
                state = None
            self.send(200 if state is not None else 400,
                      json.dumps({"ok": state is not None, "fav": bool(state)}))
        elif p == "/api/stop":
            with LOCK:
                stop_player()
            self.send(200, json.dumps({"ok": True}))
        elif p in ("/api/vol/up", "/api/vol/down"):
            change_volume(p.rsplit("/", 1)[1])
            self.send(200, json.dumps({"ok": True}))
        elif p == "/api/refresh":
            try:
                load_stations(refresh=True)
                sync_favs()
                self.send(200, json.dumps({"ok": True, "count": len(STATIONS)}))
            except Exception as e:
                self.send(500, json.dumps({"ok": False, "error": str(e)}))
        else:
            self.send(404, "{}")


if __name__ == "__main__":
    load_stations(refresh=True)
    load_favs()
    sync_favs()
    print(f"{len(STATIONS)} channels, {len(FAVS)} favourites. "
          f"Open http://<PI_IP>:{PORT}")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        with LOCK:
            stop_player()
