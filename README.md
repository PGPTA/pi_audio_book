# Audiorec

A tiny appliance for a **Raspberry Pi Zero 2 WH** that:

- Records from a **USB microphone** while a **switch** is held on (pressed/closed) and stops the moment you release it (open).
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

Flip the physical switch on (pressed/closed) to record, flip it off to stop. The big button on the dashboard still toggles between start/stop. Your next power cycle will bring everything back automatically — no re-entering credentials, no re-running the wizard.

---

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 WH | quad-core, WiFi, pre-soldered headers |
| USB microphone | any class-compliant USB mic |
| microSD card | 16 GB+ (Class 10 / A1 recommended) |
| Latching switch (or push-and-hold button) | wired between **GPIO5** and **GND** |
| (optional) 3 mm LED + ~330 Ω resistor | status LED on **GPIO18** |
| (optional) Waveshare 2.13" Touch e-Paper HAT | guest name entry — see [Touch e-paper HAT](#touch-e-paper-hat-guest-name-entry) below |
| 5 V / 2.5 A USB power supply | |

### Wiring

```
          GPIO5 (pin 29) -----[ switch / push-and-hold button ]---- GND (pin 30)

Optional LED:
          GPIO18 (pin 12) ----[330 Ω]----[ LED + ]----[ LED - ]---- GND (pin 6)
```

The internal pull-up is enabled in software, so no external resistor is needed. Closing the switch (or holding the button) connects GPIO5 to GND — recording runs for as long as the line is held low, and stops the moment it goes back high.

> **Upgrading from an older install?** The handset switch used to default to GPIO17. The Waveshare e-paper HAT claims GPIO17 as its RST line, so the default moved to GPIO5. If you've got a switch physically wired to GPIO17 and aren't using the HAT, edit `/etc/audiorec/config.toml` and set `gpio.button_pin = 17` — everything else stays the same.

---

## Touch e-paper HAT (guest name entry)

Optional add-on: a [Waveshare 2.13" Touch e-Paper HAT](https://www.waveshare.com/2.13inch-touch-e-paper-hat.htm) lets each guest tap their name on the screen before picking up the handset. Their recording is then saved as `<name>_<timestamp>.wav` (both locally and in the cloud) instead of the plain `<timestamp>.wav` default.

### Guest flow

1. Screen shows **"touch to begin"**.
2. Guest taps anywhere → on-screen QWERTY keyboard appears.
3. They type their name and hit **OK** → screen shows **"pick up the phone, \<name\>"**.
4. They pick up the handset — recording starts automatically, screen shows **"RECORDING \<name\>"** + live timer.
5. They hang up — screen shows **"saved. thanks, \<name\>"** for ~5 s, then returns to step 1.

If someone picks up the handset without typing anything the recording still happens, filed as `anonymous_<timestamp>.wav` — the display never blocks the mic.

### Wiring

The HAT is a 40-pin hat and plugs straight onto the Pi's header. It occupies these BCM pins: **10** (MOSI), **11** (SCLK), **8** (CE0), **25** (DC), **17** (RST), **24** (BUSY), **27** (INT), **22** (TRST), **2** (SDA), **3** (SCL).

Your handset switch can live happily on **GPIO5** and the LED on **GPIO18** — neither conflicts with the HAT. If you need to route those through the HAT, use stacking headers or the HAT's 12-pin ribbon instead of plugging the HAT straight on.

### Enabling it

1. Buy the board (the **V4 2023+** revision, matching sticker on the back).
2. Run the installer: `sudo bash scripts/setup.sh` — it installs the extra apt deps (`python3-pil`, `python3-spidev`, `python3-smbus`), enables SPI + I2C, and clones the Waveshare driver into `/opt/audiorec/vendor/`.
3. Flip the flag in `/etc/audiorec/config.toml`:
   ```toml
   [display]
   enabled = true
   panel = "2in13_V4"     # or 2in13_V2 / 2in13_V3 if you've got an older one
   rotate_deg = 90        # landscape; try 270 if the screen reads upside down
   ```
4. Restart the display service: `sudo systemctl restart audiorec-display`.
5. Tail the log to confirm the HAT came up cleanly: `journalctl -u audiorec-display -f`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| Service loops "EPD not available yet" | The vendored driver isn't on disk. Re-run `sudo bash scripts/setup.sh`; check `git` is installed and `https://github.com/waveshare/Touch_e-Paper_HAT` reachable. |
| Screen stays blank / is ghosted | SPI isn't enabled. Run `sudo raspi-config nonint do_spi 0 && sudo reboot`. |
| Touches aren't detected | I2C isn't enabled. `sudo raspi-config nonint do_i2c 0 && sudo reboot`. Also check `ls /dev/i2c-*` shows `/dev/i2c-1`. |
| Keys printed in the wrong places | Screen rotation is wrong. Try `rotate_deg = 270` (or 0/180) in `config.toml`. Touch coords are rotated to match automatically. |
| Panel shows colours / banding | Wrong panel revision. Check the sticker on the back of the HAT and set `panel` to `2in13_V2`, `2in13_V3`, or `2in13_V4`. |
| Recorder crashes after plugging HAT | Your old `button_pin = 17` conflicts with the HAT's RST. Set `button_pin = 5` in `/etc/audiorec/config.toml` and restart. |

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
 +------------------+     |   current_name |     +-----------+
         ^                +----------------+
         |                         ^   ^
         |                         |   |
         |                +--------+   +--------+
         |                |                     |
 +-----------------+     +---------------+     +------------------+
 | display.service |---->| webapp.service|     | (uploader above) |
 |  - e-paper UI   |     |  - FastAPI    |
 |  - GT1151 touch |     |  - setup      |
 |  - name writer  |     |  - dashboard  |
 +-----------------+     +---------------+
```

- **recorder** owns an `arecord` subprocess and writes a raw WAV. SIGUSR1 from the webapp acts exactly like a physical button press. At the start of each recording it reads (and falls back to "anonymous" for) `current_name.txt`, which is how the file ends up as `<name>_<timestamp>.wav`. If no mic is configured it idles politely until you finish the wizard.
- **uploader** polls SQLite for `pending_upload` rows, encodes WAV→Opus with `ffmpeg`, and streams to the bucket via `boto3` multipart upload (5 MB parts, `max_concurrency=1` to keep RAM low). Idles until cloud creds are set.
- **webapp** serves the setup wizard, dashboard, and recording list; talks to the recorder via a pidfile + signals; mints short-lived presigned URLs for cloud playback.
- **display** (optional, see [Touch e-paper HAT](#touch-e-paper-hat-guest-name-entry)) drives the 2.13" touch e-paper screen: renders the keyboard, writes the guest's name into `current_name.txt`, and watches the `recordings` table to step through its own `idle → keyboard → ready → recording → saved` states. Crashes here never stop the mic.

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

## Improving audio quality

The Pi Zero 2 W is loud — electrically. Its WiFi radio, SD card activity, USB bus, and PSU all radiate noise into any nearby unshielded wires, and a cheap USB mic right next to the board picks up most of it as "digital sound": high-pitched whine, clicks, hum, or hiss.

The biggest wins are on the **hardware** side. Start there. The software filter chain (below) cleans up what's left.

### Hardware checklist (in rough order of effect)

1. **Use a decent power supply.** The official Raspberry Pi 27 W or 15 W USB-C / micro-USB PSU gives you a clean 5 V rail. A dodgy phone charger is the #1 cause of a nasty whine that rises and falls with CPU load.
2. **Powered USB hub between the Pi and the mic.** This is the single most effective fix for "digital sound" — it decouples the mic's power from the Pi's noisy 5 V rail. A £5 hub works.
3. **Move the mic away from the Pi.** Even 20–30 cm helps a lot. Keep the mic cable away from the Pi's top-left corner (that's where the WiFi antenna sits). A longer shielded USB cable is worth it.
4. **Ferrite chokes (clamp-on toroids).** Snap one on the mic's USB cable near the Pi end, another on the Pi's power cable. £2 each. Especially effective against RF whine.
5. **Check grounding.** If your mic has a metal body and you can feel the Pi board through its case, you might have a ground loop. Insulating the Pi's case or using a powered hub (which breaks the loop) fixes it.
6. **If nothing else works, the mic itself is probably the problem.** Really cheap USB mics have poorly shielded ADCs. Clean/cheap options that are well-regarded: Fifine K669B, Fifine K053, Samson Go Mic, MXL AC-404. Avoid generic "mini USB mic on a stubby stalk" products — those are the worst offenders.

### Software: ffmpeg filter chain

Our encoder pipes the raw WAV through an ffmpeg `-af` filter chain before writing Opus. The default chain in `/etc/audiorec/config.toml`:

```toml
[upload]
audio_filters = "highpass=f=80,lowpass=f=8000,afftdn=nr=12"
```

- `highpass=f=80` cuts everything below 80 Hz — kills mains hum (50/60 Hz), HVAC rumble, and handling noise.
- `lowpass=f=8000` cuts everything above 8 kHz — kills the ultrasonic switching whine that bleeds out of cheap USB chargers. Human voice is ~85 Hz to ~8 kHz, so you lose nothing meaningful.
- `afftdn=nr=12` applies a 12 dB FFT-domain noise reduction — takes a baseline of the background hiss and subtracts it.

### Presets you can drop in

Edit `/etc/audiorec/config.toml`, set `audio_filters = "..."`, then `sudo systemctl restart audiorec-uploader`. The change applies to *new* recordings; old ones keep the filter they were encoded with.

**Minimal / purist** (your mic is already clean, you just want a high-pass):
```toml
audio_filters = "highpass=f=60"
```

**Default — good all-rounder for Pi Zero + USB mic:**
```toml
audio_filters = "highpass=f=80,lowpass=f=8000,afftdn=nr=12"
```

**Aggressive — Pi is in a noisy room / right next to a laptop fan:**
```toml
audio_filters = "highpass=f=100,lowpass=f=7000,afftdn=nr=20,anlmdn=s=7:p=0.002"
```

`afftdn=nr=20` doubles the noise reduction; `anlmdn` is a second-pass adaptive denoiser that's slower but very good at constant-character noise.

**Voice podcast loudness (broadcast-ready):**
```toml
audio_filters = "highpass=f=80,lowpass=f=10000,afftdn=nr=15,dynaudnorm=f=150:g=15,loudnorm=I=-16:TP=-1.5"
```

`dynaudnorm` evens out quiet/loud phrases; `loudnorm` normalizes to −16 LUFS (the podcast standard).

**Disable filtering entirely** (useful to A/B against the hardware fix):
```toml
audio_filters = ""
```

### Sample rate and bit depth

The default is 16 kHz / 16-bit mono — fine for speech and tiny on disk. If your mic sounds fuzzy even with filters, try the mic's native rate — most USB mics natively capture at 48 kHz and the Pi's ALSA layer has to resample 48k→16k, which can introduce artifacts on some devices:

```bash
# Find your mic's native rates.
arecord -D hw:1,0 --dump-hw-params 2>&1 | grep -E '^(RATE|FORMAT|CHANNELS)'
```

Then edit `/etc/audiorec/config.toml`:

```toml
[audio]
device = "hw:1,0"
sample_rate = 48000    # or 44100
channels = 1
format = "S16_LE"
```

ffmpeg will downsample cleanly when it encodes to Opus, so file size barely changes.

### A/B testing a change

Nothing beats listening. Record 10 seconds before and after each change and compare:

```bash
# Trigger a recording from the web UI or button, wait a beat, stop.
ls -lt /var/lib/audiorec/recordings/*.wav | head -2

# Play the latest WAV directly (raw, pre-filter):
aplay /var/lib/audiorec/recordings/20260418T123045Z.wav

# Play the uploaded Opus (post-filter) — either via the web UI's Cloud link,
# or locally if it hasn't been pruned yet. Temporarily re-encode on your Pi:
ffmpeg -i /var/lib/audiorec/recordings/20260418T123045Z.wav \
       -af "highpass=f=80,lowpass=f=8000,afftdn=nr=12" \
       -c:a libopus -b:a 32k /tmp/test.opus
ffplay /tmp/test.opus    # or copy to your laptop to play
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
