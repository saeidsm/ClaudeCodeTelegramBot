#!/usr/bin/env bash
#
# deploy-phase1-phase2.sh — ONE transactional, manifest-driven installer for the
# combined Phase-1 + Phase-2 bot deployment. It IS the runbook
# (docs/PHASE2_DEPLOY_RUNBOOK.md) in executable form.
#
# Round-5 production-safety contract:
#   * .env is parsed ONCE, non-executingly, into /tmp scratch state; any parser/
#     read/syntax/duplicate-key failure stops before mutation (no || true).
#   * Production thresholds (disk/RAM/swap) are HARD-CODED; DEPLOY_MIN_* and all
#     other seams are refused outside the --test-root harness. The cgroup/OOM
#     gate fails closed when the cgroup path or memory.events is unreadable.
#   * The systemd service user must EXIST (no UID-0 fallback); root probes run
#     directly, non-root probes require proven noninteractive sudo. Claude/Codex
#     capability checks validate CONTENT contracts (catalog ids, logged-in,
#     Sol/Terra/Luna, --json, resume-by-session-id), not just exit status.
#   * Listener readiness verifies the EXACT expected bind:port endpoints (no
#     wildcard/wrong-IP/unrelated-process), binds evidence to the service PID
#     where the platform reports it, and performs the bot's /healthz contract.
#     The OpenRouter probe requires HTTP 200 + a minimally valid catalog payload
#     and never prints the key/headers/bodies.
#   * Installed-tree integrity uses a full manifest (path+type+bytes+symlink
#     target+mode bits+empty dirs); escaping symlinks are rejected.
#   * Atomic editors create unpredictable O_CREAT|O_EXCL|O_NOFOLLOW temp files,
#     preserve owner/group/mode or fail, fsync file+dir, refuse symlinked
#     targets and duplicate (incl. `export KEY=`) safety-critical keys.
#   * The retention service unit is RENDERED to the resolved config/script/guard
#     paths and verified (incl. script hash) before activation; the timer is
#     non-persistent so no catch-up can fire real cleanup inside the
#     transaction; activation happens only at the final commit boundary.
#   * Health is new-PID-only: a mandatory pre-restart journal cursor, bounded
#     startup polling, journal evidence filtered by _PID=<new MainPID>, exact
#     engine set, exact listeners; observation retains the same PID.
#   * Rollback VERIFIES every restore (type/hash/tree/target/mode/owner/group)
#     and old-bot health; any failure yields ROLLBACK_FAILED, never a false
#     ROLLED_BACK. The forward failure exit code is preserved.
#
# Terminal status (exactly one):
#   DEPLOYED | ROLLED_BACK | ROLLBACK_FAILED | STOPPED_BEFORE_MUTATION
#
# Usage:
#   deploy-phase1-phase2.sh --preflight --merged-sha <40-hex> --source <detached-wt>
#   deploy-phase1-phase2.sh --execute  --reviewed-pr 19 --merged-sha <40-hex> --source <detached-wt>
#   deploy-phase1-phase2.sh --tree-manifest <dir>       # print tree manifest (test/verify utility)
#
# Test seams (honoured ONLY with --test-root beneath /tmp; production refuses
# every one): GIT SYSTEMCTL JOURNALCTL DEPLOY_CRON_DIR DEPLOY_SYSTEMD_DIR
# DEPLOY_BACKUP_ROOT DEPLOY_LOCK DEPLOY_PYTHON DEPLOY_PYTEST DEPLOY_STATE_FILE
# DEPLOY_NIGHTLY_CLEANUP DEPLOY_CLAUDE_CMD DEPLOY_CODEX_CMD DEPLOY_LISTENER_CMD
# DEPLOY_CAPACITY_CMD DEPLOY_OPENROUTER_CMD DEPLOY_CGROUP_BASE DEPLOY_SERVICE_USER
# DEPLOY_OBSERVE_SECONDS DEPLOY_FAIL_AT DEPLOY_EXPECTED_TESTS DEPLOY_MIN_DISK_MB
# DEPLOY_MIN_RAM_MB DEPLOY_MIN_SWAP_MB DEPLOY_HEALTH_DEADLINE DEPLOY_HEALTH_POLL
# DEPLOY_RB_CORRUPT_CMD.
set -euo pipefail

PROD_ROOT="/opt/shahrzad-devops"
SERVICE="claude-telegram-bot"
# HARD-CODED production minima — not overridable outside the test harness.
PROD_MIN_DISK_MB=500
PROD_MIN_RAM_MB=150
PROD_MIN_SWAP_MB=64
PROD_HEALTH_DEADLINE=60          # seconds of bounded startup polling
PROD_HEALTH_POLL=2               # seconds between polls
PROD_OBSERVE_SECONDS=90

MODE="preflight"; REVIEWED_PR=""; MERGED_SHA=""; TEST_ROOT=""; TREE_DIR=""
REVIEWED_HEAD=""; REVIEWED_TREE=""; MERGED_TREE=""
SET_FILE=""; SET_KEY=""; SET_VAL=""
SOURCE="${DEPLOY_SOURCE:-}"

ALLOWLIST=" BOT_MAX_SESSIONS BOT_MAX_RUNNING_AGENTS BOT_NIGHTWATCH_IPC_BIND2 \
BOT_CHAT_SESSIONS_DIR BOT_COORDINATION_FILE BOT_CHAT_CATALOG_TTL \
BOT_CHAT_CONCURRENCY BOT_CHAT_HISTORY_MAX_TURNS BOT_CHAT_HISTORY_MAX_CHARS \
BOT_CODEX_DEFAULT_MODEL BOT_CODEX_SANDBOX BOT_CLAUDE_MODELS BOT_CODEX_MODELS \
BOT_REPORT_RETENTION_DAYS BOT_COORD_PUBLISHER BOT_CHAT_CATALOG_CACHE "
# Safety-critical keys for duplicate detection = allow-list + path/identity keys.
CRITICAL_KEYS="$ALLOWLIST OPENROUTER_API_KEY TELEGRAM_BOT_TOKEN BOT_DATA_ROOT \
BOT_CONFIGS_DIR BOT_NIGHTWATCH_IPC_PORT BOT_NIGHTWATCH_IPC_BIND BOT_NIGHTWATCH_IPC_ENABLED"

TEST_SEAM_VARS="GIT SYSTEMCTL JOURNALCTL DEPLOY_CRON_DIR DEPLOY_SYSTEMD_DIR \
DEPLOY_BACKUP_ROOT DEPLOY_LOCK DEPLOY_PYTHON DEPLOY_PYTEST DEPLOY_STATE_FILE \
DEPLOY_NIGHTLY_CLEANUP DEPLOY_CLAUDE_CMD DEPLOY_CODEX_CMD DEPLOY_LISTENER_CMD \
DEPLOY_CAPACITY_CMD DEPLOY_OPENROUTER_CMD DEPLOY_CGROUP_BASE DEPLOY_SERVICE_USER \
DEPLOY_OBSERVE_SECONDS DEPLOY_FAIL_AT DEPLOY_EXPECTED_TESTS DEPLOY_MIN_DISK_MB \
DEPLOY_MIN_RAM_MB DEPLOY_MIN_SWAP_MB DEPLOY_HEALTH_DEADLINE DEPLOY_HEALTH_POLL \
DEPLOY_RB_CORRUPT_CMD"

log()  { echo "[deploy] $*" >&2; }
die()  { echo "[deploy] NO-GO: $*" >&2; exit 3; }
fail_at() { [ "${DEPLOY_FAIL_AT:-}" = "$1" ] && { log "fault-injection at stage '$1'"; return 0; } || return 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --preflight|--dry-run) MODE="preflight" ;;
    --execute)             MODE="execute" ;;
    --tree-manifest) MODE="tree"; TREE_DIR="${2:-}"; shift ;;
    --set-env-key)   MODE="setenv"; SET_FILE="${2:-}"; SET_KEY="${3:-}"; SET_VAL="${4:-}"; shift 3 ;;
    --reviewed-pr) REVIEWED_PR="${2:-}"; shift ;;
    --reviewed-head) REVIEWED_HEAD="${2:-}"; shift ;;
    --reviewed-tree) REVIEWED_TREE="${2:-}"; shift ;;
    --merged-sha)  MERGED_SHA="${2:-}"; shift ;;
    --source)      SOURCE="${2:-}"; shift ;;
    --test-root)   TEST_ROOT="${2:-}"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# ── tree manifest (utility + install/rollback verification core) ────────────
PYTHON="${DEPLOY_PYTHON:-python3}"
tree_manifest() {   # tree_manifest <dir>  -> deterministic manifest on stdout
  "$PYTHON" - "$1" <<'PY'
import hashlib, os, stat, sys
root = sys.argv[1]
if not os.path.isdir(root):
    sys.exit(f"not a directory: {root}")
rows = []
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirnames.sort(); filenames.sort()
    rel = os.path.relpath(dirpath, root)
    if rel != ".":
        st = os.lstat(dirpath)
        rows.append(("d", rel, oct(stat.S_IMODE(st.st_mode)), "-"))
    for n in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
        p = os.path.join(dirpath, n)
        r = os.path.relpath(p, root)
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            rows.append(("l", r, oct(stat.S_IMODE(st.st_mode)), os.readlink(p)))
        elif stat.S_ISREG(st.st_mode):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            rows.append(("f", r, oct(stat.S_IMODE(st.st_mode)), h.hexdigest()))
        else:
            rows.append(("?", r, oct(stat.S_IMODE(st.st_mode)), "special"))
rows.sort(key=lambda t: (t[1], t[0]))
for t, r, m, x in rows:
    print(f"{t}\t{r}\t{m}\t{x}")
PY
}
tree_hash() { tree_manifest "$1" | sha256sum | awk '{print $1}'; }
tree_symlinks_safe() {   # fail if any symlink target escapes the tree
  "$PYTHON" - "$1" <<'PY'
import os, sys
root = os.path.realpath(sys.argv[1])
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    for n in dirnames + filenames:
        p = os.path.join(dirpath, n)
        if os.path.islink(p):
            tgt = os.path.realpath(p)
            if not (tgt + os.sep).startswith(root + os.sep) and tgt != root:
                sys.exit(f"escaping symlink: {p} -> {os.readlink(p)}")
PY
}

if [ "$MODE" = "tree" ]; then
  [ -n "$TREE_DIR" ] || die "--tree-manifest requires a directory"
  tree_manifest "$TREE_DIR"
  exit 0
fi

# ── resolve live root; production refuses ALL test seams ────────────────────
if [ -n "$TEST_ROOT" ]; then
  case "$(readlink -f "$TEST_ROOT" 2>/dev/null || echo "$TEST_ROOT")" in
    /tmp/*) : ;; *) die "--test-root must resolve under /tmp";;
  esac
  LIVE_ROOT="$TEST_ROOT"
else
  LIVE_ROOT="$PROD_ROOT"
  # utility modes (--tree-manifest handled above; --set-env-key below) never
  # touch live paths — their targets are constrained to /tmp — so the seam
  # refusal applies only to the real preflight/execute modes
  if [ "$MODE" != "setenv" ]; then
    for v in $TEST_SEAM_VARS; do
      [ -n "${!v:-}" ] && die "production mode refuses test seam $v (use --test-root under /tmp)"
    done
  fi
fi

LIVE_SCRIPTS="$LIVE_ROOT/scripts"
LIVE_ENGINES="$LIVE_SCRIPTS/engines"
ENV_FILE="$LIVE_ROOT/.env"
CONFIGS="$LIVE_ROOT/configs"
CRON_DIR="${DEPLOY_CRON_DIR:-/etc/cron.d}"
SYSTEMD_DIR="${DEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
NIGHTLY="${DEPLOY_NIGHTLY_CLEANUP:-$LIVE_SCRIPTS/nightly-cleanup.sh}"
BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-$LIVE_ROOT/backups}"
LOCK="${DEPLOY_LOCK:-$LIVE_ROOT/.deploy.lock}"
GITCMD="${GIT:-git}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
JOURNALCTL="${JOURNALCTL:-journalctl}"
CGROUP_BASE="${DEPLOY_CGROUP_BASE:-/sys/fs/cgroup}"
REPORT="${DEPLOY_REPORT:-/tmp/phase12-deploy-report.md}"
EXPECTED_TESTS="${DEPLOY_EXPECTED_TESTS:-}"
SERVICE_USER="${DEPLOY_SERVICE_USER:-}"

# Thresholds: hard-coded in production; overridable ONLY inside the test harness.
if [ -n "$TEST_ROOT" ]; then
  MIN_DISK_MB="${DEPLOY_MIN_DISK_MB:-$PROD_MIN_DISK_MB}"
  MIN_RAM_MB="${DEPLOY_MIN_RAM_MB:-$PROD_MIN_RAM_MB}"
  MIN_SWAP_MB="${DEPLOY_MIN_SWAP_MB:-$PROD_MIN_SWAP_MB}"
  HEALTH_DEADLINE="${DEPLOY_HEALTH_DEADLINE:-$PROD_HEALTH_DEADLINE}"
  HEALTH_POLL="${DEPLOY_HEALTH_POLL:-$PROD_HEALTH_POLL}"
  OBSERVE="${DEPLOY_OBSERVE_SECONDS:-$PROD_OBSERVE_SECONDS}"
else
  MIN_DISK_MB=$PROD_MIN_DISK_MB
  MIN_RAM_MB=$PROD_MIN_RAM_MB
  MIN_SWAP_MB=$PROD_MIN_SWAP_MB
  HEALTH_DEADLINE=$PROD_HEALTH_DEADLINE
  HEALTH_POLL=$PROD_HEALTH_POLL
  OBSERVE=$PROD_OBSERVE_SECONDS
fi

case "$REPORT" in /tmp/*) : ;; *) [ -n "$TEST_ROOT" ] || die "report must be under /tmp";; esac
[ -n "$SOURCE" ] || SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$SOURCE" ] || die "source dir not found: $SOURCE"

SCRATCH="$(mktemp -d /tmp/deploy-scratch.XXXXXX)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$SCRATCH/pycache"
export GIT_OPTIONAL_LOCKS=0

STATUS="STOPPED_BEFORE_MUTATION"
ARMED=0; RESTARTED_FWD=0; TIMER_ACTIVATED=0
MANIFEST=""; BACKUP=""; OLD_PID=""; NEW_PID=""; RESTART_CURSOR=""
TIMER_WAS_ENABLED=""; TIMER_WAS_ACTIVE=""
ROLLBACK_ERRORS=""
SVC_UID=""; SVC_GID=""
R_STATE=""; R_CHAT=""; R_COORD=""; R_COORDLOCK=""; R_CACHE=""; R_TOKEN=""; R_RETENTION_ENV=""
NW_ENABLED=""; NW_BIND=""; NW_BIND2=""; NW_PORT=""
# Immutable pre-deploy listener snapshot (Gap 3): captured once right after the
# single original env parse and NEVER mutated by forward edits.
OLD_NW_ENABLED=""; OLD_NW_BIND=""; OLD_NW_BIND2=""; OLD_NW_PORT=""
QUIESCED=0; QUIESCE_STOPS=0; QUIESCE_RESTORE="n/a"
LAST_HEALTH_ERR=""

sha() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "-"; }

# ── ONE fail-closed, non-executing .env parse into /tmp scratch ─────────────
ENV_TSV="$SCRATCH/env.tsv"
parse_env_once() {
  [ -f "$ENV_FILE" ] || die ".env missing: $ENV_FILE"
  [ -L "$ENV_FILE" ] && die ".env is a symlink (ambiguous): $ENV_FILE"
  # single python pass: dotenv import, readability, strict syntax, duplicate
  # detection (export KEY= and KEY= are the SAME key), base64-encoded output.
  "$PYTHON" - "$ENV_FILE" "$ENV_TSV" "$CRITICAL_KEYS" <<'PY' || die "env parse failed (fail-closed; see reason above)"
import base64, re, sys
path, out, crit_raw = sys.argv[1], sys.argv[2], sys.argv[3]
critical = set(crit_raw.split())
try:
    from dotenv import dotenv_values
except Exception as e:
    sys.exit(f"python-dotenv parser unavailable: {e}")
try:
    raw = open(path, "rb").read()
except OSError as e:
    sys.exit(f".env unreadable: {e}")
text = raw.decode("utf-8", errors="strict") if True else ""
line_re = re.compile(r"^\s*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*\s*=")
seen = {}
for i, line in enumerate(text.splitlines(), 1):
    s = line.strip()
    if not s or s.startswith("#"):
        continue
    if not line_re.match(line):
        sys.exit(f".env syntax error at line {i}: {s[:60]!r}")
    key = re.sub(r"^\s*(?:export[ \t]+)?", "", line).split("=", 1)[0].strip()
    if key in critical:
        seen.setdefault(key, []).append(i)
for key, lines in seen.items():
    if len(lines) > 1:
        sys.exit(f"duplicate safety-critical key {key} at lines {lines} (export/plain count as the same key)")
vals = dotenv_values(path)
with open(out, "w", encoding="utf-8") as f:
    for k, v in vals.items():
        if v is None:
            v = ""
        f.write(f"{k}\t{base64.b64encode(v.encode()).decode()}\n")
PY
  [ -s "$ENV_TSV" ] || [ -f "$ENV_TSV" ] || die "env scratch state missing after parse"
  log "env parsed once into scratch (fail-closed): $ENV_TSV"
}

env_get() {   # env_get KEY -> value from the ONE validated parse (no re-parsing)
  [ -f "$ENV_TSV" ] || die "internal: env_get before parse_env_once"
  local line
  line="$(grep -m1 "^${1}$(printf '\t')" "$ENV_TSV" || true)"
  [ -n "$line" ] || { printf ''; return 0; }
  printf '%s' "$line" | cut -f2 | base64 -d
}

inside_root() {
  local p r
  p="$("$PYTHON" -c 'import os,sys; print(os.path.realpath(os.path.dirname(sys.argv[1])))' "$1")"
  r="$("$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$2")"
  case "$p/" in "$r/"*) return 0;; *) return 1;; esac
}

resolve_paths() {
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
  local p
  for p in "$R_STATE" "$R_CHAT" "$R_COORD" "$R_COORDLOCK" "$R_CACHE" "$R_TOKEN" "$R_RETENTION_ENV"; do
    inside_root "$p" "$LIVE_ROOT" || die "resolved path escapes live root: $p"
    [ -L "$p" ] && die "resolved path is a symlink (ambiguous): $p"
  done
  # listener expectation from the SAME validated parse
  NW_ENABLED="$(env_get BOT_NIGHTWATCH_IPC_ENABLED)"; NW_ENABLED="${NW_ENABLED:-true}"
  NW_BIND="$(env_get BOT_NIGHTWATCH_IPC_BIND)"; NW_BIND="${NW_BIND:-127.0.0.1}"
  NW_BIND2="$(env_get BOT_NIGHTWATCH_IPC_BIND2)"
  NW_PORT="$(env_get BOT_NIGHTWATCH_IPC_PORT)"; NW_PORT="${NW_PORT:-9091}"
  echo "$NW_PORT" | grep -Eq '^[0-9]+$' || die "invalid BOT_NIGHTWATCH_IPC_PORT: $NW_PORT"
  # Gap 3: IMMUTABLE pre-deploy listener snapshot — rollback verification uses
  # exactly these, never the forward values edit_env writes later.
  OLD_NW_ENABLED="$NW_ENABLED"; OLD_NW_BIND="$NW_BIND"
  OLD_NW_BIND2="$NW_BIND2";     OLD_NW_PORT="$NW_PORT"
  log "resolved: state=$R_STATE chat=$R_CHAT coord=$R_COORD cache=$R_CACHE configs=$CONFIGS nw=$NW_ENABLED@$NW_BIND${NW_BIND2:+,+$NW_BIND2}:$NW_PORT (original snapshot preserved)"
}

# ── artifact table ──────────────────────────────────────────────────────────
artifacts() {
  echo "$LIVE_SCRIPTS/claude-telegram-bot.py|$SOURCE/bot.py|file"
  echo "$LIVE_SCRIPTS/coordination.py|$SOURCE/coordination.py|file"
  echo "$LIVE_SCRIPTS/resource_footer.py|$SOURCE/resource_footer.py|file"
  echo "$LIVE_SCRIPTS/log_filters.py|$SOURCE/log_filters.py|file"
  echo "$LIVE_SCRIPTS/coord_publish.py|$SOURCE/scripts/coord_publish.py|file"
  echo "$LIVE_ENGINES|$SOURCE/engines|dir"
  echo "$LIVE_SCRIPTS/video_module|$SOURCE/video_module|dir"
  echo "$LIVE_SCRIPTS/cleanup-reports.sh|$SOURCE/scripts/cleanup-reports.sh|file"
  echo "$SYSTEMD_DIR/reports-cleanup.service|$SOURCE/systemd/reports-cleanup.service|render"
  echo "$SYSTEMD_DIR/reports-cleanup.timer|$SOURCE/systemd/reports-cleanup.timer|file"
}
backup_only() {
  echo "$ENV_FILE"; echo "$R_STATE"; echo "$R_COORD"; echo "$R_COORDLOCK"
  echo "$R_CACHE"; echo "$R_CHAT"; echo "$R_RETENTION_ENV"
  echo "$CRON_DIR/cleanup-reports"; echo "$CRON_DIR/nightwatch-reports-cleanup"; echo "$NIGHTLY"
}

# ── manifest (state,type,owner,group,mode,hash|tree|target) + backup ────────
record() {
  local p="$1" key state typ owner group mode hash
  key="$(printf '%s' "$p" | sha256sum | awk '{print $1}')"
  if [ -e "$p" ] || [ -L "$p" ]; then
    state="present"
    if   [ -L "$p" ]; then typ="link"; hash="$(readlink "$p")"
    elif [ -d "$p" ]; then typ="dir";  hash="$(tree_hash "$p")"
    else typ="file"; hash="$(sha "$p")"; fi
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

verify_manifest_entry() {   # after restore: prove the entry matches its record
  local state="$1" typ="$2" owner="$3" group="$4" mode="$5" hash="$6" path="$7"
  if [ "$state" = "absent" ]; then
    { [ -e "$path" ] || [ -L "$path" ]; } && rb_err "absent-before still exists: $path"
    return 0
  fi
  if [ -L "$path" ]; then
    [ "$typ" = "link" ] || { rb_err "type mismatch (now link): $path"; return 0; }
    [ "$(readlink "$path")" = "$hash" ] || rb_err "symlink target mismatch: $path"
    # stat without -L is lstat: verify the link's own recorded owner/group too
    [ "$(stat -c '%u' "$path")" = "$owner" ] || rb_err "symlink owner mismatch: $path"
    [ "$(stat -c '%g' "$path")" = "$group" ] || rb_err "symlink group mismatch: $path"
    return 0
  fi
  [ -e "$path" ] || { rb_err "present-before missing after restore: $path"; return 0; }
  local t
  if [ -d "$path" ]; then t="dir"; elif [ -f "$path" ]; then t="file"; else t="?"; fi
  [ "$t" = "$typ" ] || { rb_err "type mismatch: $path ($typ -> $t)"; return 0; }
  case "$typ" in
    file) [ "$(sha "$path")" = "$hash" ] || rb_err "content mismatch after restore: $path";;
    dir)  [ "$(tree_hash "$path")" = "$hash" ] || rb_err "tree mismatch after restore: $path";;
  esac
  [ "$(stat -c '%a' "$path")" = "$mode" ]  || rb_err "mode mismatch after restore: $path"
  [ "$(stat -c '%u' "$path")" = "$owner" ] || rb_err "owner mismatch after restore: $path"
  [ "$(stat -c '%g' "$path")" = "$group" ] || rb_err "group mismatch after restore: $path"
}

rollback() {
  log "ROLLING BACK from $MANIFEST"
  # 1) stop any newly-activated timer FIRST (no cleanup can run mid-restore)
  if [ "$TIMER_ACTIVATED" -eq 1 ]; then
    $SYSTEMCTL stop reports-cleanup.timer >/dev/null 2>&1 || rb_err "stop new timer"
  fi
  # 2) restore every entry; remove absent-before
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
  # 2b) test-only corruption injection point (verifies the verification)
  if [ -n "$TEST_ROOT" ] && [ -n "${DEPLOY_RB_CORRUPT_CMD:-}" ]; then
    "$DEPLOY_RB_CORRUPT_CMD" || rb_err "rb-corrupt-cmd failed"
  fi
  # 3) VERIFY every entry against its recorded type/hash/target/mode/owner/group
  while IFS=$'\t' read -r state typ owner group mode hash key path; do
    [ -n "$path" ] || continue
    verify_manifest_entry "$state" "$typ" "$owner" "$group" "$mode" "$hash" "$path"
  done < "$MANIFEST"
  # 3b) no editor temp files / partial artifacts may remain
  if find "$LIVE_ROOT" "$(dirname "$NIGHTLY")" -maxdepth 3 -name '.*.deploytmp' 2>/dev/null | grep -q .; then
    rb_err "editor temp files left behind"
  fi
  # 4) daemon-reload, restore prior timer state in order
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
  # 5) restart the OLD bot and verify it is genuinely back
  $SYSTEMCTL restart "$SERVICE" >/dev/null 2>&1 || rb_err "restart old bot"
  $SYSTEMCTL is-active --quiet "$SERVICE" || rb_err "old bot not active after restore"
  local rbpid
  rbpid="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ -n "$rbpid" ] && [ "$rbpid" != "0" ] || rb_err "old bot has no MainPID after restore"
  # rollback verifies the ORIGINAL pre-deploy endpoints, never forward values
  check_listeners "$rbpid" original || rb_err "listeners unhealthy after restore (original profile)"
}

on_exit() {
  local rc=$?
  set +e
  if [ "$rc" -eq 0 ] && [ "$MODE" = "execute" ]; then
    STATUS="DEPLOYED"
  elif [ "$ARMED" -eq 1 ] && [ "$STATUS" != "DEPLOYED" ]; then
    rollback
    if [ -z "$ROLLBACK_ERRORS" ]; then STATUS="ROLLED_BACK"; else STATUS="ROLLBACK_FAILED"; fi
  elif [ "$QUIESCED" -eq 1 ] && [ "$rc" -ne 0 ]; then
    # quiesce succeeded/attempted but we failed BEFORE any file mutation:
    # restore availability (start the old bot once) — live files untouched
    $SYSTEMCTL start "$SERVICE" >/dev/null 2>&1
    if $SYSTEMCTL is-active --quiet "$SERVICE"; then QUIESCE_RESTORE="ok"; else QUIESCE_RESTORE="FAILED"; fi
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
    echo "QUIESCE_STOPS: $QUIESCE_STOPS"
    echo "QUIESCE_RESTORE: $QUIESCE_RESTORE"
    echo "REVIEWED_HEAD: ${REVIEWED_HEAD:-<none>}"
    echo "REVIEWED_TREE: ${REVIEWED_TREE:-<none>}"
    echo "MERGED_TREE: ${MERGED_TREE:-<none>}"
    echo "TIMER_WAS: enabled=${TIMER_WAS_ENABLED:-?} active=${TIMER_WAS_ACTIVE:-?}"
    echo "ROLLBACK_ERRORS: ${ROLLBACK_ERRORS:-<none>}"
    echo "FORWARD_EXIT_RC: $rc"
  } > "$REPORT"
  rm -rf -- "$SCRATCH" 2>/dev/null || true
  log "STATUS=$STATUS forward_rc=$rc report=$REPORT"
  exit "$rc"
}
trap on_exit EXIT
date +%Y%m%d-%H%M%S > "$SCRATCH/stamp"

# ─────────────────────── read-only gates ───────────────────────────────────
gate_source() {
  fail_at gate_source && die "forced: gate_source"
  echo "$MERGED_SHA" | grep -Eq '^[0-9a-f]{40}$' || die "merged SHA must be 40 hex"
  if $GITCMD -C "$SOURCE" symbolic-ref -q HEAD >/dev/null 2>&1; then
    die "source is on a branch; a detached worktree at the merged SHA is required"
  fi
  [ -z "$($GITCMD -C "$SOURCE" status --porcelain 2>/dev/null)" ] \
    || die "source worktree has staged/unstaged/untracked changes"
  local head remote
  head="$($GITCMD -C "$SOURCE" rev-parse HEAD 2>/dev/null || echo x)"
  [ "$head" = "$MERGED_SHA" ] || die "source HEAD ($head) != merged SHA"
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
  # STRICT, fail-closed state read: a missing/unreadable file, invalid JSON,
  # unexpected shape, or unknown status is NEVER translated into "0 active".
  # The caller must `|| die` on nonzero exit.
  "$PYTHON" - "$R_STATE" <<'PY'
import json, sys
p = sys.argv[1]
KNOWN = {"idle", "running", "queued", "completed", "error"}
try:
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
except FileNotFoundError:
    sys.exit(f"state file missing (fail-closed): {p}")
except OSError as e:
    sys.exit(f"state unreadable (fail-closed): {e}")
except json.JSONDecodeError as e:
    sys.exit(f"state invalid JSON (fail-closed): {e}")
if not isinstance(d, dict):
    sys.exit("state top-level is not an object (fail-closed)")
ss = d.get("sessions")
if ss is None:
    sys.exit("state missing 'sessions' (fail-closed)")
if isinstance(ss, dict):
    vals = list(ss.values())
elif isinstance(ss, list):
    vals = ss
else:
    sys.exit("'sessions' has unexpected shape (fail-closed)")
n = 0
for s in vals:
    if not isinstance(s, dict):
        sys.exit("session entry is not an object (fail-closed)")
    st = s.get("status")
    if st not in KNOWN:
        sys.exit(f"unknown session status {st!r} (fail-closed)")
    if st in ("running", "queued"):
        n += 1
print(n)
PY
}
_require_zero_sessions() {   # _require_zero_sessions <context>
  local busy
  busy="$(_active_sessions)" || die "state gate failed at $1 (fail-closed; see reason above)"
  [ "$busy" = "0" ] || die "refusing at $1: $busy running/queued session(s)"
}

# ── capacity: disk + RAM + swap + MANDATORY cgroup/OOM (fail closed) ────────
check_capacity() {
  if [ -n "${DEPLOY_CAPACITY_CMD:-}" ]; then "$DEPLOY_CAPACITY_CMD"; return $?; fi
  local avail_kb ram_kb swap_kb cg evfile oom
  avail_kb="$(df -Pk "$LIVE_ROOT" 2>/dev/null | awk 'NR==2{print $4}')"
  [ "${avail_kb:-0}" -ge $((MIN_DISK_MB*1024)) ] || { log "disk low: ${avail_kb:-0}KB < ${MIN_DISK_MB}MB"; return 1; }
  ram_kb="$(free -k 2>/dev/null | awk '/^Mem:/{print $7}')"
  [ "${ram_kb:-0}" -ge $((MIN_RAM_MB*1024)) ] || { log "available RAM low: ${ram_kb:-0}KB < ${MIN_RAM_MB}MB"; return 1; }
  swap_kb="$(free -k 2>/dev/null | awk '/^Swap:/{print $4}')"
  [ "${swap_kb:-0}" -ge $((MIN_SWAP_MB*1024)) ] || { log "free swap low: ${swap_kb:-0}KB < ${MIN_SWAP_MB}MB"; return 1; }
  # cgroup gate is MANDATORY: unresolvable path / unreadable events = fail closed
  cg="$($SYSTEMCTL show "$SERVICE" -p ControlGroup --value 2>/dev/null || true)"
  [ -n "$cg" ] || { log "cgroup path unresolvable for $SERVICE (fail-closed)"; return 1; }
  evfile="${CGROUP_BASE}${cg}/memory.events"
  [ -r "$evfile" ] || { log "memory.events unreadable: $evfile (fail-closed)"; return 1; }
  oom="$(awk '/^oom_kill /{print $2}' "$evfile" 2>/dev/null)"
  [ -n "$oom" ] || { log "oom_kill missing in $evfile (fail-closed)"; return 1; }
  [ "$oom" = "0" ] || { log "prior OOM kill=$oom in cgroup"; return 1; }
  return 0
}

# ── EXACT listener endpoints + authenticated healthz, PID-bound ─────────────
# Two expectation profiles from the SINGLE original env parse:
#   original — the pre-deploy endpoints (OLD_NW_*): used by readiness AND by
#              rollback verification (the restored old bot must match what it
#              actually ran BEFORE the deploy — never the forward values);
#   forward  — the endpoints the new code will serve after our allow-listed
#              edits (NW_* incl. the BIND2 we write): used by post-restart health.
# OLD_NW_* also embodies the bot's documented defaults (127.0.0.1:9091, enabled)
# for keys absent from .env — i.e. the derived snapshot of the actual pre-deploy
# endpoint set; we never guess beyond those documented defaults.
check_listeners() {   # check_listeners <expected_pid> <profile: original|forward>
  local want_pid="${1:-}" profile="${2:-forward}"
  local en bind bind2 port
  if [ "$profile" = "original" ]; then
    en="$OLD_NW_ENABLED"; bind="$OLD_NW_BIND"; bind2="$OLD_NW_BIND2"; port="$OLD_NW_PORT"
  else
    en="$NW_ENABLED"; bind="$NW_BIND"; bind2="$NW_BIND2"; port="$NW_PORT"
  fi
  if [ -n "${DEPLOY_LISTENER_CMD:-}" ]; then "$DEPLOY_LISTENER_CMD" "$want_pid" "$profile"; return $?; fi
  [ "$en" = "true" ] || { log "listeners($profile): disabled by env (skipping by contract)"; return 0; }
  local out line body code
  out="$(ss -ltnp 2>/dev/null || true)"
  _endpoint_ok() {   # _endpoint_ok <ip> <port> <pid>
    local ip="$1" port="$2" pid="$3" hit
    # EXACT endpoint — a wildcard or different address must NOT satisfy it
    hit="$(printf '%s\n' "$out" | grep -F " ${ip}:${port} " || true)"
    [ -n "$hit" ] || { log "listener missing exact endpoint ${ip}:${port}"; return 1; }
    if [ -n "$pid" ] && printf '%s' "$out" | grep -q "pid="; then
      printf '%s\n' "$hit" | grep -q "pid=${pid}," || \
        { log "endpoint ${ip}:${port} owned by a different process (want pid=$pid)"; return 1; }
    fi
    return 0
  }
  _endpoint_ok "$bind" "$port" "$want_pid" || return 1
  if [ -n "$bind2" ]; then
    _endpoint_ok "$bind2" "$port" "$want_pid" || return 1
  fi
  # the bot's own health contract — an unrelated listener cannot fake this
  body="$(curl -s --max-time 10 "http://$bind:$port/healthz" 2>/dev/null || true)"
  printf '%s' "$body" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' \
    || { log "healthz contract failed on $bind:$port"; return 1; }
  return 0
}

# ── OpenRouter: authenticated + minimally valid catalog; never leak the key ──
check_openrouter() {
  if [ -n "${DEPLOY_OPENROUTER_CMD:-}" ]; then "$DEPLOY_OPENROUTER_CMD"; return $?; fi
  local key code bodyf
  key="$(env_get OPENROUTER_API_KEY)"
  [ -n "$key" ] || { log "OpenRouter key empty"; return 1; }
  bodyf="$SCRATCH/or_body.json"
  code="$(curl -s -o "$bodyf" -w '%{http_code}' --max-time 15 \
          -H "Authorization: Bearer $key" https://openrouter.ai/api/v1/models 2>/dev/null || echo 000)"
  [ "$code" = "200" ] || { log "OpenRouter authenticated check http=$code"; return 1; }
  "$PYTHON" - "$bodyf" <<'PY' || { log "OpenRouter catalog payload invalid/empty"; return 1; }
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
data=d.get("data")
sys.exit(0 if isinstance(data,list) and len(data)>0 else 1)
PY
  rm -f "$bodyf"
  return 0
}

# ── service user: MUST exist; probes as that user; content contracts ────────
run_as_svc() {
  if [ "$SERVICE_USER" = "root" ]; then "$@"; else sudo -n -u "$SERVICE_USER" -H "$@"; fi
}
resolve_service_user() {
  [ -n "$SERVICE_USER" ] || SERVICE_USER="$($SYSTEMCTL show "$SERVICE" -p User --value 2>/dev/null || echo '')"
  [ -n "$SERVICE_USER" ] || SERVICE_USER="root"   # systemd User= empty means root (by definition)
  SVC_UID="$(id -u "$SERVICE_USER" 2>/dev/null)" || die "service user '$SERVICE_USER' does not exist (no UID fallback)"
  SVC_GID="$(id -g "$SERVICE_USER" 2>/dev/null)" || die "service group for '$SERVICE_USER' unresolvable"
  [ -n "$SVC_UID" ] && [ -n "$SVC_GID" ] || die "service uid/gid empty for '$SERVICE_USER'"
  if [ "$SERVICE_USER" != "root" ]; then
    sudo -n -u "$SERVICE_USER" true 2>/dev/null || die "noninteractive sudo -u $SERVICE_USER unavailable (required for probes)"
  fi
  log "service user resolved: $SERVICE_USER (uid=$SVC_UID gid=$SVC_GID)"
}
_codex_expected_ids() {
  local ov; ov="$(env_get BOT_CODEX_MODELS)"
  if [ -n "$ov" ]; then
    printf '%s' "$ov" | tr ',' '\n' | cut -d: -f1 | sed '/^$/d'
  else
    printf '%s\n' gpt-5.6-sol gpt-5.6-terra gpt-5.6-luna
  fi
}
check_service_cli() {
  local claude_bin codex_bin out id
  claude_bin="${DEPLOY_CLAUDE_CMD:-claude}"; codex_bin="${DEPLOY_CODEX_CMD:-codex}"
  # ── Claude: DETERMINISTIC, read-only CLI contracts ONLY.
  # `claude models` is NOT a real subcommand — Claude Code interprets it as a
  # PROMPT and returns nondeterministic LLM-generated text (and consumes a model
  # call). It is therefore BANNED from readiness: a text answer can never pass
  # or fail this gate. Capability is proven via documented, non-generative
  # contracts, run as the service user, with zero model consumption:
  #   1. `claude --version`            -> the ENTIRE output must be exactly a
  #      semver, optionally suffixed " (Claude Code)" — never merely contain one
  #   2. `claude --help`               -> documents --model and --print
  #   3. `claude auth status --json`   -> the ENTIRE output must be one valid
  #      JSON object whose top-level `loggedIn` is boolean true (strict parse,
  #      never grep; output is never printed)
  _claude_run() {
    if [ -n "${DEPLOY_CLAUDE_CMD:-}" ]; then "$claude_bin" "$@" 2>&1
    else run_as_svc "$claude_bin" "$@" 2>&1; fi
  }
  local ver_re='^[0-9]+\.[0-9]+\.[0-9]+( \(Claude Code\))?$'
  out="$(_claude_run --version)" || { log "claude --version failed"; return 1; }
  [[ "$out" =~ $ver_re ]] \
    || { log "claude --version output is not a version (contract failed)"; return 1; }
  out="$(_claude_run --help)" || { log "claude --help failed"; return 1; }
  printf '%s' "$out" | grep -qF -- '--model' || { log "claude --help lacks --model (contract failed)"; return 1; }
  printf '%s' "$out" | grep -qF -- '--print' || { log "claude --help lacks --print (contract failed)"; return 1; }
  out="$(_claude_run auth status --json)" || { log "claude auth status failed"; return 1; }
  printf '%s' "$out" | "$PYTHON" -c '
import json, sys
try:
    doc = json.loads(sys.stdin.read())
except ValueError:
    sys.exit(1)
sys.exit(0 if isinstance(doc, dict) and doc.get("loggedIn") is True else 1)
' || { log "claude not authenticated (auth status contract failed)"; return 1; }
  # Catalog validity comes from the DETERMINISTIC adapter source + the validated
  # BOT_CLAUDE_MODELS override from the single env parse — never from LLM text,
  # and no Claude request is made.
  local ov
  ov="$(env_get BOT_CLAUDE_MODELS)"
  BOT_CLAUDE_MODELS_OVERRIDE="$ov" PYTHONPATH="$SOURCE" PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" - <<'PYCAT' || { log "claude catalog validation failed (adapter source / BOT_CLAUDE_MODELS)"; return 1; }
import os, sys
from engines import claude_adapter as ca
ids = [m.id for m in ca._DEFAULT_CATALOG if m.id]
if not ids:
    sys.exit("adapter default catalog empty")
ov = os.environ.get("BOT_CLAUDE_MODELS_OVERRIDE", "").strip()
if ov:
    pairs = [p for p in ov.split(",") if p.strip()]
    if not pairs:
        sys.exit("BOT_CLAUDE_MODELS set but contains no entries")
    for p in pairs:
        mid = p.split(":", 1)[0].strip()
        if not mid:
            sys.exit(f"BOT_CLAUDE_MODELS malformed entry: {p!r}")
print("claude-catalog-ok")
PYCAT
  # Codex: authenticated login, model discovery, exec/resume contracts
  if [ -n "${DEPLOY_CODEX_CMD:-}" ]; then
    out="$("$codex_bin" login status 2>&1)" || { log "codex login status failed"; return 1; }
  else
    out="$(run_as_svc "$codex_bin" login status 2>&1)" || { log "codex login status failed (svc user)"; return 1; }
  fi
  # "Not logged in" contains "logged in" — reject the negative form explicitly
  printf '%s' "$out" | grep -qiE 'not[ -]+logged[ -]?in' && { log "codex not authenticated"; return 1; }
  printf '%s' "$out" | grep -qiE 'logged[ -]?in' || { log "codex not authenticated"; return 1; }
  if [ -n "${DEPLOY_CODEX_CMD:-}" ]; then out="$("$codex_bin" debug models 2>&1)" || { log "codex debug models failed"; return 1; }
  else out="$(run_as_svc "$codex_bin" debug models 2>&1)" || { log "codex debug models failed (svc user)"; return 1; }; fi
  while IFS= read -r id; do
    printf '%s' "$out" | grep -qF "$id" || { log "codex catalog missing expected id: $id"; return 1; }
  done < <(_codex_expected_ids)
  if [ -n "${DEPLOY_CODEX_CMD:-}" ]; then out="$("$codex_bin" exec --help 2>&1)" || { log "codex exec --help failed"; return 1; }
  else out="$(run_as_svc "$codex_bin" exec --help 2>&1)" || { log "codex exec --help failed (svc user)"; return 1; }; fi
  printf '%s' "$out" | grep -qF -- '--json' || { log "codex exec lacks --json contract"; return 1; }
  if [ -n "${DEPLOY_CODEX_CMD:-}" ]; then out="$("$codex_bin" exec resume --help 2>&1)" || { log "codex exec resume --help failed"; return 1; }
  else out="$(run_as_svc "$codex_bin" exec resume --help 2>&1)" || { log "codex exec resume --help failed (svc user)"; return 1; }; fi
  printf '%s' "$out" | grep -qiE 'session[_ -]?id' || { log "codex resume lacks session-id contract"; return 1; }
  return 0
}

gate_readiness() {
  fail_at gate_readiness && die "forced: gate_readiness"
  $SYSTEMCTL is-active --quiet "$SERVICE" || die "bot service not active"
  OLD_PID="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  resolve_service_user
  _require_zero_sessions "readiness"
  check_capacity   || die "capacity/OOM readiness failed"
  check_listeners "$OLD_PID" original || die "NightWatch listener readiness failed"
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

# ── SECURE atomic editors (O_EXCL|O_NOFOLLOW random temp, fsync, preserve) ──
set_env_key() {
  local key="$1" val="$2"
  case " $ALLOWLIST " in *" $key "*) : ;; *) die "env key '$key' not allow-listed";; esac
  "$PYTHON" - "$ENV_FILE" "$key" "$val" <<'PY' || die "env edit failed for $key"
import os, secrets, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
if "\n" in val or "\r" in val:
    sys.exit("value has newline")
if os.path.islink(path):
    sys.exit(".env is a symlink; refusing to edit through it")
with open(path, "rb") as f:
    data = f.read()
lines, i, n = [], 0, len(data)
while i < n:
    j = data.find(b"\n", i)
    if j == -1:
        lines.append(data[i:]); break
    lines.append(data[i:j+1]); i = j+1
kb = key.encode()
def is_key(ln):
    s = ln.lstrip(b" \t")
    if s.startswith(b"export ") or s.startswith(b"export\t"):
        s = s[7:].lstrip(b" \t")
    return s.startswith(kb + b"=")
hits = [idx for idx, ln in enumerate(lines) if is_key(ln)]
if len(hits) > 1:
    sys.exit(f"duplicate allow-listed key {key} (export/plain forms count as one; ambiguous)")
newval = val.encode()
if len(hits) == 1:
    idx = hits[0]; ln = lines[idx]
    term = b""; body = ln
    while body[-1:] in (b"\n", b"\r"):
        term = body[-1:] + term; body = body[:-1]
    lead = body[:len(body) - len(body.lstrip(b" \t"))]
    # preserve an existing `export ` prefix verbatim
    rest = body[len(lead):]
    prefix = b""
    if rest.startswith(b"export ") or rest.startswith(b"export\t"):
        prefix = rest[:7]
        rest2 = rest[7:]
        extra = rest2[:len(rest2) - len(rest2.lstrip(b" \t"))]
        prefix += extra
    lines[idx] = lead + prefix + kb + b"=" + newval + term
else:
    # Documented sole structural change: appending a key to a file with no final
    # newline first terminates the last line (dominant newline style), then
    # appends `KEY=value` + newline. All other bytes are preserved verbatim.
    crlf = sum(1 for ln in lines if ln.endswith(b"\r\n"))
    lf   = sum(1 for ln in lines if ln.endswith(b"\n")) - crlf
    nl = b"\r\n" if crlf > lf else b"\n"
    if lines and not lines[-1].endswith(b"\n"):
        lines[-1] = lines[-1] + nl
    lines.append(kb + b"=" + newval + nl)
out = b"".join(lines)
st = os.stat(path)
d = os.path.dirname(path) or "."
tmp = os.path.join(d, f".{secrets.token_hex(8)}.deploytmp")
def _write_all(fd, data):
    # os.write may legally write fewer bytes than requested — loop to completion
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    _write_all(fd, out)
    os.fchmod(fd, st.st_mode & 0o7777)
    os.fchown(fd, st.st_uid, st.st_gid)      # preservation REQUIRED: failure aborts
    os.fsync(fd)
finally:
    os.close(fd)
try:
    os.replace(tmp, path)
except BaseException:
    try: os.unlink(tmp)
    except OSError: pass
    raise
dfd = os.open(d, os.O_RDONLY)
try: os.fsync(dfd)
finally: os.close(dfd)
PY
}

# ─────────────────────── mutation stages ───────────────────────────────────
install_artifacts() {
  fail_at install_artifacts && die "forced: install_artifacts"
  mkdir -p "$LIVE_SCRIPTS" "$SYSTEMD_DIR" "$CONFIGS"
  local line dest src typ
  while IFS= read -r line; do
    dest="${line%%|*}"; src="${line#*|}"; typ="${src##*|}"; src="${src%|*}"
    case "$typ" in
      dir)
        [ -d "$src" ] || die "source dir missing: $src"
        tree_symlinks_safe "$src" || die "source tree has escaping symlinks: $src"
        rm -rf -- "$dest"; cp -a "$src" "$dest"
        [ "$(tree_hash "$src")" = "$(tree_hash "$dest")" ] || die "tree manifest mismatch after install: $dest"
        ;;
      render)
        [ -f "$src" ] || die "source unit missing: $src"
        # render the service unit to the RESOLVED retention config/script/guard
        sed -e "s|^EnvironmentFile=.*$|EnvironmentFile=-$R_RETENTION_ENV|" \
            -e "s|^Environment=REPORTS_ROOT_GUARD=.*$|Environment=REPORTS_ROOT_GUARD=$LIVE_ROOT/reports|" \
            -e "s|^ExecStart=.*$|ExecStart=$LIVE_SCRIPTS/cleanup-reports.sh|" \
            "$src" > "$SCRATCH/unit.rendered"
        install -D -m 0644 "$SCRATCH/unit.rendered" "$dest"
        grep -qF "EnvironmentFile=-$R_RETENTION_ENV" "$dest" || die "rendered unit missing resolved EnvironmentFile"
        grep -qF "ExecStart=$LIVE_SCRIPTS/cleanup-reports.sh" "$dest" || die "rendered unit missing resolved ExecStart"
        grep -qF "REPORTS_ROOT_GUARD=$LIVE_ROOT/reports" "$dest" || die "rendered unit missing resolved root guard"
        ;;
      *)
        [ -f "$src" ] || die "source file missing: $src"
        install -D -m 0644 "$src" "$dest"
        [ "$(sha "$dest")" = "$(sha "$src")" ] || die "hash mismatch after install: $dest"
        ;;
    esac
    [ -e "$dest" ] || die "install failed: $dest"
  done < <(artifacts)
  # transaction safety: the installed timer must NOT be persistent (a catch-up
  # would fire real cleanup inside the rollback-capable window)
  grep -qiE '^Persistent=true' "$SYSTEMD_DIR/reports-cleanup.timer" \
    && die "timer is Persistent=true (catch-up could run cleanup mid-transaction)"
  chmod 0755 "$LIVE_SCRIPTS/cleanup-reports.sh" "$LIVE_SCRIPTS/coord_publish.py" || die "chmod helpers failed"
  # runtime dirs + EVERY existing mutable artifact owned by the service user
  mkdir -p "$R_CHAT" "$(dirname "$R_COORD")" "$(dirname "$R_CACHE")"
  chmod 0700 "$R_CHAT" || die "chmod chat dir failed"
  chown "$SVC_UID:$SVC_GID" "$R_CHAT" || die "chown chat dir failed"
  chown "$SVC_UID:$SVC_GID" "$(dirname "$R_COORD")" || die "chown coord dir failed"
  chown "$SVC_UID:$SVC_GID" "$(dirname "$R_CACHE")" || die "chown cache dir failed"
  local f
  for f in "$R_COORD" "$R_COORDLOCK" "$R_CACHE" "$R_STATE"; do
    if [ -f "$f" ]; then chown "$SVC_UID:$SVC_GID" "$f" || die "chown existing artifact failed: $f"; fi
  done
  log "artifacts installed (tree-manifest verified) + service-user ownership applied"
}

edit_env() {
  fail_at edit_env && die "forced: edit_env"
  set_env_key BOT_MAX_SESSIONS 9
  set_env_key BOT_MAX_RUNNING_AGENTS 2
  set_env_key BOT_NIGHTWATCH_IPC_BIND2 10.108.0.4
  # the post-restart listener expectation = validated parse PLUS our own applied
  # allow-listed edit (BIND2 is now guaranteed for the new process)
  NW_BIND2="10.108.0.4"
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
    "$PYTHON" - "$NIGHTLY" <<'PY' || die "nightly-cleanup migration failed (ambiguous or unsafe)"
import os, re, secrets, sys
p=sys.argv[1]
if os.path.islink(p):
    sys.exit("nightly-cleanup is a symlink; refusing")
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
for ln in out:
    if isdel(ln): sys.exit("active report-deletion remains after migration")
st=os.stat(p)
d=os.path.dirname(p) or "."
tmp=os.path.join(d, f".{secrets.token_hex(8)}.deploytmp")
def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]
fd=os.open(tmp, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
try:
    _write_all(fd, b"".join(out))
    os.fchmod(fd, st.st_mode & 0o7777)
    os.fchown(fd, st.st_uid, st.st_gid)     # preservation REQUIRED
    os.fsync(fd)
finally:
    os.close(fd)
try:
    os.replace(tmp, p)
except BaseException:
    try: os.unlink(tmp)
    except OSError: pass
    raise
dfd=os.open(d, os.O_RDONLY)
try: os.fsync(dfd)
finally: os.close(dfd)
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
  # service-user read/write/ATOMIC-REPLACE probes in every mutable runtime dir
  local d
  for d in "$R_CHAT" "$(dirname "$R_COORD")" "$(dirname "$R_CACHE")"; do
    run_as_svc "$PYTHON" - "$d" <<'PY' || die "service-user write/replace probe failed in $d"
import os, secrets, sys
d = sys.argv[1]
a = os.path.join(d, f".{secrets.token_hex(6)}.probe")
b = os.path.join(d, f".{secrets.token_hex(6)}.probe")
fd = os.open(a, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
data = memoryview(b"probe")
while data:
    data = data[os.write(fd, data):]
os.fsync(fd); os.close(fd)
os.replace(a, b)                       # atomic replace capability
assert open(b, "rb").read() == b"probe"
os.unlink(b)
PY
  done
  # existing mutable files must be replaceable by the service (owner check)
  local f o
  for f in "$R_COORD" "$R_COORDLOCK" "$R_CACHE"; do
    if [ -f "$f" ]; then
      o="$(stat -c '%u' "$f")"
      [ "$o" = "$SVC_UID" ] || die "mutable artifact not service-owned after install: $f (uid=$o)"
    fi
  done
  log "live-dir smoke + service-user write/replace probes OK"
}

_journal_new_pid() {   # journal evidence for the NEW process only (cursor + _PID)
  # NO failure suppression: a journalctl error propagates (rc!=0) so the caller
  # can distinguish "command failed" from "no lines yet" (fail-closed contract)
  $JOURNALCTL -u "$SERVICE" _PID="$NEW_PID" --after-cursor="$RESTART_CURSOR" --no-pager 2>/dev/null
}

# ── Gap 2: QUIESCENCE BOUNDARY — the running bot is stopped BEFORE the first
# production file mutation, so it can never admit a queued/running turn during
# the transaction and can never execute under mixed old/new code. Design:
# carefully trapped service quiesce (not a bot-honored gate — no bot redesign):
#   * the EXIT trap is already installed (crash-aware): any failure after the
#     stop either restores availability (pre-mutation) or performs the verified
#     rollback (post-mutation), each starting the old bot exactly once;
#   * after stop we verify the unit is inactive (systemd KillMode guarantees no
#     surviving children of the stopped unit) and re-read the AUTHORITATIVE
#     state — a turn admitted during shutdown aborts before any mutation;
#   * only THEN is the state backup captured (build_manifest runs after this),
#     so rollback can never overwrite newer session state with a stale copy;
#   * success starts the new bot exactly once (single old→new transition);
#     no restart loops anywhere.
# If the deploy process itself is SIGKILLed mid-transaction (no trap), the
# backup + MANIFEST.tsv remain on disk for manual recovery — documented.
quiesce_service() {
  fail_at quiesce_service && die "forced: quiesce_service"
  _require_zero_sessions "quiesce (pre-stop)"
  QUIESCED=1                    # from here the trap must restore availability
  $SYSTEMCTL stop "$SERVICE" || die "service stop (quiesce) failed"
  QUIESCE_STOPS=$((QUIESCE_STOPS+1))
  $SYSTEMCTL is-active --quiet "$SERVICE" && die "service still active after quiesce stop"
  # authoritative post-stop recheck: no turn slipped in during shutdown; if one
  # did, abort BEFORE any mutation — its state is preserved, nothing is lost
  _require_zero_sessions "quiesce (authoritative post-stop)"
  log "service quiesced; no admission window exists during the mutation phase"
}

start_new() {
  fail_at start_new && die "forced: start_new"
  # final authoritative recheck before new-code start (bot is stopped; state
  # cannot legally change, but verify rather than assume)
  _require_zero_sessions "pre-start"
  # MANDATORY cursor: empty/failed capture is a pre-start failure (no stale fallback)
  RESTART_CURSOR="$($JOURNALCTL -u "$SERVICE" --show-cursor -n0 --no-pager 2>/dev/null \
                    | sed -n 's/^-- cursor: //p' | tail -1)"
  [ -n "$RESTART_CURSOR" ] || die "journal cursor capture failed (mandatory; refusing to start)"
  $SYSTEMCTL daemon-reload || die "daemon-reload failed"
  $SYSTEMCTL start "$SERVICE" || die "start failed"
  RESTARTED_FWD=$((RESTARTED_FWD+1))
  NEW_PID="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ -n "$NEW_PID" ] && [ "$NEW_PID" != "0" ] && [ "$NEW_PID" != "$OLD_PID" ] || die "no new MainPID after start"
  log "single old->new transition: pid $OLD_PID -> $NEW_PID (cursor captured)"
}

_health_assert() {   # one full health assertion against NEW-PID evidence only
  local ctx="$1" jrl pid names
  pid="$($SYSTEMCTL show "$SERVICE" -p MainPID --value 2>/dev/null || echo '')"
  [ "$pid" = "$NEW_PID" ] || { log "$ctx: PID changed ($NEW_PID -> $pid)"; return 2; }
  $SYSTEMCTL is-active --quiet "$SERVICE" || { log "$ctx: service not active"; return 2; }
  if ! jrl="$(_journal_new_pid)"; then
    LAST_HEALTH_ERR="journal"
    log "$ctx: journal retrieval FAILED (retryable during startup; persistent failure is fail-closed)"
    return 1
  fi
  LAST_HEALTH_ERR=""
  if printf '%s' "$jrl" | grep -qiE 'traceback|fatal'; then
    log "$ctx: fatal/traceback in NEW-pid journal"; return 2
  fi
  printf '%s' "$jrl" | grep -q 'Bot v4 ready' || { log "$ctx: ready marker not yet seen (new pid)"; return 1; }
  printf '%s' "$jrl" | grep -Eq 'max_sessions=9' && printf '%s' "$jrl" | grep -Eq 'max_running_agents=2' \
     || { log "$ctx: effective limits not 9/2"; return 1; }
  names="$(printf '%s' "$jrl" | sed -n 's/.*engines\.registered names=//p' | tail -1 | tr -d '[:space:]')"
  [ "$names" = "chat,claude,codex" ] || { log "$ctx: engine set mismatch (got '$names', want exactly chat,claude,codex)"; return 2; }
  check_listeners "$NEW_PID" forward || { log "$ctx: exact listeners unhealthy"; return 1; }
  check_capacity  || { log "$ctx: capacity/cgroup unstable"; return 2; }
  local d
  for d in "$R_CHAT" "$(dirname "$R_COORD")" "$(dirname "$R_CACHE")"; do
    if [ "$SERVICE_USER" != "root" ]; then
      sudo -n -u "$SERVICE_USER" test -w "$d" || { log "$ctx: $d not writable by svc user"; return 2; }
    else
      [ -w "$d" ] || { log "$ctx: $d not writable"; return 2; }
    fi
  done
  return 0
}

health_check() {
  fail_at health_check && die "forced: health_check"
  # bounded startup polling: retry transient (rc=1) conditions until deadline;
  # hard (rc=2) conditions — fatal log, PID churn, engine mismatch — abort now.
  local waited=0 rc
  while :; do
    set +e; _health_assert "health"; rc=$?; set -e
    [ "$rc" -eq 0 ] && break
    [ "$rc" -eq 2 ] && die "post-restart health failed (hard condition)"
    if [ "$waited" -ge "$HEALTH_DEADLINE" ]; then
      [ "$LAST_HEALTH_ERR" = "journal" ] \
        && die "journal retrieval persistently failing (fail-closed; distinct from not-ready)"
      die "post-restart health not ready within ${HEALTH_DEADLINE}s"
    fi
    sleep "$HEALTH_POLL"; waited=$((waited+HEALTH_POLL))
  done
  log "health OK (new pid $NEW_PID) after ${waited}s: ready, 9/2, exact engines, exact listeners, paths"
}

observe() {
  fail_at observe && die "forced: observe"
  [ "$OBSERVE" -gt 0 ] && sleep "$OBSERVE"
  local rc
  set +e; _health_assert "observe"; rc=$?; set -e
  [ "$rc" -eq 0 ] || die "post-observation health failed (PID churn or degraded)"
  log "observation window ($OBSERVE s) passed; PID stable at $NEW_PID"
}

enable_timer() {
  fail_at enable_timer && die "forced: enable_timer"
  # pre-activation verification: exact unit config, script hash, root guard
  local unit="$SYSTEMD_DIR/reports-cleanup.service"
  grep -qF "EnvironmentFile=-$R_RETENTION_ENV" "$unit" || die "unit EnvironmentFile != resolved retention config"
  grep -qF "ExecStart=$LIVE_SCRIPTS/cleanup-reports.sh" "$unit" || die "unit ExecStart != stable live cleanup path"
  grep -qF "REPORTS_ROOT_GUARD=$LIVE_ROOT/reports" "$unit" || die "unit root guard != resolved reports root"
  [ -f "$R_RETENTION_ENV" ] || die "resolved retention config missing: $R_RETENTION_ENV"
  [ "$(sha "$LIVE_SCRIPTS/cleanup-reports.sh")" = "$(sha "$SOURCE/scripts/cleanup-reports.sh")" ] \
    || die "installed cleanup script hash != source"
  grep -qiE '^Persistent=true' "$SYSTEMD_DIR/reports-cleanup.timer" \
    && die "timer became Persistent=true before activation"
  $SYSTEMCTL enable reports-cleanup.timer >/dev/null 2>&1 || die "timer enable failed"
  $SYSTEMCTL start  reports-cleanup.timer >/dev/null 2>&1 || die "timer start failed"
  TIMER_ACTIVATED=1
  $SYSTEMCTL is-enabled --quiet reports-cleanup.timer || die "timer not enabled"
  $SYSTEMCTL is-active  --quiet reports-cleanup.timer || die "timer not active"
  local ex; ex="$($SYSTEMCTL show reports-cleanup.service -p ExecStart --value 2>/dev/null || echo '')"
  [ -n "$ex" ] || die "timer ExecStart is empty"
  case "$ex" in *"$LIVE_SCRIPTS/cleanup-reports.sh"*) : ;; *) die "timer ExecStart not the stable live path: $ex";; esac
  log "retention timer activated at commit boundary; ExecStart=$ex"
}

# ─────────────────────── main ──────────────────────────────────────────────
if [ "$MODE" = "setenv" ]; then
  # test/verify utility: run THE real secure editor against an arbitrary file
  # (used by the offline partial-write regression); allow-list still enforced
  [ -n "$SET_FILE" ] && [ -n "$SET_KEY" ] || die "--set-env-key <file> <key> <value>"
  case "$(readlink -f "$SET_FILE" 2>/dev/null || echo "$SET_FILE")" in
    /tmp/*) : ;; *) die "--set-env-key target must be under /tmp (test utility only)";;
  esac
  ENV_FILE="$SET_FILE"
  set_env_key "$SET_KEY" "$SET_VAL"
  exit 0
fi

parse_env_once
resolve_paths
gate_source
gate_suite
gate_readiness

if [ "$MODE" = "preflight" ]; then
  log "PREFLIGHT complete — read-only (no Git-metadata/live writes), no lock, scratch under /tmp"
  exit 0
fi

# ── Gap 1: reviewed-tree AUTHORIZATION gate (squash/merge-safe) ─────────────
# Typing --reviewed-pr 19 is not authorization. Execute mode must prove the
# merged SOURCE TREE is byte-identical to the owner-authorized reviewed tree:
# commit-SHA equality is insufficient because squash merge rewrites the commit.
gate_reviewed_identity() {
  fail_at gate_reviewed_identity && die "forced: gate_reviewed_identity"
  echo "$REVIEWED_HEAD" | grep -Eq '^[0-9a-f]{40}$' \
    || die "reviewed head must be 40 hex (--reviewed-head; owner-authorized PR head)"
  echo "$REVIEWED_TREE" | grep -Eq '^[0-9a-f]{40}$' \
    || die "reviewed tree must be 40 hex (--reviewed-tree; owner-authorized tree id)"
  MERGED_TREE="$($GITCMD -C "$SOURCE" rev-parse "${MERGED_SHA}^{tree}" 2>/dev/null | head -1)"
  [ -n "$MERGED_TREE" ] || die "cannot resolve merged tree for $MERGED_SHA"
  [ "$MERGED_TREE" = "$REVIEWED_TREE" ] \
    || die "merged tree ($MERGED_TREE) != owner-authorized reviewed tree ($REVIEWED_TREE) — squash-safe gate"
  # If the PR head branch still exists remotely, its head must equal the
  # reviewed head (read-only ls-remote; never a fetch). DOCUMENTED CASE: if the
  # branch was deleted after merge, ls-remote returns nothing and the reviewed-
  # TREE equality above remains the decisive, immutable authorization.
  local ref lrout
  ref="refs/heads/claude/phase2-engines-codex-chat"
  lrout="$($GITCMD -C "$SOURCE" ls-remote origin "$ref" 2>/dev/null | awk 'NR==1{print $1}')"
  if [ -n "$lrout" ]; then
    [ "$lrout" = "$REVIEWED_HEAD" ] \
      || die "live PR branch head ($lrout) != reviewed head ($REVIEWED_HEAD) — stale PR branch"
    log "reviewed-identity OK: live branch head == reviewed head; merged tree == reviewed tree"
  else
    log "reviewed-identity OK: PR branch deleted post-merge; decisive gate = reviewed-tree equality"
  fi
}

[ "$REVIEWED_PR" = "19" ] || die "reviewed PR must be 19"
gate_reviewed_identity
exec 9>"$LOCK"; flock -n 9 || die "another deploy holds the lock"

quiesce_service      # Gap 2: quiescence boundary BEFORE any file mutation
build_manifest       # backup captures the AUTHORITATIVE post-quiesce state
ARMED=1

install_artifacts
edit_env
migrate_retention
verify_install
start_new            # the single old->new service transition
health_check
observe
enable_timer         # final commit boundary — timer only now

exit 0
