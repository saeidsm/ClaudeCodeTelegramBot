#!/usr/bin/env bash
# Idempotent install: venv, deps, systemd link, permissions.
# Re-runs are safe — existing venvs and units are left in place unless --force is passed.
set -euo pipefail

APP_DIR="/opt/sentry-nightwatch"
VENV="$APP_DIR/venv"
UNIT_DIR="/etc/systemd/system"
FORCE=0

if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

cd "$APP_DIR"

# 1. Python venv + deps
if [[ ! -d "$VENV" || $FORCE -eq 1 ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements.txt

# 2. .env scaffold (never overwrites)
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "Created $APP_DIR/.env from template — fill in SENTRY_AUTH_TOKEN before enabling timer."
fi

# 3. Snapshot dir perms
mkdir -p "$APP_DIR/snapshots"
chmod 750 "$APP_DIR/snapshots"

# 4. Systemd unit symlinks (still requires manual enable)
if [[ -d "$UNIT_DIR" ]]; then
  ln -sf "$APP_DIR/systemd/nightwatch-daily.service" "$UNIT_DIR/nightwatch-daily.service"
  ln -sf "$APP_DIR/systemd/nightwatch-daily.timer" "$UNIT_DIR/nightwatch-daily.timer"
  systemctl daemon-reload
  echo "Systemd unit files linked. To enable:"
  echo "  systemctl enable --now nightwatch-daily.timer"
fi

# 5. Smoke
"$VENV/bin/python" -m app.redactor

echo "install.sh complete."
