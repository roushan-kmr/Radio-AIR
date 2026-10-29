#!/usr/bin/env python3
"""Phone-friendly radio player for Raspberry Pi (Akashvani + your own channels).

On every start it:
  1. Re-downloads the Akashvani page (~/live.html) to get all current channels
     (falls back to the saved copy if the download fails).
  2. Reads your own channels from NonAIRchannels.txt (next to this script).
     If that file does not exist, a sample is created.
  3. Syncs favourites.json: each favourite keeps its stream URL, and the URL
     is updated automatically if a channel's address changed.
  4. Auto Play (button on the page, state kept in AutoPlay.txt): when ON, the
     last channel that was playing is started again after a reboot or restart.
  5. The page shows the current volume and has a power button that runs
     "sudo shutdown now" (after a confirmation).
  6. The page has an audio output selector listing the default ALSA output
     and every ALSA playback device (HDMI, USB DAC, ...), like ytm.py. Your
     choice and the volume are kept in AudioOutput.txt. Playback goes straight
     to ALSA through mpv, and the volume buttons use mpv's own volume.

Streams are played with mpv on the Pi (through its default audio output).

Run:   python3 radio.py
Open:  http://<PI_IP>:8000
"""
import html
import json
import os
import re
import socket
import subprocess
import threading
import time
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

BASE = Path(os.environ.get("RADIO_DIR") or Path(__file__).resolve().parent)
PAGE_FILE = Path.home() / "live.html"
SOURCE_URL = "https://akashvani.gov.in/radio/live.php"
FAV_FILE = BASE / "favorites.json"
CUSTOM_FILE = BASE / "NonAIRchannels.txt"
AUTOPLAY_FILE = BASE / "AutoPlay.txt"
AUDIO_FILE = BASE / "AudioOutput.txt"
MPV_SOCK = "/tmp/radio-mpv.sock"
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
AUTO = {"autoplay": False, "last": "", "playing": False}
# device is an ALSA id such as "alsa/default" or "alsa/plughw:CARD=DAC,DEV=0".
AUDIO = {"device": "alsa/default", "volume": 35}
AUDIO_LOCK = threading.Lock()
LOCK = threading.Lock()
FAV_LOCK = threading.Lock()
AUTO_LOCK = threading.Lock()
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


# ---------------------------------------------------------------- auto play
def save_auto():
    text = (
        "# Written by radio.py. You can edit it while radio.py is stopped.\n"
        f"autoplay={'on' if AUTO['autoplay'] else 'off'}\n"
        f"last={AUTO['last']}\n"
        f"playing={'yes' if AUTO['playing'] else 'no'}\n"
    )
    try:
        AUTOPLAY_FILE.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"Could not write {AUTOPLAY_FILE.name}: {e}")


def load_auto():
    if not AUTOPLAY_FILE.exists():
        save_auto()
        return
    values = {}
    for line in AUTOPLAY_FILE.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip().lower()] = v.strip()
    AUTO["autoplay"] = values.get("autoplay", "off").lower() in ("on", "yes", "true", "1")
    AUTO["last"] = values.get("last", "")
    AUTO["playing"] = values.get("playing", "no").lower() in ("yes", "on", "true", "1")


def remember(name=None, playing=None):
    """Record the last channel and whether it was playing (not stopped by you)."""
    with AUTO_LOCK:
        if name is not None:
            AUTO["last"] = name
        if playing is not None:
            AUTO["playing"] = playing
        save_auto()


def toggle_autoplay():
    with AUTO_LOCK:
        AUTO["autoplay"] = not AUTO["autoplay"]
        if AUTO["autoplay"] and CURRENT and PROC is not None and PROC.poll() is None:
            AUTO["last"] = CURRENT
            AUTO["playing"] = True
        save_auto()
        return AUTO["autoplay"]


def resume_last():
    """After a start or reboot, play the last channel if Auto Play is on."""
    with AUTO_LOCK:
        wanted = AUTO["autoplay"] and AUTO["playing"] and AUTO["last"]
        name = AUTO["last"]
    if not wanted:
        return
    ids = {s["name"]: s["id"] for s in STATIONS}
    if name not in ids:
        print(f"Auto play: last channel not in the list now: {name}")
        return
    # Right after boot the network or audio may not be ready, so retry a few times.
    for attempt in range(1, 7):
        print(f"Auto play: starting {name} (attempt {attempt})")
        if not play(ids[name]):
            return
        mine = PROC
        time.sleep(8)
        if PROC is not mine:  # you stopped it or picked another channel
            return
        if mine is not None and mine.poll() is None:
            return
        time.sleep(4)
    print("Auto play: gave up (stream or audio not available)")


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
        args = ["mpv", "--no-video", "--no-terminal", "--cache=yes",
                "--cache-secs=20"]
        try:
            os.remove(MPV_SOCK)
        except OSError:
            pass
        args += ["--ao=alsa", f"--audio-device={AUDIO['device']}",
                 f"--volume={AUDIO['volume']}", "--volume-max=100",
                 f"--input-ipc-server={MPV_SOCK}"]
        PROC = subprocess.Popen(
            args + [s["url"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        CURRENT = s["name"]
    remember(name=s["name"], playing=True)
    return True


def status():
    running = PROC is not None and PROC.poll() is None
    return {"playing": CURRENT if running else None,
            "autoplay": AUTO["autoplay"],
            "volume": get_volume(),
            "outputs": list_outputs()}


def mpv_ipc(command, timeout=2):
    """Send one command to the running mpv; returns its reply dict or None."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(MPV_SOCK)
        sock.sendall((json.dumps({"command": command}) + "\n").encode())
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
            for line in buf.split(b"\n"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "error" in d:
                    return d
    except OSError:
        return None
    finally:
        sock.close()


def is_playing():
    return PROC is not None and PROC.poll() is None


def change_volume(direction):
    with AUDIO_LOCK:
        v = AUDIO["volume"] + (5 if direction == "up" else -5)
        AUDIO["volume"] = max(0, min(100, v))
        save_audio()
    if is_playing():
        mpv_ipc(["set_property", "volume", AUDIO["volume"]])


def get_volume():
    """Current output volume as {"percent": 45, "muted": False}."""
    return {"percent": AUDIO["volume"], "muted": False}


# ------------------------------------------------------------ audio output
def load_audio():
    try:
        text = AUDIO_FILE.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "device":
                AUDIO["device"] = v or "alsa/default"
            elif k == "volume" and v.isdigit():
                AUDIO["volume"] = max(0, min(100, int(v)))


def save_audio():
    try:
        AUDIO_FILE.write_text(
            "# Written by radio.py.\n"
            f"device={AUDIO['device']}\n"
            f"volume={AUDIO['volume']}\n",
            encoding="utf-8",
        )
    except OSError as e:
        print(f"Could not write {AUDIO_FILE.name}: {e}")


def list_outputs():
    """Default ALSA output plus every ALSA playback device (from 'aplay -l')."""
    outs = [{"id": "alsa/default", "name": "Default ALSA Output"}]
    cards = []
    try:
        res = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=4)
        pat = re.compile(
            r"card\s+\d+:\s+(\S+)\s+\[([^\]]*)\],\s+device\s+(\d+):[^\[]*\[([^\]]*)\]"
        )
        for line in res.stdout.splitlines():
            m = pat.search(line)
            if m:
                cards.append(m.groups())  # (card id, card name, dev no, dev name)
    except Exception:
        pass
    per_card = {}
    for cid, _, _, _ in cards:
        per_card[cid] = per_card.get(cid, 0) + 1
    for cid, cname, dev, dname in cards:
        name = cname.strip()
        if per_card[cid] > 1:
            name += f" - {dname.strip()}"
        outs.append({"id": f"alsa/plughw:CARD={cid},DEV={dev}", "name": name})
    for o in outs:
        o["default"] = (o["id"] == AUDIO["device"])
    # keep a saved device selectable even if it is not plugged in right now
    if not any(o["default"] for o in outs):
        outs.append({"id": AUDIO["device"], "default": True,
                     "name": AUDIO["device"] + " (not found)"})
    return outs


def set_output(device):
    if device not in {o["id"] for o in list_outputs()}:
        return False
    with AUDIO_LOCK:
        AUDIO["device"] = device
        save_audio()
    print(f"Audio output set to: {device or 'system default'}")
    # restart the current channel so it uses the new output straight away
    if is_playing() and CURRENT:
        ids = {s["name"]: s["id"] for s in STATIONS}
        if CURRENT in ids:
            play(ids[CURRENT])
    return True


def shutdown_pi():
    """Run 'sudo shutdown now'. Returns (ok, error message)."""
    print("Shutdown requested from the web page.")
    try:
        r = subprocess.run(
            ["sudo", "-n", "shutdown", "now"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, r.stderr.strip() or "shutdown command failed"
    return True, ""


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
         color: #eee; padding-bottom: 250px; }
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
  .bar { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
  #now { flex: 1; min-width: 0; font-size: 14px; color: #bbb; min-height: 20px; }
  #vol { font-size: 14px; color: #bbb; white-space: nowrap; }
  .row { display: flex; gap: 8px; }
  button { flex: 1; padding: 14px; font-size: 16px; border: 0; border-radius: 10px;
           background: #333; color: #eee; }
  button.stop { background: #8b2b2b; }
  #refresh, #power { flex: 0 0 52px; padding: 0; font-size: 22px; }
  #power { background: #8b2b2b; }
  select { width: 100%; margin-bottom: 8px; padding: 12px; font-size: 15px;
           border-radius: 10px; border: 1px solid #444; background: #1e1e1e; color: #eee; }
  button.auto { width: 100%; margin-bottom: 8px; padding: 12px; }
  button.auto.on { background: #2e7d32; }
</style></head>
<body>
<header>
  <input id="q" placeholder="Search stations..." autocomplete="off">
  <button id="refresh" title="Reload channel list">&#8635;</button>
  <button id="power" title="Shut down the Pi">&#9211;</button>
</header>
<ul id="list"></ul>
<footer>
  <div class="bar">
    <div id="now">Nothing playing</div>
    <div id="vol">Vol --</div>
  </div>
  <select id="out" title="Audio output"><option>Audio output...</option></select>
  <button id="auto" class="auto">Auto Play: OFF</button>
  <div class="row">
    <button id="vdown">Vol -</button>
    <button id="stop" class="stop">Stop</button>
    <button id="vup">Vol +</button>
  </div>
</footer>
<script>
let stations = [], playing = null, pollTimer = null;
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
function setAuto(on) {
  const b = document.getElementById('auto');
  b.className = 'auto' + (on ? ' on' : '');
  b.textContent = 'Auto Play: ' + (on ? 'ON' : 'OFF');
}
async function loadStations() {
  stations = await (await fetch('/api/stations')).json();
}
function setVol(v) {
  document.getElementById('vol').textContent =
    v ? 'Vol ' + v.percent + '%' + (v.muted ? ' (muted)' : '') : 'Vol --';
}
function setOutputs(list) {
  const sel = document.getElementById('out');
  if (!list || !list.length || document.activeElement === sel) return;
  sel.innerHTML = '';
  list.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.id;
    opt.textContent = '🔊 ' + o.name;
    opt.selected = o.default;
    sel.appendChild(opt);
  });
}
async function refreshStatus() {
  try {
    const r = await (await fetch('/api/status')).json();
    playing = r.playing;
    setAuto(r.autoplay);
    setVol(r.volume);
    setOutputs(r.outputs);
    now.textContent = playing ? 'Playing: ' + playing : 'Nothing playing';
    render();
  } catch (e) {
    now.textContent = 'Pi not reachable';
  }
}
async function changeVol(dir) {
  const r = await (await fetch('/api/vol/' + dir, {method: 'POST'})).json();
  setVol(r.volume);
}
async function post(path) { await fetch(path, {method: 'POST'}); refreshStatus(); }
document.getElementById('stop').onclick = () => post('/api/stop');
document.getElementById('vup').onclick = () => changeVol('up');
document.getElementById('vdown').onclick = () => changeVol('down');
document.getElementById('power').onclick = async () => {
  if (!confirm('Shut down the Raspberry Pi now?')) return;
  now.textContent = 'Shutting down...';
  try {
    const r = await (await fetch('/api/shutdown', {method: 'POST'})).json();
    if (!r.ok) { now.textContent = 'Shutdown failed: ' + (r.error || 'unknown error'); return; }
  } catch (e) { /* connection drops as the Pi goes down */ }
  clearInterval(pollTimer);
  now.textContent = 'Pi is shutting down...';
};
document.getElementById('out').onchange = async (e) => {
  now.textContent = 'Switching audio output...';
  await fetch('/api/audio', {method: 'POST', body: JSON.stringify({device: e.target.value})});
  e.target.blur();
  refreshStatus();
};
document.getElementById('auto').onclick = async () => {
  const r = await (await fetch('/api/autoplay', {method: 'POST'})).json();
  setAuto(r.autoplay);
};
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
  pollTimer = setInterval(refreshStatus, 5000);
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
            remember(playing=False)  # stopped by you, so do not resume at boot
            self.send(200, json.dumps({"ok": True}))
        elif p == "/api/audio":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                dev = json.loads(self.rfile.read(n) or b"{}").get("device", "")
                ok = set_output(str(dev))
            except (ValueError, AttributeError):
                ok = False
            self.send(200 if ok else 400, json.dumps({"ok": ok}))
        elif p == "/api/autoplay":
            self.send(200, json.dumps({"ok": True, "autoplay": toggle_autoplay()}))
        elif p in ("/api/vol/up", "/api/vol/down"):
            change_volume(p.rsplit("/", 1)[1])
            self.send(200, json.dumps({"ok": True, "volume": get_volume()}))
        elif p == "/api/shutdown":
            ok, msg = shutdown_pi()
            self.send(200 if ok else 500, json.dumps({"ok": ok, "error": msg}))
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
    load_auto()
    load_audio()
    print(f"{len(STATIONS)} channels, {len(FAVS)} favourites, "
          f"auto play {'ON' if AUTO['autoplay'] else 'OFF'}. "
          f"Open http://<PI_IP>:{PORT}")
    threading.Thread(target=resume_last, daemon=True).start()
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        with LOCK:
            stop_player()
