#!/usr/bin/env bash
# Removes audiorec services and code.
#
#   sudo bash scripts/uninstall.sh             # keeps config + data
#   sudo bash scripts/uninstall.sh --purge     # wipe config + data + user

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root." >&2
    exit 1
fi

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=1
fi

echo ">>> stopping and disabling services"
for svc in audiorec-recorder audiorec-uploader audiorec-webapp; do
    systemctl disable --now "$svc" 2>/dev/null || true
    rm -f "/etc/systemd/system/${svc}.service"
done
systemctl daemon-reload

echo ">>> removing /opt/audiorec and helper/sudoers"
rm -rf /opt/audiorec
rm -f /etc/sudoers.d/audiorec
rm -f /etc/avahi/services/audiorec.service

if [[ $PURGE -eq 1 ]]; then
    echo ">>> purging config and data (--purge)"
    rm -rf /etc/audiorec /var/lib/audiorec
    userdel audiorec 2>/dev/null || true
else
    echo ">>> keeping /etc/audiorec and /var/lib/audiorec"
    echo "    (re-run install to reuse your existing config without retyping anything)"
fi

echo "Done."
