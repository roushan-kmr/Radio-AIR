# Complete Deployment Guide for Pi Radio (`radio-pi`)

This guide covers setting up your Dockerized radio streaming application on a fresh system from scratch, ensuring persistent storage, proper permissions, and hardware audio pass-through.

---

## 1. Prerequisites & Host System Setup

Before deploying the container, prepare your fresh machine by installing Docker and adding your user account to the Docker group so you can manage containers without needing `sudo`.

### Step 1: Install Docker & Docker Compose
```bash
# Update system package lists
sudo apt update && sudo apt upgrade -y

# Install Docker using the official convenience script
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Verify Docker installation
docker --version
docker compose version
```

### Step 2: Configure Non-Root User Access
Add your current user (e.g., `pi`) to the `docker` group so you don't face `permission denied while trying to connect to the docker API` errors:

```bash
# Add user to docker group
sudo usermod -aG docker $USER

# Apply group membership to current session immediately
newgrp docker
```

---

## 2. Directory Layout & Configuration

Create your application working directory on the host machine:

```bash
mkdir -p ~/Radio
cd ~/Radio
```

### Updated `docker-compose.yml`

Create or open `docker-compose.yml`:
```bash
nano docker-compose.yml
```

Paste the following updated, self-contained setup:

```yaml
services:
  # 1. Helper container: Runs first as root to ensure ./data exists
  #    and is pre-owned by UID/GID 1000:1000 before the main app starts.
  init-permissions:
    image: alpine:latest
    container_name: radio-init-permissions
    command: sh -c "mkdir -p /data && chown -R 1000:1000 /data && chmod 755 /data"
    volumes:
      - ./data:/data

  # 2. Main Application Container
  radio:
    image: ghcr.io/roushan-kmr/radio-pi:v2
    container_name: radio
    init: true
    user: "1000:1000"
    depends_on:
      init-permissions:
        condition: service_completed_successfully
    network_mode: host
    environment:
      - HOME=/data
      - RADIO_DIR=/data
    devices:
      - /dev/snd:/dev/snd
    group_add:
      - audio
    volumes:
      - ./data:/data
    restart: unless-stopped
```

> **Key Deployment Features in this YAML:**
> * **`init-permissions`**: Automates host `./data` directory creation and permission ownership (`1000:1000`) on a fresh system without manual `mkdir` or `chmod` commands.
> * **`network_mode: host`**: Uses the Raspberry Pi host network directly for reliable stream resolution and low latency on port `8000`.
> * **`/dev/snd` & `group_add: audio`**: Grants the containerized `mpv` direct access to physical sound devices (HDMI, 3.5mm jack, USB DAC).

---

## 3. Starting the Application

Pull the latest image and launch the deployment stack in background/detached mode:

```bash
# Pull images from registry
docker compose pull

# Launch containers in the background
docker compose up -d
```

### Verify Deployment Status
```bash
docker ps
```
You should see the `radio` container running with status `Up`. The `radio-init-permissions` container will show as `Exited (0)`, which is expected.

---

## 4. First-Time Audio Setup

1. Open your browser and navigate to:
   ```text
   http://<YOUR_PI_IP>:8000
   ```
2. **Select Audio Output Device:** On initial launch, click the **Audio Output** dropdown menu at the bottom of the interface and select your active hardware sound card (e.g., *Default ALSA Output*, *HDMI*, or *USB Sound Card*).
3. **Play Stream:** Click on any station from the list. The status bar will show *"Playing: <Station Name>"* and start live audio playback.

---

## 5. Troubleshooting Guide

### Issue A: Docker Permission Error (`docker.sock`)
* **Symptom:** `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`
* **Fix:** Ensure your user belongs to the `docker` group and re-login or reload groups:
  ```bash
  sudo usermod -aG docker $USER
  newgrp docker
  ```

---

### Issue B: Web Page Not Loading
* **Symptom:** Browser cannot connect to `http://<PI_IP>:8000`.
* **Fix 1:** Verify the container is running:
  ```bash
  docker ps
  ```
* **Fix 2:** Test local web server response directly on the Pi:
  ```bash
  curl -I http://localhost:8000
  ```
* **Fix 3:** Check if host firewall (`ufw` or `iptables`) is blocking port `8000`:
  ```bash
  sudo ufw allow 8000/tcp
  ```

---

### Issue C: UI Shows "Playing" But There Is No Sound
* **Symptom:** Web interface indicates a channel is playing, but audio is silent or cuts out after a few seconds.
* **Fix 1:** **Select Audio Sink:** Open `http://<PI_IP>:8000`, expand the audio output selector, and manually pick an explicit ALSA sound card instead of relying on default routing.
* **Fix 2:** **Check Sound Hardware Pass-through:** Verify device permissions inside the container:
  ```bash
  docker compose exec radio ls -l /dev/snd
  ```
* **Fix 3:** **Test MPV Directly Inside Container:**
  ```bash
  docker compose exec radio mpv --no-video http://stream.zeno.fm/f3wvbbqmdg8uv
  ```

---

### Issue D: Streams Fail with `Name or service not known`
* **Symptom:** Container logs show `Failed to resolve hostname`.
* **Fix:** Since the updated configuration uses `network_mode: host`, ensure your Raspberry Pi host itself has active network connectivity and DNS resolution:
  ```bash
  ping -c 3 google.com
  ```

---

## 6. Useful Management Commands

* **View Real-Time Logs:**
  ```bash
  docker compose logs -f radio
  ```
* **Restart the Radio Stack:**
  ```bash
  docker compose restart radio
  ```
* **Stop and Cleanup Containers:**
  ```bash
  docker compose down
  ```