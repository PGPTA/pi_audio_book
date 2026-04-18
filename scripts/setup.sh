#!/usr/bin/env bash
# One-shot interactive bootstrap for audiorec.
#
# Run this ONCE after cloning the repo on a fresh Pi OS:
#   sudo bash scripts/setup.sh
#
# It will:
#   1. run install.sh (apt deps, user, venv, systemd units)
#   2. auto-detect your USB mic
#   3. prompt for Wasabi creds, web password, hostname
#   4. write /etc/audiorec/config.toml
#   5. set the hostname (so http://<name>.local works)
#   6. start all three services
#   7. show you the URL to open on your phone

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root: sudo bash scripts/setup.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="/etc/audiorec"
CONFIG_FILE="$CONFIG_DIR/config.toml"
INSTALL_DIR="/opt/audiorec"
SERVICE_USER="audiorec"

c_bold() { printf '\033[1m%s\033[0m\n' "$*"; }
c_info() { printf '\033[36m>>> %s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m!!! %s\033[0m\n' "$*"; }
c_ok()   { printf '\033[32m[OK] %s\033[0m\n' "$*"; }

ask() {
    local prompt="$1" default="${2:-}" var
    if [[ -n "$default" ]]; then
        read -r -p "$prompt [$default]: " var
        echo "${var:-$default}"
    else
        read -r -p "$prompt: " var
        echo "$var"
    fi
}

ask_secret() {
    local prompt="$1" var
    read -r -s -p "$prompt: " var
    echo >&2
    printf '%s' "$var"
}

# --- 1. run installer -----------------------------------------------------
c_info "Step 1/6: installing apt packages, user, venv, systemd units"
bash "$REPO_DIR/scripts/install.sh"

# --- 2. detect USB mic ----------------------------------------------------
c_info "Step 2/6: detecting microphones"
if ! command -v arecord >/dev/null; then
    c_warn "arecord not found; install failed?"
    exit 1
fi

# Gather all capture-capable cards, skipping the Pi's built-in bcm2835.
#
# `arecord -l` lines look like:
#   card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
#
# We want the hw:CARD,DEVICE id plus a friendly name.
declare -a MIC_DEVICES=()  # e.g. "hw:1,0"
declare -a MIC_LABELS=()   # e.g. "USB PnP Sound Device (USB Audio)"

while IFS= read -r line; do
    if [[ "$line" =~ ^card[[:space:]]+([0-9]+):[[:space:]]+([^[:space:]]+)[[:space:]]+\[([^\]]+)\],[[:space:]]+device[[:space:]]+([0-9]+):[[:space:]]+[^[]*\[([^\]]+)\] ]]; then
        card="${BASH_REMATCH[1]}"
        card_id="${BASH_REMATCH[2]}"
        card_name="${BASH_REMATCH[3]}"
        dev="${BASH_REMATCH[4]}"
        dev_name="${BASH_REMATCH[5]}"
        # Skip the Pi's built-in bcm283x audio (headphones/HDMI), not a mic.
        if [[ "$card_id" =~ [Bb]cm ]] || [[ "$card_name" =~ [Bb]cm ]]; then
            continue
        fi
        MIC_DEVICES+=("hw:${card},${dev}")
        MIC_LABELS+=("${card_name} - ${dev_name}")
    fi
done < <(arecord -l 2>/dev/null || true)

echo
case "${#MIC_DEVICES[@]}" in
    0)
        c_warn "No USB microphone detected."
        echo "Available capture cards:"
        arecord -l 2>/dev/null || echo "  (none)"
        echo
        echo "Plug in a USB mic and re-run, or enter a device manually."
        AUDIO_DEVICE="$(ask "ALSA device (format: hw:CARD,DEVICE)" "hw:1,0")"
        ;;
    1)
        AUDIO_DEVICE="${MIC_DEVICES[0]}"
        c_ok "Detected: $AUDIO_DEVICE  (${MIC_LABELS[0]})"
        CONFIRM="$(ask "Use this mic?" "y")"
        if [[ "$CONFIRM" =~ ^[Nn] ]]; then
            AUDIO_DEVICE="$(ask "Enter ALSA device manually" "$AUDIO_DEVICE")"
        fi
        ;;
    *)
        echo "Multiple capture devices found:"
        for i in "${!MIC_DEVICES[@]}"; do
            printf "  %d) %-12s  %s\n" $((i+1)) "${MIC_DEVICES[$i]}" "${MIC_LABELS[$i]}"
        done
        echo "  $(( ${#MIC_DEVICES[@]} + 1 ))) enter manually"
        PICK="$(ask "Choose [1-$(( ${#MIC_DEVICES[@]} + 1 ))]" "1")"
        if [[ "$PICK" =~ ^[0-9]+$ ]] && (( PICK >= 1 && PICK <= ${#MIC_DEVICES[@]} )); then
            AUDIO_DEVICE="${MIC_DEVICES[$((PICK-1))]}"
        else
            AUDIO_DEVICE="$(ask "Enter ALSA device manually" "${MIC_DEVICES[0]}")"
        fi
        ;;
esac

# Quick 1-second capture test so we fail fast if something's wrong.
c_info "Testing $AUDIO_DEVICE with a 1-second capture..."
if arecord -q -D "$AUDIO_DEVICE" -f S16_LE -r 16000 -c 1 -d 1 /dev/null >/dev/null 2>&1; then
    c_ok "Mic works"
else
    c_warn "Capture test failed - the mic may be busy, unplugged, or need a different format."
    KEEP="$(ask "Proceed with $AUDIO_DEVICE anyway?" "y")"
    if [[ "$KEEP" =~ ^[Nn] ]]; then
        AUDIO_DEVICE="$(ask "Enter a different ALSA device" "$AUDIO_DEVICE")"
    fi
fi

# --- 3. interactive config ------------------------------------------------
c_info "Step 3/6: configuring"

HOSTNAME_DEFAULT="audiorec"
while true; do
    RAW_HOSTNAME="$(ask "Short hostname (just a name, e.g. 'audiorec' - no http, no .local, no slashes)" "$HOSTNAME_DEFAULT")"
    # Normalize: strip whitespace, drop any leading http(s)://, drop .local suffix, lowercase.
    NEW_HOSTNAME="$(printf '%s' "$RAW_HOSTNAME" | tr '[:upper:]' '[:lower:]' | sed -E 's#^[[:space:]]+|[[:space:]]+$##g; s#^https?://##; s#/.*$##; s#\.local$##')"
    if [[ -z "$NEW_HOSTNAME" ]]; then
        c_warn "Hostname cannot be empty."
        continue
    fi
    # RFC-1123 short hostname: letters, digits, hyphens; must not start or end with hyphen; <=63 chars.
    if [[ ! "$NEW_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
        c_warn "Invalid hostname '$NEW_HOSTNAME'. Use letters, digits, and hyphens only."
        continue
    fi
    if [[ "$NEW_HOSTNAME" != "$RAW_HOSTNAME" ]]; then
        c_info "Using '$NEW_HOSTNAME' (normalized from '$RAW_HOSTNAME')"
    fi
    break
done

echo
c_bold "Cloud storage (S3-compatible)"
echo "Pick your provider:"
echo "  1) Backblaze B2        (recommended, 10 GB free)"
echo "  2) Cloudflare R2       (10 GB free, zero egress)"
echo "  3) Wasabi              (\$7/TB, 90-day min storage)"
echo "  4) DigitalOcean Spaces"
echo "  5) Custom / other S3-compatible"
PROVIDER_CHOICE="$(ask "Choice" "1")"

case "$PROVIDER_CHOICE" in
    1)
        echo "Create a bucket + application key at https://secure.backblaze.com"
        echo "(Use the bucket's Endpoint, e.g. 's3.us-west-004.backblazeb2.com'.)"
        WASABI_REGION_DEFAULT="us-west-004"
        WASABI_REGION="$(ask "B2 region (from your bucket's endpoint)" "$WASABI_REGION_DEFAULT")"
        WASABI_ENDPOINT_DEFAULT="https://s3.${WASABI_REGION}.backblazeb2.com"
        ;;
    2)
        echo "Create a bucket + API token at https://dash.cloudflare.com -> R2"
        echo "Your endpoint looks like 'https://<account-id>.r2.cloudflarestorage.com'."
        ACCOUNT_ID="$(ask "R2 Account ID")"
        WASABI_REGION="auto"
        WASABI_ENDPOINT_DEFAULT="https://${ACCOUNT_ID}.r2.cloudflarestorage.com"
        ;;
    3)
        echo "Create a bucket + access key at https://console.wasabisys.com"
        WASABI_REGION="$(ask "Wasabi region" "us-east-1")"
        WASABI_ENDPOINT_DEFAULT="https://s3.${WASABI_REGION}.wasabisys.com"
        ;;
    4)
        echo "Create a Space + access key at https://cloud.digitalocean.com/spaces"
        WASABI_REGION="$(ask "DO region" "nyc3")"
        WASABI_ENDPOINT_DEFAULT="https://${WASABI_REGION}.digitaloceanspaces.com"
        ;;
    *)
        WASABI_REGION="$(ask "Region")"
        WASABI_ENDPOINT_DEFAULT=""
        ;;
esac

WASABI_ENDPOINT="$(ask "Endpoint URL" "$WASABI_ENDPOINT_DEFAULT")"
WASABI_ACCESS="$(ask "Access key ID")"
WASABI_SECRET="$(ask_secret "Secret access key")"
WASABI_BUCKET="$(ask "Bucket name")"
WASABI_PREFIX="$(ask "Key prefix (folder) inside the bucket" "recordings/")"

echo
c_bold "Web UI login"
WEB_USER="$(ask "Username" "admin")"
while true; do
    WEB_PW1="$(ask_secret "Password (min 6 chars)")"
    if [[ ${#WEB_PW1} -lt 6 ]]; then
        c_warn "Too short, try again."
        continue
    fi
    WEB_PW2="$(ask_secret "Confirm password")"
    if [[ "$WEB_PW1" != "$WEB_PW2" ]]; then
        c_warn "Passwords did not match, try again."
        continue
    fi
    break
done

c_info "Hashing password"
PW_HASH="$(printf '%s' "$WEB_PW1" | "$INSTALL_DIR/venv/bin/python" -c '
import bcrypt, sys
pw = sys.stdin.buffer.read()
print(bcrypt.hashpw(pw, bcrypt.gensalt()).decode())
')"

SESSION_SECRET="$(openssl rand -hex 32)"

# --- 4. write config ------------------------------------------------------
c_info "Step 4/6: writing $CONFIG_FILE"

# Back up any existing config just in case.
if [[ -f "$CONFIG_FILE" ]]; then
    cp -a "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%s)"
fi

cat > "$CONFIG_FILE" <<EOF
# Generated by scripts/setup.sh on $(date -Iseconds)

[paths]
data_dir = "/var/lib/audiorec"
recordings_subdir = "recordings"

[audio]
device = "$AUDIO_DEVICE"
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
opus_bitrate = "24k"
multipart_part_size_mb = 5
max_retries = 10

[wasabi]
access_key = "$WASABI_ACCESS"
secret_key = "$WASABI_SECRET"
endpoint_url = "$WASABI_ENDPOINT"
region = "$WASABI_REGION"
bucket = "$WASABI_BUCKET"
key_prefix = "$WASABI_PREFIX"

[web]
host = "0.0.0.0"
port = 80
username = "$WEB_USER"
password_hash = "$PW_HASH"
session_secret = "$SESSION_SECRET"
session_lifetime_s = 2592000
hostname = "$NEW_HOSTNAME.local"
EOF

chown root:"$SERVICE_USER" "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"
c_ok "wrote $CONFIG_FILE"

# --- 5. hostname ----------------------------------------------------------
c_info "Step 5/6: setting system hostname to '$NEW_HOSTNAME'"
CURRENT_HOSTNAME="$(hostname)"
if [[ "$CURRENT_HOSTNAME" != "$NEW_HOSTNAME" ]]; then
    hostnamectl set-hostname "$NEW_HOSTNAME"
    # Rewrite /etc/hosts safely (no sed: avoids any issue with characters in
    # the hostname, and handles both "line exists" and "line missing" cases).
    awk -v h="$NEW_HOSTNAME" '
        BEGIN { done = 0 }
        /^127\.0\.1\.1[[:space:]]/ { print "127.0.1.1\t" h; done = 1; next }
        /^127\.0\.1\.1$/           { print "127.0.1.1\t" h; done = 1; next }
        { print }
        END { if (!done) print "127.0.1.1\t" h }
    ' /etc/hosts > /etc/hosts.audiorec.new && mv /etc/hosts.audiorec.new /etc/hosts
    systemctl restart avahi-daemon || true
fi

# --- 6. start services ----------------------------------------------------
c_info "Step 6/6: starting services"
systemctl daemon-reload
systemctl restart audiorec-recorder audiorec-uploader audiorec-webapp

sleep 2
for svc in audiorec-recorder audiorec-uploader audiorec-webapp; do
    if systemctl is-active --quiet "$svc"; then
        c_ok "$svc is running"
    else
        c_warn "$svc failed to start - check 'journalctl -u $svc -n 30'"
    fi
done

IP="$(hostname -I | awk '{print $1}')"
echo
c_bold "=========================================================="
c_bold "  Done! Open on your phone (same WiFi):"
echo   "    http://$NEW_HOSTNAME.local"
echo   "    http://$IP              (fallback if .local doesn't resolve)"
echo
echo   "  Press your button (GPIO17 to GND) to start/stop recording."
echo   "  Tail the recorder: journalctl -fu audiorec-recorder"
c_bold "=========================================================="
