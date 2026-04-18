#!/usr/bin/env bash
# Audiorec installer for Raspberry Pi OS (Bookworm/Bullseye).
#
# Run from the checked-out repo on the Pi:
#   sudo bash scripts/install.sh
#
# This:
#   - installs apt deps (python, ffmpeg, alsa, avahi, comitup)
#   - creates the 'audiorec' service user
#   - copies source to /opt/audiorec and builds a venv
#   - installs config templates to /etc/audiorec (preserving any existing file)
#   - installs systemd units and enables them
#   - configures avahi + comitup

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root: sudo bash scripts/install.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/audiorec"
CONFIG_DIR="/etc/audiorec"
DATA_DIR="/var/lib/audiorec"
SERVICE_USER="audiorec"

echo ">>> apt: installing system packages"
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    alsa-utils ffmpeg \
    avahi-daemon \
    ca-certificates

# Comitup lives in Debian contrib; some images have it, some don't. Try hard
# but don't fail the install if it's genuinely unavailable -- the rest of the
# system still works, you just won't have the WiFi-AP fallback.
if ! dpkg -s comitup >/dev/null 2>&1; then
    if ! apt-get install -y --no-install-recommends comitup; then
        echo "!!! comitup package not available from apt; see README for manual install." >&2
    fi
fi

echo ">>> user: ensuring '$SERVICE_USER' exists"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin \
        --groups audio,gpio "$SERVICE_USER"
else
    usermod -a -G audio,gpio "$SERVICE_USER"
fi

echo ">>> dirs: creating $INSTALL_DIR, $CONFIG_DIR, $DATA_DIR"
install -d -m 0755 -o root -g root "$INSTALL_DIR"
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR/recordings"

echo ">>> code: copying src to $INSTALL_DIR/src"
rm -rf "$INSTALL_DIR/src"
cp -a "$REPO_DIR/src" "$INSTALL_DIR/src"
cp -a "$REPO_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
chown -R root:root "$INSTALL_DIR/src" "$INSTALL_DIR/requirements.txt"

echo ">>> venv: creating Python virtualenv"
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo ">>> config: installing template (preserving existing)"
if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    install -m 0640 -o root -g "$SERVICE_USER" \
        "$REPO_DIR/config/config.toml.example" "$CONFIG_DIR/config.toml"
    echo "    -> wrote $CONFIG_DIR/config.toml (edit this before starting services!)"
else
    echo "    -> $CONFIG_DIR/config.toml already exists; not overwriting"
fi

echo ">>> avahi: installing service advertisement"
install -m 0644 -o root -g root \
    "$REPO_DIR/config/audiorec.avahi.service" \
    /etc/avahi/services/audiorec.service
systemctl enable --now avahi-daemon

echo ">>> comitup: installing config (if comitup is installed)"
if dpkg -s comitup >/dev/null 2>&1; then
    install -m 0644 -o root -g root "$REPO_DIR/config/comitup.conf" /etc/comitup.conf
    systemctl enable comitup || true
else
    echo "    -> comitup not installed; skipping"
fi

echo ">>> systemd: installing units"
install -m 0644 "$REPO_DIR/systemd/audiorec-recorder.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/systemd/audiorec-uploader.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/systemd/audiorec-webapp.service"  /etc/systemd/system/
systemctl daemon-reload

echo ">>> systemd: enabling services"
systemctl enable audiorec-recorder.service
systemctl enable audiorec-uploader.service
systemctl enable audiorec-webapp.service

cat <<EOF

========================================================================
Install complete.

Next steps:
  1. Generate a web password hash:
       sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/python \\
           $REPO_DIR/scripts/hash_password.py
  2. Generate a session secret:
       openssl rand -hex 32
  3. Edit $CONFIG_DIR/config.toml and set:
       - [audio].device       (run 'arecord -l' to find your USB mic)
       - [wasabi].access_key, secret_key, bucket, endpoint_url, region
       - [web].password_hash, session_secret
  4. Start the services:
       sudo systemctl start audiorec-recorder audiorec-uploader audiorec-webapp
  5. Open http://audiorec.local on your phone (same WiFi).
     First boot without WiFi? Look for SSID 'audiorec-XXX' on your phone,
     connect, and enter your WiFi creds in the captive portal.
========================================================================
EOF
