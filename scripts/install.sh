#!/usr/bin/env bash
# Audiorec installer for Raspberry Pi OS (Bookworm/Bullseye).
#
#   sudo bash scripts/install.sh
#
# This is fully non-interactive. On a fresh Pi it:
#   - installs apt deps (python, ffmpeg, alsa, avahi, comitup, sudo)
#   - creates the 'audiorec' service user
#   - copies source to /opt/audiorec and builds a venv
#   - drops a minimal /etc/audiorec/config.toml if one doesn't exist
#   - installs the setup helper + sudoers entry
#   - installs systemd units, enables them, and starts them
#
# On a reinstall it preserves your existing config (so you don't re-enter
# your mic, cloud creds, and admin password). Run scripts/uninstall.sh
# --purge first if you really want to wipe everything.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root: sudo bash scripts/install.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/audiorec"
CONFIG_DIR="/etc/audiorec"
CONFIG_FILE="$CONFIG_DIR/config.toml"
DATA_DIR="/var/lib/audiorec"
SERVICE_USER="audiorec"
HELPER_BIN="$INSTALL_DIR/bin/audiorec-setup-helper"

c_info() { printf '\033[36m>>> %s\033[0m\n' "$*"; }
c_ok()   { printf '\033[32m[OK] %s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m!!! %s\033[0m\n' "$*"; }

c_info "apt: installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    build-essential swig \
    liblgpio-dev \
    alsa-utils ffmpeg \
    avahi-daemon \
    ca-certificates \
    git \
    sudo \
    fonts-dejavu-core \
    python3-pil python3-numpy python3-spidev python3-smbus

# Comitup lives in Debian contrib; some images have it, some don't. Try hard
# but don't fail the install if it's genuinely unavailable -- the rest of the
# system still works, you just won't have the WiFi-AP fallback.
if ! dpkg -s comitup >/dev/null 2>&1; then
    if ! apt-get install -y --no-install-recommends comitup; then
        c_warn "comitup not available from apt; see README for manual install."
    fi
fi

c_info "user: ensuring '$SERVICE_USER' exists"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin \
        --groups audio,gpio "$SERVICE_USER"
else
    usermod -a -G audio,gpio "$SERVICE_USER"
fi

c_info "dirs: $INSTALL_DIR, $CONFIG_DIR, $DATA_DIR"
install -d -m 0755 -o root -g root "$INSTALL_DIR"
install -d -m 0755 -o root -g root "$INSTALL_DIR/bin"
# The webapp (running as 'audiorec') needs to *rewrite* config.toml during the
# setup wizard, so the directory is group-writable by audiorec and the file
# itself is owned by audiorec. Everything else on the system still can't see
# it (no world permissions).
install -d -m 0770 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR/recordings"

c_info "code: copying src to $INSTALL_DIR/src"
rm -rf "$INSTALL_DIR/src"
cp -a "$REPO_DIR/src" "$INSTALL_DIR/src"
cp -a "$REPO_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
chown -R root:root "$INSTALL_DIR/src" "$INSTALL_DIR/requirements.txt"

# --- Waveshare Touch e-Paper HAT driver (vendored as a git clone) ---------
# Only fetched if someone later enables [display].enabled in config.toml,
# but keeping it pre-installed means zero-friction when they do. The clone
# is ~500 KB so it costs nothing to have on disk.
c_info "vendor: ensuring Waveshare Touch_e-Paper_HAT driver is present"
VENDOR_DIR="$INSTALL_DIR/vendor"
install -d -m 0755 -o root -g root "$VENDOR_DIR"
TPLIB_DIR="$VENDOR_DIR/Touch_e-Paper_HAT"
if [[ ! -d "$TPLIB_DIR/.git" ]]; then
    rm -rf "$TPLIB_DIR"
    if git clone --depth 1 https://github.com/waveshare/Touch_e-Paper_HAT.git "$TPLIB_DIR"; then
        c_ok "cloned Waveshare driver into $TPLIB_DIR"
    else
        c_warn "could not clone Waveshare driver -- the e-paper service will idle until this succeeds"
    fi
else
    # Fast-forward an existing clone so rev bumps land on re-install.
    ( cd "$TPLIB_DIR" && git fetch --depth 1 origin 2>/dev/null || true; git reset --hard origin/HEAD 2>/dev/null || true ) || true
    c_ok "updated vendored driver at $TPLIB_DIR"
fi
chown -R root:root "$VENDOR_DIR"

# --- Enable SPI + I2C interfaces (required by the e-paper HAT) ------------
# Using raspi-config nonint so we don't prompt. These are no-ops if the
# interfaces are already enabled.
c_info "interfaces: enabling SPI + I2C (for e-paper HAT)"
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_spi 0 || c_warn "raspi-config do_spi failed"
    raspi-config nonint do_i2c 0 || c_warn "raspi-config do_i2c failed"
else
    # Fallback: append dtparam lines if the tool isn't available (non-Pi-OS).
    CONFIG_TXT="/boot/firmware/config.txt"
    [[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"
    if [[ -f "$CONFIG_TXT" ]]; then
        grep -q '^dtparam=spi=on' "$CONFIG_TXT" || echo 'dtparam=spi=on' >> "$CONFIG_TXT"
        grep -q '^dtparam=i2c_arm=on' "$CONFIG_TXT" || echo 'dtparam=i2c_arm=on' >> "$CONFIG_TXT"
    fi
fi

# Make sure the audiorec user can actually see /dev/spidev* and /dev/i2c-*.
usermod -a -G spi,i2c "$SERVICE_USER" 2>/dev/null || true

c_info "helper: installing $HELPER_BIN"
install -m 0755 -o root -g root "$REPO_DIR/scripts/audiorec-setup-helper" "$HELPER_BIN"

c_info "sudoers: allowing '$SERVICE_USER' to invoke the helper"
install -m 0440 -o root -g root "$REPO_DIR/config/audiorec.sudoers" /etc/sudoers.d/audiorec
# Validate so a bad sudoers can't lock anyone out.
if ! visudo -c -f /etc/sudoers.d/audiorec >/dev/null 2>&1; then
    c_warn "sudoers file failed validation; removing"
    rm -f /etc/sudoers.d/audiorec
fi

c_info "venv: building Python virtualenv"
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

c_info "config: ensuring $CONFIG_FILE exists (preserving if present)"
if [[ ! -f "$CONFIG_FILE" ]]; then
    # Minimal skeleton with a fresh session secret. Everything else gets filled
    # in by the web setup wizard.
    SESSION_SECRET="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
    umask 027
    cat > "$CONFIG_FILE" <<EOF
# Created by scripts/install.sh on $(date -Iseconds).
# The web setup wizard fills in the rest. You can hand-edit this file too;
# services need a restart after changes (sudo systemctl restart audiorec-*).

[meta]
setup_complete = false

[paths]
data_dir = "$DATA_DIR"
recordings_subdir = "recordings"

[audio]
device = ""
sample_rate = 16000
channels = 1
format = "S16_LE"

[gpio]
# BCM pin the handset cradle switch is wired to. Default is BCM5 because
# the 2.13" Touch e-Paper HAT claims BCM17 as its RST line -- if you're
# not using the HAT, any unused pin works (BCM17 was the old default).
button_pin = 5
led_pin = 18
debounce_s = 0.05
long_press_s = 3.0

[display]
# Set enabled = true after wiring up the Waveshare 2.13" Touch e-Paper HAT.
# The display service stays idle (zero CPU) until this flag flips on.
enabled = false
panel = "2in13_V4"
rotate_deg = 90
thank_you_seconds = 5
idle_full_refresh_s = 600
max_name_length = 20

[upload]
poll_interval_s = 5
local_retention_days = 7
# Cloud file format: "mp3" (universal, default), "opus" (smallest / best voice
# quality, but not playable in QuickTime/Windows Media), or "wav" (lossless,
# ~10 MB/min).
format = "mp3"
# Bitrate for lossy formats. 64k MP3 = clean voice; 128k = podcast-tier.
# Ignored when format = "wav".
bitrate = "64k"
multipart_part_size_mb = 5
max_retries = 10
# ffmpeg audio filter chain applied before encoding. Kills mains hum,
# ultrasonic switching noise from the Pi, and constant hiss. Set to ""
# to disable entirely, or tune - see README "Improving audio quality".
audio_filters = "highpass=f=80,lowpass=f=8000,afftdn=nr=12"

[cloud]
provider = ""
access_key = ""
secret_key = ""
endpoint_url = ""
region = ""
bucket = ""
key_prefix = "recordings/"

[web]
host = "0.0.0.0"
port = 80
username = ""
password_hash = ""
session_secret = "$SESSION_SECRET"
session_lifetime_s = 2592000
hostname = "audiorec.local"
EOF
    chown "$SERVICE_USER":"$SERVICE_USER" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
    c_ok "wrote fresh $CONFIG_FILE (setup wizard will fill it in)"
else
    # Make sure the webapp can still read + rewrite it across upgrades.
    chown "$SERVICE_USER":"$SERVICE_USER" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
    c_ok "kept existing $CONFIG_FILE"
fi

c_info "avahi: installing service advertisement"
install -m 0644 -o root -g root \
    "$REPO_DIR/config/audiorec.avahi.service" \
    /etc/avahi/services/audiorec.service
systemctl enable --now avahi-daemon

c_info "comitup: installing config (WiFi AP fallback)"
if dpkg -s comitup >/dev/null 2>&1; then
    install -m 0644 -o root -g root "$REPO_DIR/config/comitup.conf" /etc/comitup.conf
    systemctl enable comitup || true
else
    c_warn "comitup not installed; skipping"
fi

c_info "systemd: installing units"
install -m 0644 "$REPO_DIR/systemd/audiorec-recorder.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/systemd/audiorec-uploader.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/systemd/audiorec-webapp.service"  /etc/systemd/system/
install -m 0644 "$REPO_DIR/systemd/audiorec-display.service" /etc/systemd/system/
systemctl daemon-reload

c_info "systemd: enabling + starting services (auto-start on boot too)"
systemctl enable audiorec-recorder.service
systemctl enable audiorec-uploader.service
systemctl enable audiorec-webapp.service
systemctl enable audiorec-display.service
systemctl restart audiorec-recorder.service
systemctl restart audiorec-uploader.service
systemctl restart audiorec-webapp.service
systemctl restart audiorec-display.service

sleep 2
for svc in audiorec-recorder audiorec-uploader audiorec-webapp audiorec-display; do
    if systemctl is-active --quiet "$svc"; then
        c_ok "$svc is running"
    else
        c_warn "$svc failed to start - check 'journalctl -u $svc -n 30'"
    fi
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOSTNAME_LOCAL="$(hostname).local"
cat <<EOF

========================================================================
Install complete. Nothing else to type into a shell.

Open one of these on your phone (same WiFi):
  http://$HOSTNAME_LOCAL
  http://$IP                 (fallback if .local doesn't resolve)

You'll land on the setup wizard. Finish these steps in the browser:
  1. Create admin account (username + password)
  2. Pick your USB microphone (tap "Test" to confirm it works)
  3. Enter cloud storage creds (B2 / R2 / Wasabi / DO / custom S3)
  4. (Optional) Change the hostname
  5. Hit "Finish"

No WiFi configured yet? Look for SSID 'audiorec-XXXX' on your phone,
connect, and use the captive portal to give it your WiFi creds.

All three services are set to auto-start on boot -- just power-cycle the
Pi and it'll come back up on its own.
========================================================================
EOF
