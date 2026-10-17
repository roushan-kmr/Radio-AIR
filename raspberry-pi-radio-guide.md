# Akashvani Radio on Raspberry Pi (Phone-Controlled, No Browser on the Pi)

A tested setup for a Raspberry Pi 5 (Raspberry Pi OS, Debian 13). It plays Akashvani (All India Radio) live streams on the Pi's audio output (e.g. a USB DAC) and gives you a phone-friendly page to pick stations, mark favourites and change volume.

**Why this instead of a browser on the Pi:** a browser inside a virtual VNC screen made the radio stall, because the screen encoder (Xtigervnc) maxed out one CPU core. Playing the stream directly with `mpv` uses almost no CPU, so it does not stall.

**How it works:**
1. The Akashvani website holds its station list as a JavaScript list inside the page. A script reads it from a saved copy of the page (`live.html`).
2. `radio.py` serves a small web page on port 8000 and starts `mpv --no-video` for the station you tap.
3. Your phone is only a remote control. The sound comes from the Pi.

---

## Files

All in `~/Radio` (except `live.html`, which is in your home folder):

| File | Purpose |
|---|---|
| `radio.py` | The phone page and player (the main program) |
| `find_urls.py` (or your renamed copy) | Looks up the current stream URLs of your favourite channels, checks they respond, and flags changes |
| `~/live.html` | Saved copy of the Akashvani page, the source of the station list |
| `favorites.json` | Your starred stations (created automatically) |
| `radio_urls.json` | Last saved URLs from the checker script |
| `radio.log` | Output and errors when running in the background |

---

## 1. Install mpv

```bash
sudo apt update
sudo apt install mpv -y
```

## 2. Download the station page

```bash
cd ~
curl -sL -A "Mozilla/5.0" https://akashvani.gov.in/radio/live.php -o live.html
```

Check that stream addresses are in it:

```bash
grep -c m3u8 live.html
```

A number in the hundreds is normal.

## 3. Test one stream

```bash
mpv --no-video "https://radio.wavespb.com/live/146ed6ec6dea5a24/146ed6ec6dea5a24.m3u8"
```

That is Vividh Bharati. Press `q` to stop. Harmless startup lines such as `Invalid HLS ID3 audio timestamp` can be ignored.

## 4. Make the DAC the default output

Sound goes to the Pi's default audio output. Check and set it:

```bash
wpctl status            # find your DAC under Sinks
wpctl set-default <ID>
```

## 5. Put the scripts in place

Create the folder and copy the files into it:

```bash
mkdir -p ~/Radio
cd ~/Radio
```

Place `radio.py` and the checker script here (copy from your PC with `scp` or Samba, or paste with `nano radio.py`, then Ctrl+O, Enter, Ctrl+X).

## 6. Check your channel URLs (optional but useful)

```bash
python3 ~/Radio/find_urls.py
```

It prints each channel's current URL, whether the playlist responds, and `CHANGED` if a URL differs from the last run. Channels to track are listed in the `CHANNELS` list at the top of the script (names must match the website's names exactly).

## 7. Run the radio page

```bash
cd ~/Radio
python3 radio.py
```

You should see `Loaded ... stations, ... favourites`. On your phone (same Wi-Fi), open:

```
http://<PI_IP>:8000
```

Find the Pi's IP with `hostname -I`.

- Type in the search box to filter stations.
- Tap a station name to play it.
- Tap the star to add or remove a favourite. Favourites appear first.
- **Stop**, **Vol -** and **Vol +** are at the bottom.

Press `Ctrl+C` in the terminal to stop the server.

## 8. Keep it running in the background

```bash
cd ~/Radio
nohup python3 radio.py > radio.log 2>&1 &
```

What this does: `nohup` keeps it running after you close the terminal, `> radio.log 2>&1` saves output and errors to a file, and `&` runs it in the background.

Useful commands:

```bash
pgrep -af radio.py     # is it running?
tail -f radio.log      # watch the log (Ctrl+C to leave)
pkill -f radio.py      # stop it
```

## 9. Start automatically at boot (optional)

```bash
(crontab -l 2>/dev/null; echo "@reboot sleep 20 && python3 /home/pi/Radio/radio.py > /home/pi/Radio/radio.log 2>&1") | crontab -
```

`radio.py` sets the audio session variables itself, so mpv and the volume control can find PipeWire when started from cron. In testing this worked without extra steps. If sound or volume control fail only after a reboot, run this once so your user's audio services start at boot:

```bash
sudo loginctl enable-linger pi
```

---

## Optional: a terminal shortcut

Play a saved channel by name from the command line. This reads the URLs saved by the checker script:

```bash
cat >> ~/.bashrc <<'EOF'
radio() {
  url=$(python3 -c "import json,os,sys;print(json.load(open(os.path.expanduser('~/Radio/radio_urls.json')))[sys.argv[1]])" "$1") || return
  mpv --no-video "$url"
}
EOF
source ~/.bashrc
```

Use it like this: `radio "Vividh Bharati"`

---

## Maintenance: when stations stop playing

Stream addresses can change. To refresh:

1. Update the saved page and station list:
   ```bash
   curl -sL -A "Mozilla/5.0" https://akashvani.gov.in/radio/live.php -o ~/live.html
   curl -X POST http://localhost:8000/api/refresh
   ```
2. Run `python3 ~/Radio/find_urls.py` to see which channels changed or stopped responding.
3. If the checker says no stations were found, the website layout has changed and the pattern in the scripts needs updating.

To check the checker automatically every Monday at 9 am and keep a log:

```bash
(crontab -l 2>/dev/null; echo "0 9 * * 1 python3 /home/pi/Radio/find_urls.py >> /home/pi/Radio/find_urls.log 2>&1") | crontab -
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Page does not open on the phone | Check the phone is on the same Wi-Fi, the IP is correct, and `pgrep -af radio.py` shows it running |
| Station list is empty | `~/live.html` is missing or the site layout changed; re-download it and restart |
| Tapping a station does nothing / no sound | Test the URL with `mpv --no-video "<url>"`; check the default sink with `wpctl status` |
| One station stops or errors | Its stream URL may have changed or the station is down; run the checker script |
| Volume buttons do nothing | `wpctl status` should list a default sink; use the newest `radio.py`, which sets the audio environment |
| `Could not connect to PipeWire` in the log | You are on an old `radio.py`; replace it with the latest version |
| Favourites missing after a restart | They are saved in `favorites.json` next to `radio.py`; run the script from the same folder or check that file exists |
| Port already in use | An old copy is running: `pkill -f radio.py`, then start again |

## Notes

- The page has no password. Anyone on your network who knows the address can change stations and volume, so keep it on your home network and do not expose port 8000 to the internet. Use a VPN such as Tailscale or WireGuard for remote use.
- The streams come from the public Akashvani website. Addresses may change without notice, which is why the checker script exists.
