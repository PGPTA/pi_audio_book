# Audiorec

A tiny appliance for a **Raspberry Pi Zero 2 WH** that:

- Records from a **USB microphone** when you press a **button** and stops when you press it again.
- Saves recordings to the SD card as WAV.
- In the background, encodes each recording to **Opus** and uploads it to any **S3-compatible** object store (Backblaze B2, Cloudflare R2, Wasabi, DigitalOcean Spaces, AWS S3, MinIO, ...).
- Serves a **password-protected web UI** you can open on your phone over the local WiFi to see status, start/stop recording without the button, browse and play back recordings.
- Falls back to an **open WiFi access point + captive portal** for first-boot WiFi setup (via [comitup](https://github.com/davesteele/comitup)).

All three moving parts — recorder, uploader, web UI — are independent `systemd` services talking through a shared SQLite file, so a slow upload or a webapp crash never stops you recording.

---

## Quickstart (one command, everything else in the browser)

### 1. Flash Raspberry Pi OS

Use Raspberry Pi Imager. **Raspberry Pi OS Lite (64-bit, Bookworm)** is ideal. In the imager's advanced options pre-configure your WiFi + SSH so you can skip the AP-mode step on first install.

### 2. Clone + install on the Pi

```bash
ssh pi@raspberrypi.local
git clone https://github.com/PGPTA/pi_audio_book.git ~/pi_audio_book
cd ~/pi_audio_book
sudo bash scripts/setup.sh
```

That's it. No questions asked — the script installs apt packages, creates the service user, copies the code, builds a venv, writes a skeleton config, and starts all three services (recorder / uploader / webapp), each of which is enabled to auto-start on every boot.

When it finishes, open on your phone (same WiFi):

```
http://audiorec.local
# or http://<pi-ip> if .local doesn't resolve
```

You'll land on the **setup wizard**.

### 3. Finish setup in the browser

The wizard walks you through five small steps:

1. **Admin account** — pick a username + password; you're logged in automatically.
2. **Microphone** — the Pi scans for USB mics and lists them; tap one, hit **Test 2s capture** to confirm it's picking you up, then **Use this mic**.
3. **Cloud storage** — pick a provider (B2 / R2 / Wasabi / DO / custom S3), paste your keys + bucket, hit **Test connection** to verify, then **Save**.
4. **Hostname** *(optional)* — change `audiorec.local` to something else if you run more than one Pi.
5. **Finish** — flips the "setup complete" flag, restarts the recorder + uploader with the new config, and drops you on the dashboard.

### 4. That's it

Press the physical button or the big button on the dashboard to start/stop recording. Your next power cycle will bring everything back automatically — no re-entering credentials, no re-running the wizard.

---

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 WH | quad-core, WiFi, pre-soldered headers |
| USB microphone | any class-compliant USB mic |
| microSD card | 16 GB+ (Class 10 / A1 recommended) |
| Momentary push button (NO) | wired between **GPIO17** and **GND** |
| (optional) 3 mm LED + ~330 Ω resistor | status LED on **GPIO18** |
| 5 V / 2.5 A USB power supply | |

### Wiring

```
          GPIO17 (pin 11) ----[ push button (momentary) ]---- GND (pin 9)

Optional LED:
          GPIO18 (pin 12) ----[330 Ω]----[ LED + ]----[ LED - ]---- GND (pin 6)
```

The internal pull-up is enabled in software, so no external resistor is needed for the button. Pressing the button briefly connects GPIO17 to GND, which registers as a press.

---

## First-boot WiFi (no router? no problem)

If the Pi can't find a WiFi network on boot, `comitup` spins up its own open access point called `audiorec-XXXX`:

1. On your phone, join that SSID.
2. A captive portal pops up.
3. Pick your real WiFi, enter the password.
4. The Pi joins your WiFi and the AP disappears.

From that point on, open `http://audiorec.local` from the same network.

---

## Architecture

```
 +------------------+     +----------------+     +-----------+
 | recorder.service |---->| /var/lib/      |<----| uploader  |
 |  - arecord       |     |  audiorec/     |     |  - ffmpeg |
 |  - GPIO button   |     |   recordings/  |     |  - boto3  |
 |  - POSIX signals |     |   recordings.db|     |           |
 +------------------+     +----------------+     +-----------+
                                   ^
                                   |
                           +---------------+
                           | webapp.service|
                           |  - FastAPI    |
                           |  - setup      |
                           |  - dashboard  |
                           +---------------+
```

- **recorder** owns an `arecord` subprocess and writes a raw WAV. SIGUSR1 from the webapp acts exactly like a physical button press. If no mic is configured it idles politely until you finish the wizard.
- **uploader** polls SQLite for `pending_upload` rows, encodes WAV→Opus with `ffmpeg`, and streams to the bucket via `boto3` multipart upload (5 MB parts, `max_concurrency=1` to keep RAM low). Idles until cloud creds are set.
- **webapp** serves the setup wizard, dashboard, and recording list; talks to the recorder via a pidfile + signals; mints short-lived presigned URLs for cloud playback.

### How the webapp writes config without being root

The webapp runs as the unprivileged `audiorec` user. It writes `/etc/audiorec/config.toml` directly (the file is owned by `audiorec`), and uses `sudo` to call a tightly-scoped helper at `/opt/audiorec/bin/audiorec-setup-helper` for the three things that genuinely need root:

- `restart-recorder` / `restart-uploader` — so the new config takes effect
- `set-hostname <name>` — updates `/etc/hostname` + `/etc/hosts` and restarts avahi

That's the only sudo entry granted to `audiorec` (see `/etc/sudoers.d/audiorec`), and the helper validates every argument it receives.

---

## Reinstalling / upgrading / wiping

### Pull an update without losing settings

```bash
cd ~/pi_audio_book
git pull
sudo bash scripts/setup.sh
```

The script detects an existing `/etc/audiorec/config.toml` and leaves it alone, so you keep your admin password, mic choice, cloud creds, and hostname. Services are restarted with the new code.

### Start fresh but keep recordings

```bash
sudo bash scripts/uninstall.sh   # removes code, keeps /etc/audiorec and /var/lib/audiorec
sudo bash scripts/setup.sh       # reinstalls, reuses the kept config
```

### Nuke absolutely everything (credentials, recordings, service user)

```bash
sudo bash scripts/uninstall.sh --purge
```

---

## Useful commands

```bash
# tail each service's log
journalctl -fu audiorec-recorder
journalctl -fu audiorec-uploader
journalctl -fu audiorec-webapp

# restart manually (the webapp does this automatically when you save settings)
sudo systemctl restart audiorec-recorder
sudo systemctl restart audiorec-uploader

# peek at the config
sudo cat /etc/audiorec/config.toml

# disk + queue status
du -sh /var/lib/audiorec/recordings
sqlite3 /var/lib/audiorec/recordings.db 'select status, count(*) from recordings group by status;'
```

---

## Troubleshooting

### The setup page keeps asking me to finish — mic / cloud says "not set" even though I saved

Check `journalctl -u audiorec-webapp -n 50`. Two common causes:

- **The config file isn't writable.** It must be owned by user `audiorec` with mode `0640` and live in a directory the `audiorec` group can write (`/etc/audiorec` is installed as `root:audiorec 0770`).
- **sudo is blocked.** After a save the webapp runs `sudo /opt/audiorec/bin/audiorec-setup-helper restart-recorder`. If `/etc/sudoers.d/audiorec` wasn't installed, that call fails silently. Reinstall: `sudo bash scripts/setup.sh`.

### "No USB mic detected" in the wizard

```bash
arecord -l                # shows all capture cards; look for a non-bcm283x one
lsusb                     # confirms the mic is enumerated
sudo dmesg | tail -n 30   # shows USB connect/disconnect events
```

If `arecord -l` lists it but the wizard doesn't, make sure the card/device name doesn't contain "bcm" (that's filtered out as built-in Pi audio).

### Cloud test says "connection failed"

Usually endpoint URL. Quick sanity:

- **B2**: endpoint must match the region, e.g. `https://s3.us-west-004.backblazeb2.com`. The region is the middle chunk.
- **R2**: endpoint is `https://<account-id>.r2.cloudflarestorage.com`; region is always `auto`.
- **Wasabi**: `https://s3.<region>.wasabisys.com`.
- **DO Spaces**: `https://<region>.digitaloceanspaces.com`.

### Nothing uploads

`journalctl -u audiorec-uploader -n 50`. Most common: the bucket exists but the access key has no write permission — check the key's policy in your provider's console.

### Pi won't auto-start the services on boot

```bash
systemctl is-enabled audiorec-recorder audiorec-uploader audiorec-webapp
```

All three should say `enabled`. If not:

```bash
sudo systemctl enable --now audiorec-recorder audiorec-uploader audiorec-webapp
```

---

## Development notes

- Python 3.11+ is assumed (3.14 tested). The code path also works on 3.11/3.12 via `tomli`.
- Local dev loop:

  ```bash
  python3.11 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  AUDIOREC_CONFIG=$(pwd)/config/config.toml.example PYTHONPATH=src python -m webapp.main
  ```

  You won't have GPIO or `arecord` off the Pi, but the webapp + setup wizard can be poked at `http://127.0.0.1:80/setup` (change the port in the config first if 80 is taken).

- The config loader (`src/common/config.py`) is deliberately lenient: a missing file, a missing section, or empty fields all produce a valid `Config` object. The services check `is_audio_configured()` / `is_cloud_configured()` / `is_admin_set()` to decide whether to run or idle.

---

## License

MIT. See `LICENSE`.
