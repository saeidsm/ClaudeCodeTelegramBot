#!/usr/bin/env bash
# Delete snapshot directories older than $1 days (default: env or 30).
set -euo pipefail

APP_DIR="/opt/sentry-nightwatch"
DAYS="${1:-${NIGHTWATCH_RETENTION_DAYS:-30}}"

cd "$APP_DIR"
exec "$APP_DIR/venv/bin/python" -m app.main prune --days "$DAYS"
