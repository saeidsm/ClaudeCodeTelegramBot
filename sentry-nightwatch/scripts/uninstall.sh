#!/usr/bin/env bash
# Clean revert. Leaves snapshots/ and .env in place by default.
# Pass --purge to also wipe snapshots and .env.
set -euo pipefail

APP_DIR="/opt/sentry-nightwatch"
UNIT_DIR="/etc/systemd/system"
PURGE=0

if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

# Stop & disable systemd timer.
if systemctl list-unit-files | grep -q '^nightwatch-daily\.timer'; then
  systemctl disable --now nightwatch-daily.timer || true
fi

# Remove unit symlinks.
rm -f "$UNIT_DIR/nightwatch-daily.service" "$UNIT_DIR/nightwatch-daily.timer"
systemctl daemon-reload || true

# Remove venv.
rm -rf "$APP_DIR/venv"

if [[ $PURGE -eq 1 ]]; then
  rm -f "$APP_DIR/.env"
  rm -rf "$APP_DIR/snapshots"
  echo "Purged .env and snapshots/."
else
  echo ".env and snapshots/ left intact. Pass --purge to remove them."
fi

echo "uninstall.sh complete."
