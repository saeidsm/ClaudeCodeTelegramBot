#!/usr/bin/env bash
#
# deploy-phase1-phase2.sh — ONE transactional, manifest-driven installer for the
# combined Phase-1 + Phase-2 bot deployment (see docs/PHASE2_DEPLOY_RUNBOOK.md).
#
# It exists so deployment is an EXACT, tested procedure instead of Claude
# inventing commands at deploy time (the failure mode behind the prior deploy
# loops). Default is read-only; mutation is gated behind the reviewed PR + merged
# SHA and an explicit --execute, runs under one flock from an immutable detached
# source, records a PRESENT_BEFORE manifest for every artifact, edits .env only
# through a byte-preserving allow-list (never source/eval), performs the Phase-1
# retention migration + Phase-2 artifact install, runs a live-dir import smoke
# using the real PTB construction/footer path, restarts EXACTLY once, and rolls
# everything back from the manifest on any post-mutation failure.
#
# Usage:
#   deploy-phase1-phase2.sh --preflight
#   deploy-phase1-phase2.sh --execute --reviewed-pr 19 --merged-sha <40-hex>
#
# Options:
#   --preflight | --dry-run   read-only gates only; never mutates (default)
#   --execute                 perform the transaction (needs the gate below)
#   --reviewed-pr <N>         the reviewed PR number (must equal 19)
#   --merged-sha <SHA>        the merged commit; must equal the source HEAD and
#                             origin/main (verified read-only)
#   --source <DIR>            immutable detached worktree to install FROM
#   --test-root <DIR>         TEST ONLY: retarget the live root to a dir under
#                             /tmp (production root is otherwise hardcoded)
#
# Test seams (honoured only when set; documented so the offline suite is exact):
#   DEPLOY_SOURCE, DEPLOY_CRON_DIR, DEPLOY_SYSTEMD_DIR, DEPLOY_BACKUP_ROOT,
#   DEPLOY_LOCK, DEPLOY_PYTHON, DEPLOY_REPORT, DEPLOY_FAIL_AT
#
# Final report (exactly one) written to $DEPLOY_REPORT:
#   DEPLOYED | ROLLED_BACK | STOPPED_BEFORE_MUTATION
set -euo pipefail

# ── hardcoded production scope (never freely redefinable) ───────────────────
PROD_ROOT="/opt/shahrzad-devops"

MODE="preflight"
REVIEWED_PR=""
MERGED_SHA=""
TEST_ROOT=""
SOURCE="${DEPLOY_SOURCE:-}"

# Allow-listed, NON-SECRET env keys. Anything else — TELEGRAM_BOT_TOKEN,
# OPENROUTER_API_KEY, ANTHROPIC_*, any *_KEY/*_TOKEN/*_SECRET — is refused.
ALLOWLIST=" BOT_MAX_SESSIONS BOT_MAX_RUNNING_AGENTS BOT_NIGHTWATCH_IPC_BIND2 \
BOT_CHAT_SESSIONS_DIR BOT_COORDINATION_FILE BOT_CHAT_CATALOG_TTL \
BOT_CHAT_CONCURRENCY BOT_CHAT_HISTORY_MAX_TURNS BOT_CHAT_HISTORY_MAX_CHARS \
BOT_CODEX_DEFAULT_MODEL BOT_CODEX_SANDBOX BOT_CLAUDE_MODELS BOT_CODEX_MODELS \
BOT_REPORT_RETENTION_DAYS BOT_COORD_PUBLISHER "

log()  { echo "[deploy] $*" >&2; }
die()  { echo "[deploy] NO-GO: $*" >&2; exit 3; }
fail_at() { [ "${DEPLOY_FAIL_AT:-}" = "$1" ] && { log "fault-injection at stage '$1'"; return 0; } || return 1; }

# ── arg parse ───────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --preflight|--dry-run) MODE="preflight" ;;
    --execute)             MODE="execute" ;;
    --reviewed-pr) REVIEWED_PR="${2:-}"; shift ;;
    --merged-sha)  MERGED_SHA="${2:-}"; shift ;;
    --source)      SOURCE="${2:-}"; shift ;;
    --test-root)   TEST_ROOT="${2:-}"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# ── resolve live root (protected paths cannot be retargeted) ────────────────
if [ -n "$TEST_ROOT" ]; then
  case "$(readlink -f "$TEST_ROOT" 2>/dev/null || echo "$TEST_ROOT")" in
    /tmp/*) : ;;
    *) die "--test-root must resolve under /tmp (refusing to retarget a protected path)";;
  esac
  LIVE_ROOT="$TEST_ROOT"
else
  LIVE_ROOT="$PROD_ROOT"
fi

LIVE_SCRIPTS="$LIVE_ROOT/scripts"
LIVE_ENGINES="$LIVE_SCRIPTS/engines"
ENV_FILE="$LIVE_ROOT/.env"
CONFIGS="$LIVE_ROOT/configs"
CRON_DIR="${DEPLOY_CRON_DIR:-/etc/cron.d}"
SYSTEMD_DIR="${DEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-$LIVE_ROOT/backups}"
LOCK="${DEPLOY_LOCK:-$LIVE_ROOT/.deploy.lock}"
PYTHON="${DEPLOY_PYTHON:-python3}"
REPORT="${DEPLOY_REPORT:-/tmp/phase12-deploy-report.md}"
SERVICE="claude-telegram-bot"

[ -n "$SOURCE" ] || SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$SOURCE" ] || die "source dir not found: $SOURCE"

STATUS="STOPPED_BEFORE_MUTATION"
ARMED=0
MANIFEST=""
BACKUP=""

# ── artifact table: "<abs dest>|<abs source or '' for remove/config>" ───────
artifacts() {
  echo "$LIVE_SCRIPTS/claude-telegram-bot.py|$SOURCE/bot.py"
  echo "$LIVE_SCRIPTS/coordination.py|$SOURCE/coordination.py"
  echo "$LIVE_SCRIPTS/resource_footer.py|$SOURCE/resource_footer.py"
  echo "$LIVE_SCRIPTS/coord_publish.py|$SOURCE/scripts/coord_publish.py"
  echo "$LIVE_ENGINES|$SOURCE/engines"                         # whole package dir
  echo "$LIVE_SCRIPTS/log_filters.py|$SOURCE/log_filters.py"   # runtime dep of bot.py
  echo "$LIVE_SCRIPTS/video_module|$SOURCE/video_module"       # runtime package dep
  echo "$LIVE_SCRIPTS/cleanup-reports.sh|$SOURCE/scripts/cleanup-reports.sh"
  echo "$SYSTEMD_DIR/reports-cleanup.service|$SOURCE/systemd/reports-cleanup.service"
  echo "$SYSTEMD_DIR/reports-cleanup.timer|$SOURCE/systemd/reports-cleanup.timer"
}
# Conflicting retention paths REMOVED as part of the migration (record+restore).
removals() {
  echo "$CRON_DIR/cleanup-reports"
  echo "$CRON_DIR/nightwatch-reports-cleanup"
}

sha() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "-"; }

# ── manifest / backup ───────────────────────────────────────────────────────
record() {
  # record <path>; copies present artifact into backup, notes present/absent
  local p="$1" key state
  key="$(echo "$p" | sha256sum | awk '{print $1}')"
  if [ -e "$p" ] || [ -L "$p" ]; then
    state="present"
    cp -a "$p" "$BACKUP/$key"
  else
    state="absent"
  fi
  printf '%s\t%s\t%s\n' "$state" "$key" "$p" >> "$MANIFEST"
}

build_manifest() {
  BACKUP="$BACKUP_ROOT/deploy-$(date +%Y%m%d-%H%M%S)-$$"
  mkdir -p "$BACKUP"
  MANIFEST="$BACKUP/MANIFEST.tsv"
  : > "$MANIFEST"
  local line dest
  while IFS= read -r line; do
    dest="${line%%|*}"; record "$dest"
  done < <(artifacts)
  while IFS= read -r dest; do record "$dest"; done < <(removals)
  record "$ENV_FILE"
  record "$CONFIGS/reports-cleanup.env"
  log "PRESENT_BEFORE manifest: $MANIFEST"
}

rollback() {
  log "ROLLING BACK from $MANIFEST"
  local state key path
  while IFS=$'\t' read -r state key path; do
    if [ "$state" = "present" ]; then
      rm -rf "$path"
      cp -a "$BACKUP/$key" "$path"      # byte-for-byte restore
    else
      rm -rf "$path"                    # was absent -> remove what we created
    fi
  done < "$MANIFEST"
  # one restart of the OLD bot after restoring the unit files
  "${SYSTEMCTL:-systemctl}" daemon-reload || true
  "${SYSTEMCTL:-systemctl}" restart "$SERVICE" || true
}

on_exit() {
  local rc=$?
  if [ "$rc" -eq 0 ] && [ "$MODE" = "execute" ]; then
    STATUS="DEPLOYED"
  elif [ "$ARMED" -eq 1 ] && [ "$STATUS" != "DEPLOYED" ]; then
    rollback && STATUS="ROLLED_BACK" || STATUS="ROLLED_BACK"
  fi
  {
    echo "# Deploy transaction report"
    echo
    echo "STATUS: $STATUS"
    echo "MODE: $MODE"
    echo "LIVE_ROOT: $LIVE_ROOT"
    echo "SOURCE: $SOURCE"
    echo "MERGED_SHA: ${MERGED_SHA:-<none>}"
    echo "BACKUP: ${BACKUP:-<none>}"
    echo "EXIT_RC: $rc"
  } > "$REPORT"
  log "STATUS=$STATUS report=$REPORT"
}
trap on_exit EXIT

# ── read-only gates (both modes) ────────────────────────────────────────────
gate_preflight() {
  command -v "$PYTHON" >/dev/null 2>&1 || die "python missing"
  # syntax gates on the SOURCE (immutable), never the dirty main checkout
  "$PYTHON" -m py_compile "$SOURCE/bot.py" "$SOURCE/coordination.py" \
     "$SOURCE/resource_footer.py" "$SOURCE"/engines/*.py \
     || die "py_compile failed on source"
  for s in "$SOURCE"/scripts/*.sh; do bash -n "$s" || die "bash -n failed: $s"; done
  log "source syntax gates OK"
  # single flock — no second deployer
  exec 9>"$LOCK"
  flock -n 9 || die "another deploy holds the lock ($LOCK)"
}

gate_execute() {
  [ "$REVIEWED_PR" = "19" ] || die "reviewed PR must be 19 (got '${REVIEWED_PR:-}')"
  echo "$MERGED_SHA" | grep -Eq '^[0-9a-f]{40}$' || die "merged SHA must be 40 hex chars"
  local src_head origin_head
  src_head="$(${GIT:-git} -C "$SOURCE" rev-parse HEAD 2>/dev/null || echo x)"
  [ "$src_head" = "$MERGED_SHA" ] || die "source HEAD ($src_head) != merged SHA"
  ${GIT:-git} -C "$SOURCE" fetch --quiet origin 2>/dev/null || true
  origin_head="$(${GIT:-git} -C "$SOURCE" rev-parse origin/main 2>/dev/null || echo y)"
  [ "$origin_head" = "$MERGED_SHA" ] || die "origin/main ($origin_head) != merged SHA (not merged?)"
  log "execute gate OK: PR #$REVIEWED_PR @ $MERGED_SHA"
}

# ── byte-preserving allow-listed dotenv editor (never source/eval) ──────────
set_env_key() {
  local key="$1" val="$2" f="$ENV_FILE" tmp
  case " $ALLOWLIST " in *" $key "*) : ;; *) die "env key '$key' not in allow-list";; esac
  case "$val" in *$'\n'*) die "env value for '$key' contains newline";; esac
  [ -f "$f" ] || die "env file missing: $f"
  tmp="$f.deploytmp.$$"
  # Replace an existing "KEY=..." line in place; else append. All OTHER lines are
  # copied byte-for-byte (awk prints the untouched original for non-matches).
  if grep -Eq "^${key}=" "$f"; then
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="}
      $1==k && index($0,"=")>0 {print k "=" v; next} {print}' "$f" > "$tmp"
  else
    cp -a "$f" "$tmp"; printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  cat "$tmp" > "$f"          # preserve inode/owner/mode of the original .env
  rm -f "$tmp"
}

# ── mutation stages ─────────────────────────────────────────────────────────
install_artifacts() {
  fail_at install_artifacts && die "forced failure: install_artifacts"
  local line dest src
  mkdir -p "$LIVE_SCRIPTS" "$SYSTEMD_DIR"
  while IFS= read -r line; do
    dest="${line%%|*}"; src="${line#*|}"
    if [ -d "$src" ]; then
      rm -rf "$dest"; cp -a "$src" "$dest"
    else
      [ -f "$src" ] || die "source artifact missing: $src"
      install -D -m 0644 "$src" "$dest"
    fi
    [ -e "$dest" ] || die "install failed: $dest"
  done < <(artifacts)
  chmod 0755 "$LIVE_SCRIPTS/cleanup-reports.sh" "$LIVE_SCRIPTS/coord_publish.py" 2>/dev/null || true
  log "artifacts installed"
}

migrate_retention() {
  fail_at migrate_retention && die "forced failure: migrate_retention"
  # remove ONLY the two conflicting report cron files (recorded in manifest)
  rm -f "$CRON_DIR/cleanup-reports" "$CRON_DIR/nightwatch-reports-cleanup"
  mkdir -p "$CONFIGS"
  printf 'BOT_REPORT_RETENTION_DAYS=15\n' > "$CONFIGS/reports-cleanup.env"
  chmod 0644 "$CONFIGS/reports-cleanup.env"
  # retention DRY-RUN only — never a real deletion during the transaction
  if [ -x "$LIVE_SCRIPTS/cleanup-reports.sh" ]; then
    BOT_DATA_ROOT="$LIVE_ROOT" "$LIVE_SCRIPTS/cleanup-reports.sh" --dry-run \
        ${TEST_ROOT:+--test-root "$LIVE_ROOT/reports"} >/dev/null 2>&1 || true
  fi
  log "retention migrated (dry-run only, no deletion)"
}

edit_env() {
  fail_at edit_env && die "forced failure: edit_env"
  set_env_key BOT_MAX_SESSIONS 9
  set_env_key BOT_MAX_RUNNING_AGENTS 2
  log "env allow-list applied"
}

import_smoke() {
  fail_at import_smoke && die "forced failure: import_smoke"
  # Real PTB construction/footer path against the LIVE dir on sys.path. Offline:
  # builds a FooterBot + Application with a fake token (no network) and asserts
  # the footer wiring is callable. This is what previously blew up at startup.
  PYTHONPATH="$LIVE_SCRIPTS" "$PYTHON" - <<'PY' || die "live-dir import/construction smoke failed"
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location(
    "livebot", os.path.join(os.environ["PYTHONPATH"], "claude-telegram-bot.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from telegram.ext import Application, ExtBot
from telegram.request import HTTPXRequest
bot = m.FooterBot("123456:AAHfake-offline-token_xxxxxxxxxxxxxxxxxx",
                  request=HTTPXRequest(), get_updates_request=HTTPXRequest(connection_pool_size=1))
app = Application.builder().bot(bot).build()
assert isinstance(app.bot, ExtBot) and isinstance(app.bot, m.FooterBot)
assert callable(app.bot.send_message) and callable(app.bot.edit_message_text)
print("import-smoke-ok")
PY
  log "live-dir import + construction smoke OK"
}

restart_once() {
  fail_at restart_once && die "forced failure: restart_once"
  "${SYSTEMCTL:-systemctl}" daemon-reload || die "daemon-reload failed"
  "${SYSTEMCTL:-systemctl}" restart "$SERVICE" || die "restart failed"
  log "service restarted exactly once"
}

verify_live() {
  fail_at verify_live && die "forced failure: verify_live"
  "${SYSTEMCTL:-systemctl}" is-active --quiet "$SERVICE" || die "service not active after restart"
  log "post-restart verification OK"
}

# ── main ────────────────────────────────────────────────────────────────────
gate_preflight
if [ "$MODE" = "preflight" ]; then
  log "PREFLIGHT complete — read-only, no mutation performed"
  exit 0
fi

gate_execute
build_manifest
ARMED=1                       # trap will now roll back on any failure

install_artifacts
migrate_retention
edit_env
import_smoke
restart_once
verify_live

exit 0
