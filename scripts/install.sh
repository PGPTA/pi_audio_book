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
    python3 python3-venv python3-pip \
    alsa-utils ffmpeg \
    avahi-daemon \
    ca-certificates \
    sudo

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
button_pin = 17
led_pin = 18
debounce_s = 0.05
long_press_s = 3.0

[upload]
poll_interval_s = 5
local_retention_days = 7
opus_bitrate = "32k"
multipart_part_size_mb = 5
max_retries = 10
# ffmpeg audio filter chain applied before Opus encoding. Kills mains hum,
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
systemctl daemon-reload

c_info "systemd: enabling + starting services (auto-start on boot too)"
systemctl enable audiorec-recorder.service
systemctl enable audiorec-uploader.service
systemctl enable audiorec-webapp.service
systemctl restart audiorec-recorder.service
systemctl restart audiorec-uploader.service
systemctl restart audiorec-webapp.service

sleep 2
for svc in audiorec-recorder audiorec-uploader audiorec-webapp; do
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
