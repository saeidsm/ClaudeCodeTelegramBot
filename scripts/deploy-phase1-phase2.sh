#!/usr/bin/env bash
#
# deploy-phase1-phase2.sh — ONE transactional, manifest-driven installer for the
# combined Phase-1 + Phase-2 bot deployment. It IS the runbook
# (docs/PHASE2_DEPLOY_RUNBOOK.md) in executable form, so deployment is an exact,
# tested procedure instead of Claude inventing commands at deploy time.
#
# Default is read-only. Mutation is gated behind the reviewed PR + merged SHA and
# an explicit --execute, runs under one flock from an immutable DETACHED source at
# the merged SHA, records a full PRESENT_BEFORE manifest (state/type/owner/group/
# mode/hash), edits .env only through a byte-preserving allow-list (never
# source/eval), migrates all three conflicting retention mechanisms, installs from
# source, runs a live-dir import + FooterBot/Application construction smoke,
# restarts EXACTLY once, enables the retention timer only AFTER bot health, and
# on any post-mutation failure a trap restores/removes every artifact and prior
# timer state by manifest and restarts the old bot.
#
# Usage:
#   deploy-phase1-phase2.sh --preflight
#   deploy-phase1-phase2.sh --execute --reviewed-pr 19 --merged-sha <40-hex> --source <detached-worktree>
#
# Final report (exactly one) -> $DEPLOY_REPORT:
#   DEPLOYED | ROLLED_BACK | STOPPED_BEFORE_MUTATION
#
# Test seams (accepted ONLY together with a --test-root beneath /tmp; a production
# run — no --test-root — refuses if ANY is set): GIT SYSTEMCTL JOURNALCTL
# DEPLOY_CRON_DIR DEPLOY_SYSTEMD_DIR DEPLOY_BACKUP_ROOT DEPLOY_LOCK DEPLOY_PYTHON
# DEPLOY_PYTEST DEPLOY_STATE_FILE DEPLOY_NIGHTLY_CLEANUP DEPLOY_CLAUDE_CMD
# DEPLOY_CODEX_CMD DEPLOY_LISTENER_CMD DEPLOY_CAPACITY_CMD DEPLOY_SERVICE_USER
# DEPLOY_OBSERVE_SECONDS DEPLOY_FAIL_AT DEPLOY_EXPECTED_TESTS DEPLOY_REPORT.
set -euo pipefail

PROD_ROOT="/opt/shahrzad-devops"
SERVICE="claude-telegram-bot"

MODE="preflight"
REVIEWED_PR=""
MERGED_SHA=""
TEST_ROOT=""
SOURCE="${DEPLOY_SOURCE:-}"

# Allow-listed, NON-SECRET env keys. Anything else (TELEGRAM_BOT_TOKEN,
# OPENROUTER_API_KEY, ANTHROPIC_*, any *_KEY/*_TOKEN/*_SECRET) is refused.
ALLOWLIST=" BOT_MAX_SESSIONS BOT_MAX_RUNNING_AGENTS BOT_NIGHTWATCH_IPC_BIND2 \
BOT_CHAT_SESSIONS_DIR BOT_COORDINATION_FILE BOT_CHAT_CATALOG_TTL \
BOT_CHAT_CONCURRENCY BOT_CHAT_HISTORY_MAX_TURNS BOT_CHAT_HISTORY_MAX_CHARS \
BOT_CODEX_DEFAULT_MODEL BOT_CODEX_SANDBOX BOT_CLAUDE_MODELS BOT_CODEX_MODELS \
BOT_REPORT_RETENTION_DAYS BOT_COORD_PUBLISHER "

TEST_SEAM_VARS="GIT SYSTEMCTL JOURNALCTL DEPLOY_CRON_DIR DEPLOY_SYSTEMD_DIR \
DEPLOY_BACKUP_ROOT DEPLOY_LOCK DEPLOY_PYTHON DEPLOY_PYTEST DEPLOY_STATE_FILE \
DEPLOY_NIGHTLY_CLEANUP DEPLOY_CLAUDE_CMD DEPLOY_CODEX_CMD DEPLOY_LISTENER_CMD \
DEPLOY_CAPACITY_CMD DEPLOY_SERVICE_USER DEPLOY_OBSERVE_SECONDS DEPLOY_FAIL_AT \
DEPLOY_EXPECTED_TESTS"

log()  { echo "[deploy] $*" >&2; }
die()  { echo "[deploy] NO-GO: $*" >&2; exit 3; }
fail_at() { [ "${DEPLOY_FAIL_AT:-}" = "$1" ] && { log "fault-injection at stage '$1'"; return 0; } || return 1; }

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

# ── resolve live root; production refuses test seams ────────────────────────
if [ -n "$TEST_ROOT" ]; then
  case "$(readlink -f "$TEST_ROOT" 2>/dev/null || echo "$TEST_ROOT")" in
    /tmp/*) : ;;
    *) die "--test-root must resolve under /tmp";;
  esac
  LIVE_ROOT="$TEST_ROOT"
else
  LIVE_ROOT="$PROD_ROOT"
  for v in $TEST_SEAM_VARS; do
    [ -n "${!v:-}" ] && die "production mode refuses test seam $v (use --test-root under /tmp)"
  done
fi

LIVE_SCRIPTS="$LIVE_ROOT/scripts"
LIVE_ENGINES="$LIVE_SCRIPTS/engines"
ENV_FILE="$LIVE_ROOT/.env"
CONFIGS="$LIVE_ROOT/configs"
STATE_FILE="${DEPLOY_STATE_FILE:-$CONFIGS/bot-state.json}"
CHAT_DIR="$LIVE_ROOT/chat-sessions"
COORD_FILE="$CONFIGS/coordination.json"
CATALOG_CACHE="$CONFIGS/openrouter-catalog.json"
CRON_DIR="${DEPLOY_CRON_DIR:-/etc/cron.d}"
SYSTEMD_DIR="${DEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
NIGHTLY="${DEPLOY_NIGHTLY_CLEANUP:-$LIVE_SCRIPTS/nightly-cleanup.sh}"
BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-$LIVE_ROOT/backups}"
LOCK="${DEPLOY_LOCK:-$LIVE_ROOT/.deploy.lock}"
PYTHON="${DEPLOY_PYTHON:-python3}"
GITCMD="${GIT:-git}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
JOURNALCTL="${JOURNALCTL:-journalctl}"
REPORT="${DEPLOY_REPORT:-/tmp/phase12-deploy-report.md}"
OBSERVE="${DEPLOY_OBSERVE_SECONDS:-90}"
EXPECTED_TESTS="${DEPLOY_EXPECTED_TESTS:-}"
SERVICE_USER="${DEPLOY_SERVICE_USER:-}"

case "$REPORT" in /tmp/*) : ;; *) [ -n "$TEST_ROOT" ] || die "report must be under /tmp";; esac

[ -n "$SOURCE" ] || SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$SOURCE" ] || die "source dir not found: $SOURCE"

# Scratch dir for preflight — strictly under /tmp, never the source/live tree.
SCRATCH="$(mktemp -d /tmp/deploy-scratch.XXXXXX)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$SCRATCH/pycache"

STATUS="STOPPED_BEFORE_MUTATION"
ARMED=0; RESTARTED_FWD=0
MANIFEST=""; BACKUP=""; OLD_PID=""; TIMER_WAS_ENABLED=""; TIMER_WAS_ACTIVE=""

# ── artifact table: "<abs dest>|<abs source>|<'file'|'dir'>" ────────────────
artifacts() {
  echo "$LIVE_SCRIPTS/claude-telegram-bot.py|$SOURCE/bot.py|file"
  echo "$LIVE_SCRIPTS/coordination.py|$SOURCE/coordination.py|file"
  echo "$LIVE_SCRIPTS/resource_footer.py|$SOURCE/resource_footer.py|file"
  echo "$LIVE_SCRIPTS/log_filters.py|$SOURCE/log_filters.py|file"
  echo "$LIVE_SCRIPTS/coord_publish.py|$SOURCE/scripts/coord_publish.py|file"
  echo "$LIVE_ENGINES|$SOURCE/engines|dir"
  echo "$LIVE_SCRIPTS/video_module|$SOURCE/video_module|dir"
  echo "$LIVE_SCRIPTS/cleanup-reports.sh|$SOURCE/scripts/cleanup-reports.sh|file"
  echo "$SYSTEMD_DIR/reports-cleanup.service|$SOURCE/systemd/reports-cleanup.service|file"
  echo "$SYSTEMD_DIR/reports-cleanup.timer|$SOURCE/systemd/reports-cleanup.timer|file"
}
# Paths that are only recorded/backed-up (not installed from source): live state
# and data that startup may mutate, the conflicting crons, the nightly script,
# the canonical retention config, and the live .env.
backup_only() {
  echo "$ENV_FILE"
  echo "$STATE_FILE"
  echo "$COORD_FILE"
  echo "$COORD_FILE.lock"
  echo "$CATALOG_CACHE"
  echo "$CHAT_DIR"
  echo "$CONFIGS/reports-cleanup.env"
  echo "$CRON_DIR/cleanup-reports"
  echo "$CRON_DIR/nightwatch-reports-cleanup"
  echo "$NIGHTLY"
}

sha() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "-"; }

# ── manifest / backup (state,type,owner,group,mode,hash) ────────────────────
record() {
  local p="$1" key state typ owner group mode hash
  key="$(printf '%s' "$p" | sha256sum | awk '{print $1}')"
  if [ -e "$p" ] || [ -L "$p" ]; then
    state="present"
    if   [ -L "$p" ]; then typ="link"
    elif [ -d "$p" ]; then typ="dir"
    else typ="file"; fi
    owner="$(stat -c '%u' "$p" 2>/dev/null || echo -)"
    group="$(stat -c '%g' "$p" 2>/dev/null || echo -)"
    mode="$(stat -c '%a' "$p" 2>/dev/null || echo -)"
    hash="$([ -f "$p" ] && sha "$p" || echo -)"
    cp -a "$p" "$BACKUP/$key"
  else
    state="absent"; typ="-"; owner="-"; group="-"; mode="-"; hash="-"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$state" "$typ" "$owner" "$group" "$mode" "$hash" "$key" "$p" >> "$MANIFEST"
}

build_manifest() {
  BACKUP="$BACKUP_ROOT/deploy-$(cat "$SCRATCH/stamp")-$$"
  mkdir -p "$BACKUP"
  MANIFEST="$BACKUP/MANIFEST.tsv"; : > "$MANIFEST"
  local line dest
  while IFS= read -r line; do dest="${line%%|*}"; record "$dest"; done < <(artifacts)
  while IFS= read -r dest; do record "$dest"; done < <(backup_only)
  # capture prior timer enabled/active state (first stdout line only; the
  # commands exit non-zero when disabled/inactive but still print the word)
  TIMER_WAS_ENABLED="$({ $SYSTEMCTL is-enabled reports-cleanup.timer 2>/dev/null || true; } | head -1)"
  TIMER_WAS_ACTIVE="$({ $SYSTEMCTL is-active  reports-cleanup.timer 2>/dev/null || true; } | head -1)"
  [ -n "$TIMER_WAS_ENABLED" ] || TIMER_WAS_ENABLED="disabled"
  [ -n "$TIMER_WAS_ACTIVE" ]  || TIMER_WAS_ACTIVE="inactive"
  printf 'timer_enabled=%s\ntimer_active=%s\n' "$TIMER_WAS_ENABLED" "$TIMER_WAS_ACTIVE" \
    > "$BACKUP/TIMER_STATE"       # audit copy only; rollback uses the globals below
  log "manifest built (timer was enabled=$TIMER_WAS_ENABLED active=$TIMER_WAS_ACTIVE)"
}

rollback() {
  log "ROLLING BACK from $MANIFEST"
  local state typ owner group mode hash key path
  while IFS=$'\t' read -r state typ owner group mode hash key path; do
    [ -n "$path" ] || continue
    if [ "$state" = "present" ]; then
      rm -rf -- "$path"
      cp -a "$BACKUP/$key" "$path"        # content + metadata (cp -a) byte-for-byte
    else
      rm -rf -- "$path"                    # absent-before -> remove what we created
    fi
  done < "$MANIFEST"
  # restore timer enabled/active to the captured prior state (globals, not a
  # sourced file — sourcing under set -e in the trap is fragile)
  if [ "$TIMER_WAS_ENABLED" = "enabled" ]; then
    $SYSTEMCTL enable reports-cleanup.timer >/dev/null 2>&1 || true
  else
    $SYSTEMCTL disable reports-cleanup.timer >/dev/null 2>&1 || true
  fi
  if [ "$TIMER_WAS_ACTIVE" = "active" ]; then
    $SYSTEMCTL start reports-cleanup.timer >/dev/null 2>&1 || true
  else
    $SYSTEMCTL stop reports-cleanup.timer >/dev/null 2>&1 || true
  fi
  $SYSTEMCTL daemon-reload >/dev/null 2>&1 || true
  $SYSTEMCTL restart "$SERVICE" >/dev/null 2>&1 || true   # separately-accounted restore restart
}

on_exit() {
  local rc=$?
  set +e                      # the report MUST always be written, even if rollback errors
  if [ "$rc" -eq 0 ] && [ "$MODE" = "execute" ]; then
    STATUS="DEPLOYED"
  elif [ "$ARMED" -eq 1 ] && [ "$STATUS" != "DEPLOYED" ]; then
    rollback; STATUS="ROLLED_BACK"
  fi
  {
    echo "# Deploy transaction report"
    echo "STATUS: $STATUS"
    echo "MODE: $MODE"
    echo "LIVE_ROOT: $LIVE_ROOT"
    echo "SOURCE: $SOURCE"
    echo "MERGED_SHA: ${MERGED_SHA:-<none>}"
    echo "BACKUP: ${BACKUP:-<none>}"
    echo "OLD_PID: ${OLD_PID:-<none>}"
    echo "FORWARD_RESTARTS: $RESTARTED_FWD"
    echo "TIMER_WAS: enabled=${TIMER_WAS_ENABLED:-?} active=${TIMER_WAS_ACTIVE:-?}"
    echo "EXIT_RC: $rc"
  } > "$REPORT"
  rm -rf -- "$SCRATCH" 2>/dev/null || true
  log "STATUS=$STATUS report=$REPORT"
}
trap on_exit EXIT

# stamp (no Date in-loop dependency for tests; still deterministic per run)
date +%Y%m%d-%H%M%S > "$SCRATCH/stamp"

# ─────────────────────── read-only gates ───────────────────────────────────
gate_source() {
  fail_at gate_source && die "forced: gate_source"
  echo "$MERGED_SHA" | grep -Eq '^[0-9a-f]{40}$' || die "merged SHA must be 40 hex"
  # must be a DETACHED worktree (no branch ref) at the exact SHA, fully clean
  if $GITCMD -C "$SOURCE" symbolic-ref -q HEAD >/dev/null 2>&1; then
    die "source is on a branch; a detached worktree at the merged SHA is required"
  fi
  [ -z "$($GITCMD -C "$SOURCE" status --porcelain 2>/dev/null)" ] \
    || die "source worktree has staged/unstaged/untracked changes"
  local head origin
  head="$($GITCMD -C "$SOURCE" rev-parse HEAD 2>/dev/null || echo x)"
  [ "$head" = "$MERGED_SHA" ] || die "source HEAD ($head) != merged SHA"
  $GITCMD -C "$SOURCE" fetch origin >/dev/null 2>&1 || die "git fetch origin failed (fail-closed)"
  origin="$($GITCMD -C "$SOURCE" rev-parse origin/main 2>/dev/null || echo y)"
  [ "$origin" = "$MERGED_SHA" ] || die "origin/main ($origin) != merged SHA (not merged?)"
  log "source gate OK: detached, clean, HEAD==origin/main==$MERGED_SHA"
}

gate_suite() {
  fail_at gate_suite && die "forced: gate_suite"
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m py_compile \
     "$SOURCE/bot.py" "$SOURCE/coordination.py" "$SOURCE/resource_footer.py" \
     "$SOURCE/log_filters.py" "$SOURCE"/engines/*.py "$SOURCE"/video_module/*.py \
     "$SOURCE/scripts/coord_publish.py" || die "py_compile failed on source"
  for s in "$SOURCE"/scripts/*.sh; do bash -n "$s" || die "bash -n failed: $s"; done
  # complete offline suite from the immutable source, bytecode + caches to /tmp
  local out passed
  out="$( cd "$SOURCE" && PYTHONDONTWRITEBYTECODE=1 \
        ${DEPLOY_PYTEST:-$PYTHON -m pytest} -q -p no:cacheprovider \
        -o cache_dir="$SCRATCH/pytest" --basetemp="$SCRATCH/pt" 2>&1 )" \
    || { echo "$out" | tail -5 >&2; die "source test suite failed"; }
  passed="$(printf '%s' "$out" | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')"
  if [ -n "$EXPECTED_TESTS" ]; then
    [ "$passed" = "$EXPECTED_TESTS" ] || die "suite total $passed != expected $EXPECTED_TESTS"
  fi
  log "source suite OK ($passed passed)"
}

_active_sessions() {
  # count running/queued sessions from the ACTUAL state path, non-executingly
  [ -f "$STATE_FILE" ] || { echo 0; return; }
  "$PYTHON" - "$STATE_FILE" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print(0); sys.exit()
n=0
for s in (d.get("sessions") or {}).values() if isinstance(d.get("sessions"),dict) else (d.get("sessions") or []):
    if isinstance(s,dict) and s.get("status") in ("running","queued"): n+=1
print(n)
PY
}

gate_readiness() {
  fail_at gate_readiness && die "forced: gate_readiness"
  $SYSTEMCTL is-active --quiet "$SERVICE" || die "bot service not active"
  OLD_PID="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ -z "$SERVICE_USER" ] && SERVICE_USER="$($SYSTEMCTL show "$SERVICE" -p User --value 2>/dev/null || echo root)"
  log "service active: PID=$OLD_PID user=$SERVICE_USER"
  # zero running/queued sessions
  local busy; busy="$(_active_sessions)"
  [ "$busy" = "0" ] || die "refusing: $busy running/queued session(s)"
  # capacity / OOM (seam-able command; default: real df/free check)
  if [ -n "${DEPLOY_CAPACITY_CMD:-}" ]; then
    "$DEPLOY_CAPACITY_CMD" || die "capacity/OOM readiness failed"
  fi
  # NightWatch listeners (only when a check is configured)
  if [ -n "${DEPLOY_LISTENER_CMD:-}" ]; then
    "$DEPLOY_LISTENER_CMD" || die "NightWatch listener(s) unhealthy"
  fi
  # .env parses non-executingly and has a nonempty OpenRouter key (never printed)
  [ -f "$ENV_FILE" ] || die ".env missing"
  "$PYTHON" - "$ENV_FILE" <<'PY' || die ".env parse / OpenRouter key check failed"
import sys
from dotenv import dotenv_values
v=dotenv_values(sys.argv[1])
sys.exit(0 if (v.get("OPENROUTER_API_KEY") or "").strip() else 1)
PY
  # service-user CLI + import readiness
  "${DEPLOY_CLAUDE_CMD:-claude}" models >/dev/null 2>&1 || die "Claude CLI unavailable"
  "${DEPLOY_CODEX_CMD:-codex}" login status >/dev/null 2>&1 || die "Codex login status failed"
  "${DEPLOY_CODEX_CMD:-codex}" debug models >/dev/null 2>&1 || die "Codex model contract failed"
  PYTHONPATH="$SOURCE" "$PYTHON" -c "import telegram, aiohttp, dotenv, engines.base, coordination, resource_footer" \
     || die "runtime import readiness failed (PTB/aiohttp/dotenv/modules)"
  # report cleanup dry-run (no || true) — capture target/counts
  local dry
  dry="$( BOT_DATA_ROOT="$LIVE_ROOT" BOT_CONFIGS_DIR="$CONFIGS" "$SOURCE/scripts/cleanup-reports.sh" --dry-run \
          ${TEST_ROOT:+--test-root "$LIVE_ROOT/reports"} 2>&1 )" \
     || die "report cleanup dry-run failed"
  log "cleanup dry-run OK"
  # optional tools: report only, never modify/claim integration
  command -v basic-memory >/dev/null 2>&1 && log "basic-memory: present (deploy-gated, NOT integrated)" \
     || log "basic-memory: ABSENT — follow-up (optional, deploy-gated)"
  command -v graphify >/dev/null 2>&1 && log "graphify: present (deploy-gated, NOT integrated)" \
     || log "graphify: ABSENT — follow-up (optional, deploy-gated)"
  log "readiness gates OK"
}

# ─────────────────────── byte-preserving dotenv editor ─────────────────────
set_env_key() {
  local key="$1" val="$2" f="$ENV_FILE" tmp
  case " $ALLOWLIST " in *" $key "*) : ;; *) die "env key '$key' not allow-listed";; esac
  case "$val" in *$'\n'*) die "env value for '$key' has newline";; esac
  [ -f "$f" ] || die "env file missing: $f"
  tmp="$f.deploytmp.$$"
  if grep -Eq "^${key}=" "$f"; then
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="}
      $1==k && index($0,"=")>0 {print k "=" v; next} {print}' "$f" > "$tmp"
  else
    cp -a "$f" "$tmp"; printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  cat "$tmp" > "$f"; rm -f "$tmp"      # preserve inode/owner/mode of .env
}

# ─────────────────────── mutation stages ───────────────────────────────────
install_artifacts() {
  fail_at install_artifacts && die "forced: install_artifacts"
  mkdir -p "$LIVE_SCRIPTS" "$SYSTEMD_DIR" "$CONFIGS"
  local line dest src typ
  while IFS= read -r line; do
    dest="${line%%|*}"; src="${line#*|}"; typ="${src##*|}"; src="${src%|*}"
    if [ "$typ" = "dir" ]; then
      [ -d "$src" ] || die "source dir missing: $src"
      rm -rf -- "$dest"; cp -a "$src" "$dest"
    else
      [ -f "$src" ] || die "source file missing: $src"
      install -D -m 0644 "$src" "$dest"
      [ "$(sha "$dest")" = "$(sha "$src")" ] || die "hash mismatch after install: $dest"
    fi
    [ -e "$dest" ] || die "install failed: $dest"
  done < <(artifacts)
  chmod 0755 "$LIVE_SCRIPTS/cleanup-reports.sh" "$LIVE_SCRIPTS/coord_publish.py" 2>/dev/null || true
  # least-privilege runtime dirs (service-user owned when known)
  mkdir -p "$CHAT_DIR" "$(dirname "$COORD_FILE")"
  chmod 0700 "$CHAT_DIR" 2>/dev/null || true
  log "artifacts installed + hash-verified"
}

edit_env() {
  fail_at edit_env && die "forced: edit_env"
  set_env_key BOT_MAX_SESSIONS 9
  set_env_key BOT_MAX_RUNNING_AGENTS 2
  set_env_key BOT_NIGHTWATCH_IPC_BIND2 10.108.0.4
  # safe explicit Phase-2 non-secret paths + absolute coordination publisher
  set_env_key BOT_CHAT_SESSIONS_DIR "$CHAT_DIR"
  set_env_key BOT_COORDINATION_FILE "$COORD_FILE"
  set_env_key BOT_COORD_PUBLISHER "$LIVE_SCRIPTS/coord_publish.py"
  log "env allow-list applied (9/2/BIND2 + phase-2 paths)"
}

migrate_retention() {
  fail_at migrate_retention && die "forced: migrate_retention"
  # (1)+(2) remove the two conflicting report cron files
  rm -f -- "$CRON_DIR/cleanup-reports" "$CRON_DIR/nightwatch-reports-cleanup"
  # (3) surgical, deterministic removal of ONLY report-deletion commands from
  # nightly-cleanup.sh — comment them out, preserving every other byte. Fail
  # closed if the result is ambiguous (an active report-deletion line remains).
  if [ -f "$NIGHTLY" ]; then
    local tmp="$NIGHTLY.deploytmp.$$"
    # AMBIGUITY GUARD (fail closed): a report-deletion that shares its line with
    # another command (&&, ||, ;) or a line-continuation cannot be surgically
    # removed without risking unrelated commands — abort rather than guess.
    if awk '
        function isdel(l){ return (l ~ /(-delete|-exec[ \t]+rm|rm[ \t]+-rf)/) && (l ~ /reports/) && (l !~ /^[ \t]*#/) }
        isdel($0) && ($0 ~ /(&&|\|\||;|\\[ \t]*$)/) { found=1 }
        END { exit(found?0:1) }
      ' "$NIGHTLY"; then
      die "nightly-cleanup report-deletion is entangled with other commands; aborting (ambiguous)"
    fi
    # Comment out ONLY standalone report-deletion lines; every other byte kept.
    awk '
      function isdel(l){ return (l ~ /(-delete|-exec[ \t]+rm|rm[ \t]+-rf)/) && (l ~ /reports/) && (l !~ /^[ \t]*#/) }
      isdel($0) { print "# [phase1-2 retention migration: removed report-deletion] " $0; next }
      { print }
    ' "$NIGHTLY" > "$tmp"
    # no ACTIVE report-deletion command may remain
    if grep -nE '(-delete|-exec[ \t]+rm|rm[ \t]+-rf)' "$tmp" | grep -E 'reports' | grep -vqE '^[0-9]+:[ \t]*#'; then
      rm -f "$tmp"; die "nightly-cleanup still has an active report-deletion after migration"
    fi
    cat "$tmp" > "$NIGHTLY"; rm -f "$tmp"
  fi
  # install canonical retention config; require its dry-run; NEVER real cleanup
  printf 'BOT_REPORT_RETENTION_DAYS=15\n' > "$CONFIGS/reports-cleanup.env"
  chmod 0644 "$CONFIGS/reports-cleanup.env"
  BOT_DATA_ROOT="$LIVE_ROOT" BOT_CONFIGS_DIR="$CONFIGS" "$LIVE_SCRIPTS/cleanup-reports.sh" --dry-run \
      ${TEST_ROOT:+--test-root "$LIVE_ROOT/reports"} >/dev/null 2>&1 \
      || die "canonical cleanup dry-run failed"
  # prove no conflicting report-deletion command remains anywhere we migrated
  if [ -f "$NIGHTLY" ] && grep -nE '(-delete|-exec[ \t]+rm|rm[ \t]+-rf)' "$NIGHTLY" | grep -E 'reports' | grep -vqE '^[0-9]+:[ \t]*#'; then
    die "post-migration: a report-deletion command still active in nightly-cleanup"
  fi
  [ -e "$CRON_DIR/cleanup-reports" ] && die "cron cleanup-reports not removed"
  [ -e "$CRON_DIR/nightwatch-reports-cleanup" ] && die "cron nightwatch not removed"
  log "retention migrated (2 crons + nightly surgical); canonical dry-run OK"
}

verify_install() {
  fail_at verify_install && die "forced: verify_install"
  # live-dir compile/import closure + real offline FooterBot/Application build
  PYTHONPATH="$LIVE_SCRIPTS" PYTHONDONTWRITEBYTECODE=1 "$PYTHON" - <<'PY' || die "live-dir import/construction smoke failed"
import importlib.util, os
d=os.environ["PYTHONPATH"]
spec=importlib.util.spec_from_file_location("livebot", os.path.join(d,"claude-telegram-bot.py"))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from telegram.ext import Application, ExtBot
from telegram.request import HTTPXRequest
b=m.FooterBot("123456:AAHfake-offline-token_xxxxxxxxxxxxxxxxxx",
              request=HTTPXRequest(), get_updates_request=HTTPXRequest(connection_pool_size=1))
app=Application.builder().bot(b).build()
assert isinstance(app.bot, ExtBot) and isinstance(app.bot, m.FooterBot)
assert callable(app.bot.send_message) and callable(app.bot.edit_message_text)
print("live-smoke-ok")
PY
  log "live-dir import + FooterBot/Application smoke OK"
}

restart_once() {
  fail_at restart_once && die "forced: restart_once"
  # re-run the busy-session gate IMMEDIATELY before restart
  [ "$(_active_sessions)" = "0" ] || die "busy sessions appeared before restart"
  $SYSTEMCTL daemon-reload || die "daemon-reload failed"
  $SYSTEMCTL restart "$SERVICE" || die "restart failed"
  RESTARTED_FWD=$((RESTARTED_FWD+1))
  log "service restarted (forward count=$RESTARTED_FWD)"
}

health_check() {
  fail_at health_check && die "forced: health_check"
  $SYSTEMCTL is-active --quiet "$SERVICE" || die "service not active after restart"
  local newpid; newpid="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ -n "$newpid" ] && [ "$newpid" != "$OLD_PID" ] || die "PID did not change after restart"
  local jrl; jrl="$($JOURNALCTL -u "$SERVICE" -n 200 --no-pager 2>/dev/null || echo '')"
  echo "$jrl" | grep -qiE 'traceback|fatal' && die "fatal traceback in journal"
  echo "$jrl" | grep -q 'Bot v4 ready' || die "'Bot v4 ready' not seen"
  echo "$jrl" | grep -Eq 'max_sessions=9' && echo "$jrl" | grep -Eq 'max_running_agents=2' \
     || die "effective limits not 9/2"
  echo "$jrl" | grep -Eq 'engines.*(claude).*codex.*chat|registered 3 engines' \
     || die "three engines not registered"
  [ -n "${DEPLOY_LISTENER_CMD:-}" ] && { "$DEPLOY_LISTENER_CMD" || die "listener(s) unhealthy post-restart"; }
  [ -w "$(dirname "$COORD_FILE")" ] || die "coordination path not writable"
  [ -w "$CHAT_DIR" ] || die "chat path not writable"
  log "health OK: pid $OLD_PID -> $newpid, Bot v4 ready, 9/2, engines, paths"
}

enable_timer() {
  fail_at enable_timer && die "forced: enable_timer"
  # only AFTER bot health
  $SYSTEMCTL enable reports-cleanup.timer >/dev/null 2>&1 || die "timer enable failed"
  $SYSTEMCTL start  reports-cleanup.timer >/dev/null 2>&1 || die "timer start failed"
  $SYSTEMCTL is-enabled --quiet reports-cleanup.timer || die "timer not enabled"
  $SYSTEMCTL is-active  --quiet reports-cleanup.timer || die "timer not active"
  # verify the unit resolves the stable live cleanup path
  local ex; ex="$($SYSTEMCTL show reports-cleanup.service -p ExecStart --value 2>/dev/null || echo '')"
  case "$ex" in *"$LIVE_SCRIPTS/cleanup-reports.sh"*|"") : ;; *) die "timer ExecStart unexpected: $ex";; esac
  log "retention timer enabled+active after health"
}

observe() {
  fail_at observe && die "forced: observe"
  [ "$OBSERVE" -gt 0 ] && sleep "$OBSERVE"
  # final recheck
  $SYSTEMCTL is-active --quiet "$SERVICE" || die "service died during observation"
  $SYSTEMCTL is-active --quiet reports-cleanup.timer || die "timer inactive after observation"
  log "observation window ($OBSERVE s) passed"
}

# ─────────────────────── main ──────────────────────────────────────────────
gate_source
gate_suite
gate_readiness

if [ "$MODE" = "preflight" ]; then
  log "PREFLIGHT complete — read-only, no lock, no mutation, scratch under /tmp"
  exit 0
fi

[ "$REVIEWED_PR" = "19" ] || die "reviewed PR must be 19"
# acquire the single deploy flock ONLY now (execute, after read-only gates)
exec 9>"$LOCK"; flock -n 9 || die "another deploy holds the lock"

build_manifest
ARMED=1

install_artifacts
edit_env
migrate_retention
verify_install
restart_once
health_check
enable_timer
observe

exit 0
