#!/usr/bin/env bash
# One-shot bootstrap for audiorec. Runs on the Pi after cloning the repo:
#
#   sudo bash scripts/setup.sh
#
# All real configuration (admin password, mic, cloud creds, hostname) is
# then done from the web setup wizard at http://<pi>.local or the Pi's
# IP. The services are also enabled to auto-start on boot so a power
# cycle "just works".

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$REPO_DIR/scripts/install.sh" "$@"
