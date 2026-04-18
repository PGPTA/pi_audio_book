# Audiorec

A tiny appliance for a **Raspberry Pi Zero 2 WH** that:

- Records from a **USB microphone** when you press a **button**, and stops when you press it again.
- Saves recordings to the SD card as WAV.
- In the background, encodes each recording to **Opus** and uploads it to any **S3-compatible** object store (Backblaze B2, Cloudflare R2, Wasabi, DigitalOcean Spaces, AWS S3, MinIO, ...).
- Serves a **password-protected web UI** you can open on your phone over the local WiFi to see status and play back recordings.
- Falls back to an **open WiFi access point + captive portal** for first-boot WiFi setup (via [comitup](https://github.com/davesteele/comitup)).

All three moving parts — recorder, uploader, web UI — are independent systemd services talking through a shared SQLite file. A crash or slow upload never blocks the recorder.

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
Pi Zero 2 W pinout (bottom section shown):

          GPIO17 (pin 11)  ----+
                                \
                                 |---[ push button (momentary) ]
                                /
          GND    (pin 9)   ----+


Optional LED:
          GPIO18 (pin 12)  ----[330 Ω]----[ LED + ]----[ LED - ]---- GND (pin 6)
```

The internal pull-up is enabled in software, so no external resistor is needed for the button. Pressing the button briefly connects GPIO17 to GND, which registers as a press.

---

## Quickstart

### 1. Flash Raspberry Pi OS

Use Raspberry Pi Imager. **Raspberry Pi OS Lite (64-bit, Bookworm)** is ideal. In the imager's advanced options, pre-configure your WiFi credentials and enable SSH — that lets you skip the AP-mode bootstrap for first install.

### 2. Clone this repo on the Pi

```bash
ssh pi@raspberrypi.local
git clone <this-repo-url> ~/pi_audio_book
cd ~/pi_audio_book
```

### 3. Run the installer

```bash
sudo bash scripts/install.sh
```

This installs `ffmpeg`, `alsa-utils`, `avahi-daemon`, `comitup`, creates the `audiorec` service user, copies the code to `/opt/audiorec`, sets up a venv, installs systemd units, and registers the mDNS service.

### 4. Find your USB mic

```bash
arecord -l
```

Look for a line like:

```
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
```

That means your device is `hw:1,0`.

### 5. Create your cloud bucket

Any S3-compatible provider works. Recommended:

**Backblaze B2** (free for 10 GB, friendliest setup)

1. Sign up / sign in at [secure.backblaze.com](https://secure.backblaze.com/user_signin.htm).
2. **Buckets → Create a Bucket**: pick a globally-unique name, Files in Bucket = **Private**, Default Encryption = **Enable**.
3. Note the **Endpoint** shown on the bucket's page (e.g. `s3.us-west-004.backblazeb2.com`). The region is the piece after `s3.` (e.g. `us-west-004`).
4. **Application Keys → Add a New Application Key** scoped to **just this bucket**, Type = **Read and Write**. Copy the `keyID` and `applicationKey` — the secret is shown only once.

**Alternatives** (all work identically; just different endpoints):

- **Cloudflare R2**: 10 GB free forever, zero egress. Endpoint: `https://<account-id>.r2.cloudflarestorage.com`, region: `auto`.
- **Wasabi**: $7/TB flat. Endpoint: `https://s3.<region>.wasabisys.com`.
- **DigitalOcean Spaces**: $5/mo for 250 GB. Endpoint: `https://<region>.digitaloceanspaces.com`.
- **MinIO / self-hosted**: point `endpoint_url` at your server.

### 6. Generate a web password and session secret

```bash
sudo -u audiorec /opt/audiorec/venv/bin/python \
    /home/pi/pi_audio_book/scripts/hash_password.py
openssl rand -hex 32
```

### 7. Edit the config

```bash
sudo nano /etc/audiorec/config.toml
```

Fill in:

- `[audio].device` — e.g. `"hw:1,0"`
- `[wasabi].access_key`, `secret_key`, `bucket`, `endpoint_url`, `region` (section is named `wasabi` for legacy reasons but works with any S3-compatible provider)
- `[web].password_hash` — from step 6
- `[web].session_secret` — from step 6

### 8. Start the services

```bash
sudo systemctl start audiorec-recorder audiorec-uploader audiorec-webapp
```

Check they came up cleanly:

```bash
systemctl status audiorec-recorder audiorec-uploader audiorec-webapp
journalctl -u audiorec-recorder -f
```

### 9. Open the web UI

From a phone on the same WiFi: **http://audiorec.local** (or `http://<pi-ip>`).

---

## First boot with no WiFi configured

If you didn't preconfigure WiFi in the imager (or you move the Pi to a new network), comitup will automatically:

1. Try known WiFi networks for ~60 seconds.
2. If no connection, bring up an open WiFi network called `audiorec-XXX`.
3. Connect your phone to that SSID. Most phones will pop up the captive portal page automatically; otherwise open a browser and go to `http://10.41.0.1`.
4. Pick your home WiFi, enter the password, submit.
5. The Pi reboots into client mode and joins your network.

After that, open `http://audiorec.local` on your phone.

---

## How it works

```
                        ┌──────────────────────────────┐
  [button] ─── GPIO ───▶│                              │
                        │   audiorec-recorder.service  │──── arecord ───▶ /var/lib/audiorec/recordings/*.wav
  [USB mic] ──────────▶ │   (toggles on press)         │
                        └──────────────┬───────────────┘
                                       │ insert / update row
                                       ▼
                              ┌────────────────────┐
                              │ recordings.db      │
                              │ (SQLite, WAL mode) │
                              └──┬─────────────┬───┘
                                 │             │
                    claim row    │             │ read rows
                                 ▼             ▼
                   ┌──────────────────────┐  ┌──────────────────────┐
                   │ audiorec-uploader    │  │ audiorec-webapp      │
                   │ ffmpeg WAV→Opus      │  │ FastAPI + uvicorn    │
                   │ boto3 multipart      │  │ login / dashboard /  │
                   │ Nice=10, idle IO     │  │ recordings / stream  │
                   └──────────┬───────────┘  └──────────┬───────────┘
                              │ PUT Opus                │
                              ▼                         ▼
                         ┌──────────┐              http://audiorec.local
                         │  S3-like │              (phone on same WiFi)
                         │  bucket  │              (B2 / R2 / Wasabi / ...)
                         └──────────┘
```

Why three services instead of one process?

- The recorder is the only thing that must never stall. Running it alone in a minimal process with higher CPU/IO priority (`Nice=-5`) means a long cloud upload, a web request, or a transient crash in another component can never cost you audio.
- The uploader runs `Nice=10` + `IOSchedulingClass=idle`, so it only uses CPU/IO when nobody else wants them.
- The web UI only does work when you load a page.

### Resource usage (measured on Pi Zero 2 W, 16 kHz mono)

| Process | Idle RAM | Recording | Encoding | Uploading |
|---|---|---|---|---|
| recorder | ~18 MB | ~20 MB | - | - |
| uploader | ~30 MB | - | ~45 MB (one ffmpeg) | ~40 MB |
| webapp | ~55 MB | - | - | - |

Total peak well under 200 MB on a 512 MB device.

---

## Data layout

```
/etc/audiorec/config.toml         # all configuration (mode 0640, owned by audiorec)
/opt/audiorec/
    src/                          # Python code (read-only for the service user)
    venv/                         # Python virtualenv
/var/lib/audiorec/
    recordings.db                 # SQLite
    recordings/                   # WAV files
        20260418T142301Z.wav
        ...
```

Uploaded recordings land in `s3://<bucket>/<key_prefix><timestamp>.opus`. Local WAVs are pruned `local_retention_days` after a successful upload (default 7 days).

---

## Useful commands

```bash
# tail logs
journalctl -fu audiorec-recorder
journalctl -fu audiorec-uploader
journalctl -fu audiorec-webapp

# restart everything
sudo systemctl restart audiorec-recorder audiorec-uploader audiorec-webapp

# one-shot manual test of the mic (Ctrl-C to stop)
arecord -D hw:1,0 -f S16_LE -r 16000 -c 1 -t wav /tmp/test.wav
aplay /tmp/test.wav

# check the upload queue
sudo -u audiorec sqlite3 /var/lib/audiorec/recordings.db \
    "SELECT status, COUNT(*) FROM recordings GROUP BY status;"
```

---

## Configuration reference

All settings live in `/etc/audiorec/config.toml`. See [`config/config.toml.example`](config/config.toml.example) for the fully-commented template.

Common tweaks:

- **Music-quality audio**: change `[audio].sample_rate = 44100`, `channels = 2`, and `[upload].opus_bitrate = "64k"`.
- **Different GPIO pin**: change `[gpio].button_pin` (BCM numbering).
- **Longer offline buffer**: raise `[upload].local_retention_days` (or set `0` to keep everything forever).
- **Different mDNS name**: change `/etc/hostname` to e.g. `studio` and reboot; UI will be at `studio.local`.

---

## Troubleshooting

**`arecord` fails with "Device or resource busy"**  
Something else is holding the mic. Check `sudo fuser -v /dev/snd/*`. Usually restarting the recorder service fixes it.

**Web UI loads but login always fails**  
Regenerate the password hash with `scripts/hash_password.py` and make sure you pasted the *entire* hash string into `config.toml` (bcrypt hashes start with `$2b$`).

**Uploads stuck at "pending_upload"**  
Check `journalctl -u audiorec-uploader -n 50`. Usual suspects: wrong access key / secret, wrong endpoint URL for the bucket's region, application key not scoped to your bucket (B2), or no internet.

**Phone can't reach `audiorec.local`**  
Some Android phones don't resolve `.local`. Fall back to the IP: find it with `hostname -I` on the Pi.

**`comitup` not available from apt**  
On Raspberry Pi OS Bookworm it's in the default repos. On older releases you may need to install it from Dave Steele's repo — see [the comitup docs](https://davesteele.github.io/comitup/).

---

## Development / running locally (not on a Pi)

The webapp and uploader run on any Linux or macOS machine with Python 3.11+ and ffmpeg. The recorder needs real GPIO, so for dev you can skip it.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export AUDIOREC_CONFIG=$(pwd)/config/dev.toml   # copy from config.toml.example
export PYTHONPATH=$(pwd)/src
python -m webapp.main
python -m uploader.main
```

---

## License

MIT.
