#!/usr/bin/env bash
#
# deploy-phase1-phase2.sh — ONE transactional, manifest-driven installer for the
# combined Phase-1 + Phase-2 bot deployment. It IS the runbook
# (docs/PHASE2_DEPLOY_RUNBOOK.md) in executable form.
#
# Read-only by default. Mutation is gated behind the reviewed PR + merged SHA and
# an explicit --execute, runs under one flock from an immutable DETACHED source at
# the merged SHA, records a full PRESENT_BEFORE manifest, edits .env only through a
# binary-safe byte-preserving allow-list, migrates all three retention mechanisms,
# installs from source, verifies, restarts EXACTLY once, activates the retention
# timer only at the final commit boundary, and on any post-mutation failure a trap
# performs an ordered, VERIFIED rollback. Terminal status is exactly one of:
#   DEPLOYED | ROLLED_BACK | ROLLBACK_FAILED | STOPPED_BEFORE_MUTATION
#
# Preflight writes ONLY under /tmp: it never mutates Git metadata (read-only
# ls-remote, GIT_OPTIONAL_LOCKS=0), takes no lock, and leaves no source caches.
#
# Test seams (honoured ONLY with a --test-root beneath /tmp; a production run — no
# --test-root — refuses if ANY is set): GIT SYSTEMCTL JOURNALCTL DEPLOY_CRON_DIR
# DEPLOY_SYSTEMD_DIR DEPLOY_BACKUP_ROOT DEPLOY_LOCK DEPLOY_PYTHON DEPLOY_PYTEST
# DEPLOY_STATE_FILE DEPLOY_NIGHTLY_CLEANUP DEPLOY_CLAUDE_CMD DEPLOY_CODEX_CMD
# DEPLOY_LISTENER_CMD DEPLOY_CAPACITY_CMD DEPLOY_OPENROUTER_CMD DEPLOY_CGROUP_BASE
# DEPLOY_SERVICE_USER DEPLOY_OBSERVE_SECONDS DEPLOY_FAIL_AT DEPLOY_EXPECTED_TESTS.
set -euo pipefail

PROD_ROOT="/opt/shahrzad-devops"
SERVICE="claude-telegram-bot"

MODE="preflight"; REVIEWED_PR=""; MERGED_SHA=""; TEST_ROOT=""
SOURCE="${DEPLOY_SOURCE:-}"

ALLOWLIST=" BOT_MAX_SESSIONS BOT_MAX_RUNNING_AGENTS BOT_NIGHTWATCH_IPC_BIND2 \
BOT_CHAT_SESSIONS_DIR BOT_COORDINATION_FILE BOT_CHAT_CATALOG_TTL \
BOT_CHAT_CONCURRENCY BOT_CHAT_HISTORY_MAX_TURNS BOT_CHAT_HISTORY_MAX_CHARS \
BOT_CODEX_DEFAULT_MODEL BOT_CODEX_SANDBOX BOT_CLAUDE_MODELS BOT_CODEX_MODELS \
BOT_REPORT_RETENTION_DAYS BOT_COORD_PUBLISHER BOT_CHAT_CATALOG_CACHE "

TEST_SEAM_VARS="GIT SYSTEMCTL JOURNALCTL DEPLOY_CRON_DIR DEPLOY_SYSTEMD_DIR \
DEPLOY_BACKUP_ROOT DEPLOY_LOCK DEPLOY_PYTHON DEPLOY_PYTEST DEPLOY_STATE_FILE \
DEPLOY_NIGHTLY_CLEANUP DEPLOY_CLAUDE_CMD DEPLOY_CODEX_CMD DEPLOY_LISTENER_CMD \
DEPLOY_CAPACITY_CMD DEPLOY_OPENROUTER_CMD DEPLOY_CGROUP_BASE DEPLOY_SERVICE_USER \
DEPLOY_OBSERVE_SECONDS DEPLOY_FAIL_AT DEPLOY_EXPECTED_TESTS"

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

if [ -n "$TEST_ROOT" ]; then
  case "$(readlink -f "$TEST_ROOT" 2>/dev/null || echo "$TEST_ROOT")" in
    /tmp/*) : ;; *) die "--test-root must resolve under /tmp";;
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
CONFIGS="$LIVE_ROOT/configs"          # hardcoded default; overridable resolved paths validated to stay inside LIVE_ROOT
CRON_DIR="${DEPLOY_CRON_DIR:-/etc/cron.d}"
SYSTEMD_DIR="${DEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
NIGHTLY="${DEPLOY_NIGHTLY_CLEANUP:-$LIVE_SCRIPTS/nightly-cleanup.sh}"
BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-$LIVE_ROOT/backups}"
LOCK="${DEPLOY_LOCK:-$LIVE_ROOT/.deploy.lock}"
PYTHON="${DEPLOY_PYTHON:-python3}"
GITCMD="${GIT:-git}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
JOURNALCTL="${JOURNALCTL:-journalctl}"
CGROUP_BASE="${DEPLOY_CGROUP_BASE:-/sys/fs/cgroup}"
REPORT="${DEPLOY_REPORT:-/tmp/phase12-deploy-report.md}"
OBSERVE="${DEPLOY_OBSERVE_SECONDS:-90}"
EXPECTED_TESTS="${DEPLOY_EXPECTED_TESTS:-}"
SERVICE_USER="${DEPLOY_SERVICE_USER:-}"
MIN_DISK_MB="${DEPLOY_MIN_DISK_MB:-500}"
MIN_RAM_MB="${DEPLOY_MIN_RAM_MB:-150}"

case "$REPORT" in /tmp/*) : ;; *) [ -n "$TEST_ROOT" ] || die "report must be under /tmp";; esac
[ -n "$SOURCE" ] || SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$SOURCE" ] || die "source dir not found: $SOURCE"

# Scratch strictly under /tmp — never the source/live tree.
SCRATCH="$(mktemp -d /tmp/deploy-scratch.XXXXXX)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$SCRATCH/pycache"
export GIT_OPTIONAL_LOCKS=0            # git status must never write the index during preflight

STATUS="STOPPED_BEFORE_MUTATION"
ARMED=0; RESTARTED_FWD=0; TIMER_ACTIVATED=0
MANIFEST=""; BACKUP=""; OLD_PID=""; NEW_PID=""; RESTART_CURSOR=""
TIMER_WAS_ENABLED=""; TIMER_WAS_ACTIVE=""
ROLLBACK_ERRORS=""

# Resolved (env-aware) paths — filled by resolve_paths().
R_STATE=""; R_CHAT=""; R_COORD=""; R_COORDLOCK=""; R_CACHE=""; R_TOKEN=""; R_RETENTION_ENV=""

sha() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "-"; }
# deterministic recursive tree hash (path+content), order-independent
tree_hash() {
  [ -d "$1" ] || { echo "-"; return; }
  ( cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum 2>/dev/null ) \
    | sha256sum | awk '{print $1}'
}

# ── non-executing .env parse (dotenv_values) ────────────────────────────────
env_get() {   # env_get KEY  -> prints value (empty if unset); never source/eval
  [ -f "$ENV_FILE" ] || return 0
  "$PYTHON" - "$ENV_FILE" "$1" <<'PY' 2>/dev/null || true
import sys
from dotenv import dotenv_values
v=dotenv_values(sys.argv[1]).get(sys.argv[2])
sys.stdout.write(v if v else "")
PY
}

# validate a resolved path is inside an allowed root and not a symlink-escape
inside_root() {   # inside_root <path> <root>
  local p r
  p="$($PYTHON - "$1" <<'PY'
import os,sys
print(os.path.realpath(os.path.dirname(sys.argv[1])))
PY
)"
  r="$($PYTHON - "$2" <<'PY'
import os,sys
print(os.path.realpath(sys.argv[1]))
PY
)"
  case "$p/" in "$r/"*) return 0;; *) return 1;; esac
}

resolve_paths() {
  # Resolve the EXACT paths the running/old bot uses, from the non-executed live
  # env (defaults mirror bot.py). Everything must stay inside LIVE_ROOT.
  local data configs chat coord cache
  data="$(env_get BOT_DATA_ROOT)";    data="${data:-$LIVE_ROOT}"
  configs="$(env_get BOT_CONFIGS_DIR)"; configs="${configs:-$data/configs}"
  chat="$(env_get BOT_CHAT_SESSIONS_DIR)"; chat="${chat:-$data/chat-sessions}"
  coord="$(env_get BOT_COORDINATION_FILE)"; coord="${coord:-$configs/coordination.json}"
  cache="$(env_get BOT_CHAT_CATALOG_CACHE)"; cache="${cache:-$configs/openrouter-catalog.json}"
  R_STATE="${DEPLOY_STATE_FILE:-$configs/bot-state.json}"
  R_CHAT="$chat"; R_COORD="$coord"; R_COORDLOCK="$coord.lock"
  R_CACHE="$cache"; R_TOKEN="$configs/reports-token.env"
  R_RETENTION_ENV="$configs/reports-cleanup.env"
  CONFIGS="$configs"
  # containment: every resolved path must be inside LIVE_ROOT (fail closed)
  local p
  for p in "$R_STATE" "$R_CHAT" "$R_COORD" "$R_COORDLOCK" "$R_CACHE" "$R_TOKEN" "$R_RETENTION_ENV"; do
    inside_root "$p" "$LIVE_ROOT" || die "resolved path escapes live root: $p"
    [ -L "$p" ] && die "resolved path is a symlink (ambiguous): $p"
  done
  log "resolved live paths: state=$R_STATE chat=$R_CHAT coord=$R_COORD cache=$R_CACHE configs=$CONFIGS"
}

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
backup_only() {   # live data/config the bot ACTUALLY uses (resolved) + retention files
  echo "$ENV_FILE"; echo "$R_STATE"; echo "$R_COORD"; echo "$R_COORDLOCK"
  echo "$R_CACHE"; echo "$R_CHAT"; echo "$R_RETENTION_ENV"
  echo "$CRON_DIR/cleanup-reports"; echo "$CRON_DIR/nightwatch-reports-cleanup"; echo "$NIGHTLY"
}

# ── manifest / backup (state,type,owner,group,mode,hash) ────────────────────
record() {
  local p="$1" key state typ owner group mode hash
  key="$(printf '%s' "$p" | sha256sum | awk '{print $1}')"
  if [ -e "$p" ] || [ -L "$p" ]; then
    state="present"
    if   [ -L "$p" ]; then typ="link"
    elif [ -d "$p" ]; then typ="dir"; hash="$(tree_hash "$p")"
    else typ="file"; hash="$(sha "$p")"; fi
    [ -d "$p" ] || [ "${hash:-}" ] || hash="-"
    owner="$(stat -c '%u' "$p" 2>/dev/null || echo -)"
    group="$(stat -c '%g' "$p" 2>/dev/null || echo -)"
    mode="$(stat -c '%a' "$p" 2>/dev/null || echo -)"
    cp -a "$p" "$BACKUP/$key"
  else
    state="absent"; typ="-"; owner="-"; group="-"; mode="-"; hash="-"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$state" "$typ" "$owner" "$group" "$mode" "${hash:--}" "$key" "$p" >> "$MANIFEST"
}

build_manifest() {
  BACKUP="$BACKUP_ROOT/deploy-$(cat "$SCRATCH/stamp")-$$"
  mkdir -p "$BACKUP"
  MANIFEST="$BACKUP/MANIFEST.tsv"; : > "$MANIFEST"
  local line dest
  while IFS= read -r line; do dest="${line%%|*}"; record "$dest"; done < <(artifacts)
  while IFS= read -r dest; do record "$dest"; done < <(backup_only)
  TIMER_WAS_ENABLED="$({ $SYSTEMCTL is-enabled reports-cleanup.timer 2>/dev/null || true; } | head -1)"
  TIMER_WAS_ACTIVE="$({ $SYSTEMCTL is-active  reports-cleanup.timer 2>/dev/null || true; } | head -1)"
  [ -n "$TIMER_WAS_ENABLED" ] || TIMER_WAS_ENABLED="disabled"
  [ -n "$TIMER_WAS_ACTIVE" ]  || TIMER_WAS_ACTIVE="inactive"
  printf 'timer_enabled=%s\ntimer_active=%s\n' "$TIMER_WAS_ENABLED" "$TIMER_WAS_ACTIVE" > "$BACKUP/TIMER_STATE"
  log "manifest built (timer prior: enabled=$TIMER_WAS_ENABLED active=$TIMER_WAS_ACTIVE)"
}

rb_err() { ROLLBACK_ERRORS="${ROLLBACK_ERRORS}${ROLLBACK_ERRORS:+; }$1"; log "rollback-error: $1"; }

rollback() {
  log "ROLLING BACK from $MANIFEST"
  # 1) stop any newly-activated timer FIRST so no cleanup can run mid-restore
  if [ "$TIMER_ACTIVATED" -eq 1 ]; then
    $SYSTEMCTL stop reports-cleanup.timer >/dev/null 2>&1 || rb_err "stop new timer"
  fi
  # 2) restore files/content/metadata; remove absent-before
  local state typ owner group mode hash key path
  while IFS=$'\t' read -r state typ owner group mode hash key path; do
    [ -n "$path" ] || continue
    if [ "$state" = "present" ]; then
      rm -rf -- "$path" || rb_err "rm before restore $path"
      cp -a "$BACKUP/$key" "$path" || rb_err "restore $path"
    else
      rm -rf -- "$path" || rb_err "remove absent-before $path"
    fi
  done < "$MANIFEST"
  # 3) daemon-reload, then restore prior timer enabled/active in order
  $SYSTEMCTL daemon-reload >/dev/null 2>&1 || rb_err "daemon-reload"
  if [ "$TIMER_WAS_ENABLED" = "enabled" ]; then
    $SYSTEMCTL enable reports-cleanup.timer >/dev/null 2>&1 || rb_err "re-enable timer"
  else
    $SYSTEMCTL disable reports-cleanup.timer >/dev/null 2>&1 || rb_err "re-disable timer"
  fi
  if [ "$TIMER_WAS_ACTIVE" = "active" ]; then
    $SYSTEMCTL start reports-cleanup.timer >/dev/null 2>&1 || rb_err "re-start timer"
  else
    $SYSTEMCTL stop reports-cleanup.timer >/dev/null 2>&1 || rb_err "re-stop timer"
  fi
  # 4) restart the OLD bot and VERIFY it came back
  $SYSTEMCTL restart "$SERVICE" >/dev/null 2>&1 || rb_err "restart old bot"
  $SYSTEMCTL is-active --quiet "$SERVICE" || rb_err "old bot not active after restore"
  check_listeners || rb_err "listeners unhealthy after restore"
}

on_exit() {
  local rc=$?
  set +e
  if [ "$rc" -eq 0 ] && [ "$MODE" = "execute" ]; then
    STATUS="DEPLOYED"
  elif [ "$ARMED" -eq 1 ] && [ "$STATUS" != "DEPLOYED" ]; then
    rollback
    if [ -z "$ROLLBACK_ERRORS" ]; then STATUS="ROLLED_BACK"; else STATUS="ROLLBACK_FAILED"; fi
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
    echo "NEW_PID: ${NEW_PID:-<none>}"
    echo "FORWARD_RESTARTS: $RESTARTED_FWD"
    echo "TIMER_WAS: enabled=${TIMER_WAS_ENABLED:-?} active=${TIMER_WAS_ACTIVE:-?}"
    echo "ROLLBACK_ERRORS: ${ROLLBACK_ERRORS:-<none>}"
    echo "FORWARD_EXIT_RC: $rc"
  } > "$REPORT"
  rm -rf -- "$SCRATCH" 2>/dev/null || true
  log "STATUS=$STATUS forward_rc=$rc report=$REPORT"
  # preserve the original forward failure exit code
  exit "$rc"
}
trap on_exit EXIT
date +%Y%m%d-%H%M%S > "$SCRATCH/stamp"

# ─────────────────────── read-only gates ───────────────────────────────────
gate_source() {
  fail_at gate_source && die "forced: gate_source"
  echo "$MERGED_SHA" | grep -Eq '^[0-9a-f]{40}$' || die "merged SHA must be 40 hex"
  # DETACHED (no branch), fully clean; GIT_OPTIONAL_LOCKS=0 keeps status read-only
  if $GITCMD -C "$SOURCE" symbolic-ref -q HEAD >/dev/null 2>&1; then
    die "source is on a branch; a detached worktree at the merged SHA is required"
  fi
  [ -z "$($GITCMD -C "$SOURCE" status --porcelain 2>/dev/null)" ] \
    || die "source worktree has staged/unstaged/untracked changes"
  local head remote
  head="$($GITCMD -C "$SOURCE" rev-parse HEAD 2>/dev/null || echo x)"
  [ "$head" = "$MERGED_SHA" ] || die "source HEAD ($head) != merged SHA"
  # READ-ONLY fresh remote check — ls-remote never updates local refs/objects
  remote="$($GITCMD -C "$SOURCE" ls-remote origin refs/heads/main 2>/dev/null | awk 'NR==1{print $1}')"
  [ -n "$remote" ] || die "git ls-remote origin refs/heads/main failed/empty (fail-closed)"
  [ "$remote" = "$MERGED_SHA" ] || die "origin/main ($remote) != merged SHA (not merged?)"
  log "source gate OK: detached, clean, HEAD==ls-remote main==$MERGED_SHA"
}

gate_suite() {
  fail_at gate_suite && die "forced: gate_suite"
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m py_compile \
     "$SOURCE/bot.py" "$SOURCE/coordination.py" "$SOURCE/resource_footer.py" \
     "$SOURCE/log_filters.py" "$SOURCE"/engines/*.py "$SOURCE"/video_module/*.py \
     "$SOURCE/scripts/coord_publish.py" || die "py_compile failed on source"
  for s in "$SOURCE"/scripts/*.sh; do bash -n "$s" || die "bash -n failed: $s"; done
  local out passed
  out="$( cd "$SOURCE" && PYTHONDONTWRITEBYTECODE=1 \
        ${DEPLOY_PYTEST:-$PYTHON -m pytest} -q -p no:cacheprovider \
        -o cache_dir="$SCRATCH/pytest" --basetemp="$SCRATCH/pt" 2>&1 )" \
    || { echo "$out" | tail -5 >&2; die "source test suite failed"; }
  passed="$(printf '%s' "$out" | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')"
  [ -z "$EXPECTED_TESTS" ] || [ "$passed" = "$EXPECTED_TESTS" ] || die "suite total $passed != expected $EXPECTED_TESTS"
  log "source suite OK ($passed passed)"
}

_active_sessions() {
  [ -f "$R_STATE" ] || { echo 0; return; }
  "$PYTHON" - "$R_STATE" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print(0); sys.exit()
ss=d.get("sessions") or {}
vals=ss.values() if isinstance(ss,dict) else ss
print(sum(1 for s in vals if isinstance(s,dict) and s.get("status") in ("running","queued")))
PY
}

# built-in production gates (each replaceable by a seam ONLY under --test-root;
# the built-in itself uses PATH tools that tests shadow via PATH)
check_capacity() {
  if [ -n "${DEPLOY_CAPACITY_CMD:-}" ]; then "$DEPLOY_CAPACITY_CMD"; return $?; fi
  local avail_kb ram_kb cg oom
  avail_kb="$(df -Pk "$LIVE_ROOT" 2>/dev/null | awk 'NR==2{print $4}')"
  [ "${avail_kb:-0}" -ge $((MIN_DISK_MB*1024)) ] || { log "disk low: ${avail_kb:-0}KB < ${MIN_DISK_MB}MB"; return 1; }
  ram_kb="$(free -k 2>/dev/null | awk '/^Mem:/{print $7}')"
  [ "${ram_kb:-0}" -ge $((MIN_RAM_MB*1024)) ] || { log "available RAM low"; return 1; }
  cg="$($SYSTEMCTL show "$SERVICE" -p ControlGroup --value 2>/dev/null || true)"
  if [ -n "$cg" ] && [ -r "${CGROUP_BASE}${cg}/memory.events" ]; then
    oom="$(awk '/^oom_kill /{print $2}' "${CGROUP_BASE}${cg}/memory.events" 2>/dev/null || echo 0)"
    [ "${oom:-0}" = "0" ] || { log "prior OOM kill=$oom in cgroup"; return 1; }
  fi
  return 0
}
check_listeners() {
  if [ -n "${DEPLOY_LISTENER_CMD:-}" ]; then "$DEPLOY_LISTENER_CMD"; return $?; fi
  local port bind2
  port="$(env_get BOT_NIGHTWATCH_IPC_PORT)"; port="${port:-9091}"
  bind2="$(env_get BOT_NIGHTWATCH_IPC_BIND2)"
  ss -ltn 2>/dev/null | grep -q ":$port " || { log "primary NightWatch listener :$port down"; return 1; }
  if [ -n "$bind2" ]; then
    ss -ltn 2>/dev/null | grep -q "$bind2:$port" || { log "BIND2 listener $bind2:$port down"; return 1; }
  fi
  return 0
}
check_openrouter() {
  if [ -n "${DEPLOY_OPENROUTER_CMD:-}" ]; then "$DEPLOY_OPENROUTER_CMD"; return $?; fi
  local key code
  key="$(env_get OPENROUTER_API_KEY)"
  [ -n "$key" ] || { log "OpenRouter key empty"; return 1; }
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
          -H "Authorization: Bearer $key" https://openrouter.ai/api/v1/key 2>/dev/null || echo 000)"
  [ "$code" = "200" ] || { log "OpenRouter authenticated check http=$code"; return 1; }
  return 0
}
run_as_service_user() { sudo -n -u "$SERVICE_USER" -H "$@"; }
check_service_cli() {
  if [ -n "${DEPLOY_CLAUDE_CMD:-}" ]; then "${DEPLOY_CLAUDE_CMD}" models >/dev/null 2>&1 || { log "Claude CLI"; return 1; }
  else run_as_service_user claude models >/dev/null 2>&1 || { log "Claude CLI (svc user)"; return 1; }; fi
  local codex; codex="${DEPLOY_CODEX_CMD:-}"
  if [ -n "$codex" ]; then
    "$codex" login status >/dev/null 2>&1 || { log "Codex login"; return 1; }
    "$codex" debug models >/dev/null 2>&1 || { log "Codex models"; return 1; }
    "$codex" exec --help    >/dev/null 2>&1 || { log "codex exec --help"; return 1; }
    "$codex" exec resume --help >/dev/null 2>&1 || { log "codex exec resume --help"; return 1; }
  else
    run_as_service_user codex login status >/dev/null 2>&1 || { log "Codex login (svc user)"; return 1; }
    run_as_service_user codex debug models >/dev/null 2>&1 || { log "Codex models (svc user)"; return 1; }
    run_as_service_user codex exec --help >/dev/null 2>&1 || { log "codex exec --help (svc user)"; return 1; }
    run_as_service_user codex exec resume --help >/dev/null 2>&1 || { log "codex resume --help (svc user)"; return 1; }
  fi
  return 0
}

gate_readiness() {
  fail_at gate_readiness && die "forced: gate_readiness"
  $SYSTEMCTL is-active --quiet "$SERVICE" || die "bot service not active"
  OLD_PID="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ -z "$SERVICE_USER" ] && SERVICE_USER="$($SYSTEMCTL show "$SERVICE" -p User --value 2>/dev/null || echo root)"
  [ -n "$SERVICE_USER" ] || SERVICE_USER=root
  log "service active: PID=$OLD_PID user=$SERVICE_USER"
  [ "$(_active_sessions)" = "0" ] || die "refusing: running/queued session(s) present"
  check_capacity   || die "capacity/OOM readiness failed"
  check_listeners  || die "NightWatch listener readiness failed"
  [ -f "$ENV_FILE" ] || die ".env missing"
  check_openrouter || die "OpenRouter authenticated readiness failed"
  check_service_cli || die "service-user CLI/capability readiness failed"
  PYTHONPATH="$SOURCE" "$PYTHON" -c "import telegram, aiohttp, dotenv, engines.base, coordination, resource_footer" \
     || die "runtime import readiness failed (PTB/aiohttp/dotenv/modules)"
  BOT_DATA_ROOT="$LIVE_ROOT" BOT_CONFIGS_DIR="$CONFIGS" "$SOURCE/scripts/cleanup-reports.sh" --dry-run \
      ${TEST_ROOT:+--test-root "$LIVE_ROOT/reports"} >/dev/null 2>&1 || die "report cleanup dry-run failed"
  command -v basic-memory >/dev/null 2>&1 && log "basic-memory: present (deploy-gated, NOT integrated)" \
     || log "basic-memory: ABSENT — optional follow-up (NOT integrated)"
  command -v graphify >/dev/null 2>&1 && log "graphify: present (deploy-gated, NOT integrated)" \
     || log "graphify: ABSENT — optional follow-up (NOT integrated)"
  log "readiness gates OK"
}

# ── binary-safe, atomic, byte-preserving allow-listed .env editor ───────────
set_env_key() {
  local key="$1" val="$2"
  case " $ALLOWLIST " in *" $key "*) : ;; *) die "env key '$key' not allow-listed";; esac
  "$PYTHON" - "$ENV_FILE" "$key" "$val" <<'PY' || die "env edit failed for $key"
import os, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
if "\n" in val or "\r" in val:
    sys.exit("value has newline")
with open(path, "rb") as f:
    data = f.read()
# split into physical lines PRESERVING each line's own terminator
lines, i, n = [], 0, len(data)
while i < n:
    j = data.find(b"\n", i)
    if j == -1:
        lines.append(data[i:]); break
    lines.append(data[i:j+1]); i = j+1
kb = key.encode()
# uncommented "KEY=" lines (allow leading blanks); fail closed on duplicates
hits = [idx for idx, ln in enumerate(lines)
        if ln.lstrip(b" \t").startswith(kb + b"=")]
if len(hits) > 1:
    sys.exit(f"duplicate allow-listed key {key} (ambiguous)")
newval = val.encode()
if len(hits) == 1:
    idx = hits[0]; ln = lines[idx]
    # keep trailing CR/LF terminator bytes exactly
    term = b""
    body = ln
    while body[-1:] in (b"\n", b"\r"):
        term = body[-1:] + term; body = body[:-1]
    lead = body[:len(body) - len(body.lstrip(b" \t"))]
    lines[idx] = lead + kb + b"=" + newval + term
else:
    # append; first normalize a missing final newline, preserving dominant style
    crlf = sum(1 for ln in lines if ln.endswith(b"\r\n"))
    lf   = sum(1 for ln in lines if ln.endswith(b"\n")) - crlf
    nl = b"\r\n" if crlf > lf else b"\n"
    if lines and not lines[-1].endswith(b"\n"):
        lines[-1] = lines[-1] + nl
    lines.append(kb + b"=" + newval + nl)
out = b"".join(lines)
d = os.path.dirname(path) or "."
fd, tmp = None, path + ".deploytmp"
f = open(tmp, "wb")
try:
    f.write(out); f.flush(); os.fsync(f.fileno())
finally:
    f.close()
# preserve mode/owner of the original
st = os.stat(path)
os.chmod(tmp, st.st_mode)
try: os.chown(tmp, st.st_uid, st.st_gid)
except PermissionError: pass
os.replace(tmp, path)
dfd = os.open(d, os.O_RDONLY)
try: os.fsync(dfd)
finally: os.close(dfd)
PY
}

# ─────────────────────── mutation stages ───────────────────────────────────
resolve_service_ids() {   # sets SVC_UID/SVC_GID for the service user (numeric)
  if [ "$SERVICE_USER" = "root" ]; then SVC_UID=0; SVC_GID=0; return; fi
  SVC_UID="$(id -u "$SERVICE_USER" 2>/dev/null || echo 0)"
  SVC_GID="$(id -g "$SERVICE_USER" 2>/dev/null || echo 0)"
}

install_artifacts() {
  fail_at install_artifacts && die "forced: install_artifacts"
  resolve_service_ids
  mkdir -p "$LIVE_SCRIPTS" "$SYSTEMD_DIR" "$CONFIGS"
  local line dest src typ
  while IFS= read -r line; do
    dest="${line%%|*}"; src="${line#*|}"; typ="${src##*|}"; src="${src%|*}"
    if [ "$typ" = "dir" ]; then
      [ -d "$src" ] || die "source dir missing: $src"
      rm -rf -- "$dest"; cp -a "$src" "$dest"
      # RECURSIVE tree hash: source vs installed must match exactly
      [ "$(tree_hash "$src")" = "$(tree_hash "$dest")" ] || die "tree hash mismatch after install: $dest"
    else
      [ -f "$src" ] || die "source file missing: $src"
      install -D -m 0644 "$src" "$dest"
      [ "$(sha "$dest")" = "$(sha "$src")" ] || die "hash mismatch after install: $dest"
    fi
    [ -e "$dest" ] || die "install failed: $dest"
  done < <(artifacts)
  # executable helpers stay root/service-owned + executable (coord_publish is
  # preflighted by _valid_coord_publisher as root/service-owned non-symlink)
  chmod 0755 "$LIVE_SCRIPTS/cleanup-reports.sh" "$LIVE_SCRIPTS/coord_publish.py" \
     || die "chmod helpers failed"
  # writable runtime dirs owned by the SERVICE user (fail on chown/chmod error)
  mkdir -p "$R_CHAT" "$(dirname "$R_COORD")" "$(dirname "$R_CACHE")"
  chmod 0700 "$R_CHAT" || die "chmod chat dir failed"
  chown "$SVC_UID:$SVC_GID" "$R_CHAT" || die "chown chat dir to service user failed"
  chown "$SVC_UID:$SVC_GID" "$(dirname "$R_COORD")" || die "chown coord dir failed"
  # writability AS the service user (not the deploy root user)
  if [ "$SERVICE_USER" != "root" ]; then
    sudo -n -u "$SERVICE_USER" test -w "$R_CHAT" || die "chat dir not writable by $SERVICE_USER"
  else
    [ -w "$R_CHAT" ] || die "chat dir not writable"
  fi
  log "artifacts installed (recursive-hash verified) + service-user ($SERVICE_USER) ownership"
}

edit_env() {
  fail_at edit_env && die "forced: edit_env"
  set_env_key BOT_MAX_SESSIONS 9
  set_env_key BOT_MAX_RUNNING_AGENTS 2
  set_env_key BOT_NIGHTWATCH_IPC_BIND2 10.108.0.4
  set_env_key BOT_CHAT_SESSIONS_DIR "$R_CHAT"
  set_env_key BOT_COORDINATION_FILE "$R_COORD"
  set_env_key BOT_CHAT_CATALOG_CACHE "$R_CACHE"
  set_env_key BOT_COORD_PUBLISHER "$LIVE_SCRIPTS/coord_publish.py"
  log "env allow-list applied (9/2/BIND2 + resolved phase-2 paths)"
}

migrate_retention() {
  fail_at migrate_retention && die "forced: migrate_retention"
  rm -f -- "$CRON_DIR/cleanup-reports" "$CRON_DIR/nightwatch-reports-cleanup"
  if [ -f "$NIGHTLY" ]; then
    # byte-preserving surgical comment-out (Python keepends); fail closed on an
    # entangled report-deletion (shares a line with &&/||/;/continuation)
    "$PYTHON" - "$NIGHTLY" <<'PY' || die "nightly-cleanup migration failed (ambiguous or unsafe)"
import re, sys
p=sys.argv[1]
data=open(p,"rb").read()
lines, i, n = [], 0, len(data)
while i<n:
    j=data.find(b"\n",i)
    if j==-1: lines.append(data[i:]); break
    lines.append(data[i:j+1]); i=j+1
DEL=re.compile(rb"(-delete|-exec[ \t]+rm|rm[ \t]+-rf)")
def isdel(b):
    s=b.lstrip()
    return bool(DEL.search(b)) and (b"reports" in b) and not s.startswith(b"#")
ENT=re.compile(rb"(&&|\|\||;|\\[ \t\r]*$)")
out=[]
for ln in lines:
    if isdel(ln):
        body=ln.rstrip(b"\r\n")
        if ENT.search(body):
            sys.exit("entangled report-deletion; aborting (ambiguous)")
        term=ln[len(body):]
        out.append(b"# [phase1-2 retention migration: removed report-deletion] "+body+term)
    else:
        out.append(ln)
# safety: no ACTIVE report-deletion may remain
for ln in out:
    if isdel(ln): sys.exit("active report-deletion remains after migration")
import os
tmp=p+".deploytmp"
f=open(tmp,"wb"); f.write(b"".join(out)); f.flush(); os.fsync(f.fileno()); f.close()
st=os.stat(p); os.chmod(tmp,st.st_mode)
try: os.chown(tmp,st.st_uid,st.st_gid)
except PermissionError: pass
os.replace(tmp,p)
PY
  fi
  printf 'BOT_REPORT_RETENTION_DAYS=15\n' > "$R_RETENTION_ENV"
  chmod 0644 "$R_RETENTION_ENV" || die "chmod retention env failed"
  BOT_DATA_ROOT="$LIVE_ROOT" BOT_CONFIGS_DIR="$CONFIGS" "$LIVE_SCRIPTS/cleanup-reports.sh" --dry-run \
      ${TEST_ROOT:+--test-root "$LIVE_ROOT/reports"} >/dev/null 2>&1 || die "canonical cleanup dry-run failed"
  [ -e "$CRON_DIR/cleanup-reports" ] && die "cron cleanup-reports not removed"
  [ -e "$CRON_DIR/nightwatch-reports-cleanup" ] && die "cron nightwatch not removed"
  if [ -f "$NIGHTLY" ] && grep -nE '(-delete|-exec[ \t]+rm|rm[ \t]+-rf)' "$NIGHTLY" | grep -E 'reports' | grep -vqE '^[0-9]+:[ \t]*#'; then
    die "post-migration: a report-deletion command still active in nightly-cleanup"
  fi
  log "retention migrated (2 crons + surgical nightly); canonical dry-run OK"
}

verify_install() {
  fail_at verify_install && die "forced: verify_install"
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

_journal_since_restart() {   # only the NEW process's evidence, from the restart cursor
  if [ -n "$RESTART_CURSOR" ]; then
    $JOURNALCTL -u "$SERVICE" --after-cursor="$RESTART_CURSOR" --no-pager 2>/dev/null || true
  else
    $JOURNALCTL -u "$SERVICE" --no-pager 2>/dev/null || true
  fi
}

restart_once() {
  fail_at restart_once && die "forced: restart_once"
  [ "$(_active_sessions)" = "0" ] || die "busy sessions appeared before restart"
  # capture a journal cursor BEFORE restart so health reads only the new window
  RESTART_CURSOR="$($JOURNALCTL -u "$SERVICE" --show-cursor -n0 --no-pager 2>/dev/null \
                    | sed -n 's/^-- cursor: //p' | tail -1)"
  $SYSTEMCTL daemon-reload || die "daemon-reload failed"
  $SYSTEMCTL restart "$SERVICE" || die "restart failed"
  RESTARTED_FWD=$((RESTARTED_FWD+1))
  NEW_PID="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ -n "$NEW_PID" ] && [ "$NEW_PID" != "$OLD_PID" ] || die "PID did not change after restart"
  log "service restarted once: pid $OLD_PID -> $NEW_PID"
}

_health_assert() {   # shared by health_check and observe
  local ctx="$1" jrl pid
  pid="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ "$pid" = "$NEW_PID" ] || { log "$ctx: PID changed ($NEW_PID -> $pid)"; return 1; }
  $SYSTEMCTL is-active --quiet "$SERVICE" || { log "$ctx: service not active"; return 1; }
  jrl="$(_journal_since_restart)"
  echo "$jrl" | grep -qiE 'traceback|fatal' && { log "$ctx: fatal/traceback in new-process journal"; return 1; }
  echo "$jrl" | grep -q 'Bot v4 ready' || { log "$ctx: 'Bot v4 ready' not seen (new process)"; return 1; }
  echo "$jrl" | grep -Eq 'max_sessions=9' && echo "$jrl" | grep -Eq 'max_running_agents=2' \
     || { log "$ctx: effective limits not 9/2"; return 1; }
  # exact three engines from the real registration line
  local names; names="$(echo "$jrl" | sed -n 's/.*engines\.registered names=//p' | tail -1)"
  echo ",$names," | grep -q ',claude,' && echo ",$names," | grep -q ',codex,' && echo ",$names," | grep -q ',chat,' \
     || { log "$ctx: engines.registered names missing an engine (got '$names')"; return 1; }
  check_listeners || { log "$ctx: listeners unhealthy"; return 1; }
  check_capacity  || { log "$ctx: cgroup/OOM/capacity unstable"; return 1; }
  # writable paths AS the service user
  if [ "$SERVICE_USER" != "root" ]; then
    sudo -n -u "$SERVICE_USER" test -w "$R_CHAT" || { log "$ctx: chat not writable by svc user"; return 1; }
    sudo -n -u "$SERVICE_USER" test -w "$(dirname "$R_COORD")" || { log "$ctx: coord dir not writable by svc user"; return 1; }
  else
    [ -w "$R_CHAT" ] && [ -w "$(dirname "$R_COORD")" ] || { log "$ctx: paths not writable"; return 1; }
  fi
  return 0
}

health_check() {
  fail_at health_check && die "forced: health_check"
  _health_assert "health" || die "post-restart health failed"
  log "health OK (new pid $NEW_PID): ready, 9/2, engines, listeners, paths, cgroup"
}

observe() {
  fail_at observe && die "forced: observe"
  [ "$OBSERVE" -gt 0 ] && sleep "$OBSERVE"
  # repeat the FULL health assertion, and require the SAME new PID (no churn)
  _health_assert "observe" || die "post-observation health failed (PID churn or degraded)"
  log "observation window ($OBSERVE s) passed; PID stable at $NEW_PID"
}

enable_timer() {
  # activate ONLY at the final commit boundary, after bot observation
  fail_at enable_timer && die "forced: enable_timer"
  $SYSTEMCTL enable reports-cleanup.timer >/dev/null 2>&1 || die "timer enable failed"
  $SYSTEMCTL start  reports-cleanup.timer >/dev/null 2>&1 || die "timer start failed"
  TIMER_ACTIVATED=1
  $SYSTEMCTL is-enabled --quiet reports-cleanup.timer || die "timer not enabled"
  $SYSTEMCTL is-active  --quiet reports-cleanup.timer || die "timer not active"
  local ex; ex="$($SYSTEMCTL show reports-cleanup.service -p ExecStart --value 2>/dev/null || echo '')"
  [ -n "$ex" ] || die "timer ExecStart is empty"                        # no empty-bypass
  case "$ex" in *"$LIVE_SCRIPTS/cleanup-reports.sh"*) : ;; *) die "timer ExecStart not the stable live path: $ex";; esac
  log "retention timer activated at commit boundary; ExecStart=$ex"
}

# ─────────────────────── main ──────────────────────────────────────────────
resolve_paths
gate_source
gate_suite
gate_readiness

if [ "$MODE" = "preflight" ]; then
  log "PREFLIGHT complete — read-only (no Git-metadata/live writes), no lock, scratch under /tmp"
  exit 0
fi

[ "$REVIEWED_PR" = "19" ] || die "reviewed PR must be 19"
exec 9>"$LOCK"; flock -n 9 || die "another deploy holds the lock"

build_manifest
ARMED=1

install_artifacts
edit_env
migrate_retention
verify_install
restart_once
health_check
observe
enable_timer        # final commit boundary — timer only now

exit 0
