#!/usr/bin/env bash
#
# cleanup-reports.sh — canonical, repository-owned report retention.
#
# Single source of truth for report TTL, replacing three conflicting live jobs
# (hourly file-delete @24h, nightly dir/zip-delete @1d, dir-delete @15d) that
# left hollow report directories and 404'd Summary/ZIP links. See
# docs/REPORT_RETENTION_MIGRATION.md.
#
# Policy: a report's { Summary + Browse directory + ZIP } stay available
# TOGETHER for BOT_REPORT_RETENTION_DAYS (default 15). Expired report dirs and
# their sibling .zip are removed as a unit; already-hollow historical dirs are
# swept regardless of age. Reports within the TTL are preserved intact.
#
# Usage:  cleanup-reports.sh [--dry-run] [--test-root <DIR under /tmp>]
# Env:
#   BOT_REPORT_RETENTION_DAYS  retention in days (default 15; must be >= 1)
#   BOT_DATA_ROOT              default /opt/shahrzad-devops
#   BOT_REPORTS               reports base   (default $BOT_DATA_ROOT/reports)
#   BOT_CONFIGS_DIR           configs dir    (default $BOT_DATA_ROOT/configs)
#   REPORTS_TOKEN_FILE        token env file (default $BOT_CONFIGS_DIR/reports-token.env)
#   REPORTS_ROOT_GUARD        OPTIONAL cross-check only; if set it MUST equal the
#                             pinned production guard (systemd pins it). It can
#                             confirm the scope but never redefine it.
#
# The production deletion scope is HARDCODED to /opt/shahrzad-devops/reports and
# is NOT freely redefinable by ordinary configuration. Tests may retarget it ONLY
# via the explicit --test-root flag AND only to a path beneath /tmp.
#
# The path token is READ from the token file (key REPORTS_PATH_TOKEN); it is
# never embedded in this script. Exits non-zero on unsafe config or on any
# deletion failure.

set -euo pipefail

die() { echo "cleanup-reports: ERROR: $*" >&2; exit 2; }

# Hardcoded production deletion scope. Never redefined by normal config.
PROD_GUARD="/opt/shahrzad-devops/reports"
GUARD="$PROD_GUARD"

# ── Parse args (order-independent): --dry-run, --test-root <dir> ──
DRY_RUN=0
TEST_ROOT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --test-root)
      shift
      [[ $# -gt 0 ]] || die "--test-root requires a path"
      TEST_ROOT="$1"; shift ;;
    *) die "unknown argument '$1' (only --dry-run / --test-root <dir>)" ;;
  esac
done

RETENTION_DAYS="${BOT_REPORT_RETENTION_DAYS:-15}"
DATA_ROOT="${BOT_DATA_ROOT:-/opt/shahrzad-devops}"
REPORTS_BASE="${BOT_REPORTS:-$DATA_ROOT/reports}"
CONFIGS_DIR="${BOT_CONFIGS_DIR:-$DATA_ROOT/configs}"
TOKEN_FILE="${REPORTS_TOKEN_FILE:-$CONFIGS_DIR/reports-token.env}"

# ── Test-root mode: explicit flag, and ONLY beneath /tmp ──
if [[ -n "$TEST_ROOT" ]]; then
  # 1) Cheap raw-string gate first (unchanged behavior/message): reject
  #    anything that isn't even written as a /tmp path.
  case "$TEST_ROOT" in
    /tmp/*) : ;;
    *) die "--test-root must be an absolute path beneath /tmp (got '$TEST_ROOT')" ;;
  esac
  # 2) Canonicalize BEFORE trusting that raw /tmp prefix. A /tmp/... symlink
  #    can resolve OUTSIDE /tmp entirely (e.g. /tmp/link ->
  #    /opt/shahrzad-devops/reports); readlink -f later would then make
  #    guard==base==the escaped path and every downstream "outside guard"
  #    check would pass. `realpath -m` resolves all symlinks (incl. the final
  #    component) and does not require the leaf to exist, so a not-yet-created
  #    test dir still validates; when unavailable, fall back to canonicalizing
  #    the nearest existing parent so a nonexistent-but-safe path still works.
  canon_root="$(realpath -m -- "$TEST_ROOT" 2>/dev/null || true)"
  if [[ -z "$canon_root" ]]; then
    parent="$(dirname -- "$TEST_ROOT")"
    while [[ ! -e "$parent" && "$parent" != "/" ]]; do parent="$(dirname -- "$parent")"; done
    real_parent="$(readlink -f -- "$parent" 2>/dev/null || echo "$parent")"
    canon_root="${real_parent%/}/${TEST_ROOT#"$parent"/}"
  fi
  [[ -n "$canon_root" ]] || die "--test-root could not be canonicalized ('$TEST_ROOT')"
  case "$canon_root" in
    /tmp)   die "--test-root canonical path is /tmp itself ('$TEST_ROOT' -> '$canon_root')" ;;
    /tmp/*) : ;;
    *)      die "--test-root canonical path escapes /tmp ('$TEST_ROOT' -> '$canon_root')" ;;
  esac
  TEST_ROOT="$canon_root"
  GUARD="$TEST_ROOT"
  REPORTS_BASE="$TEST_ROOT"
  CONFIGS_DIR="${BOT_CONFIGS_DIR:-$TEST_ROOT/configs}"
  TOKEN_FILE="${REPORTS_TOKEN_FILE:-$CONFIGS_DIR/reports-token.env}"
fi

# ── If REPORTS_ROOT_GUARD env is set (systemd pins it), it must MATCH the guard.
# It can confirm the scope; it can never redefine it to something else. ──
if [[ -n "${REPORTS_ROOT_GUARD:-}" && "$REPORTS_ROOT_GUARD" != "$GUARD" ]]; then
  die "REPORTS_ROOT_GUARD='$REPORTS_ROOT_GUARD' does not match pinned guard '$GUARD'"
fi

# ── Validate retention ──
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( RETENTION_DAYS >= 1 )) \
  || die "invalid BOT_REPORT_RETENTION_DAYS='$RETENTION_DAYS' (need integer >= 1)"

# ── Load the path token (never embedded). Controlled errors for missing line /
# empty / malformed — the grep must not trip an uncontrolled pipefail exit. ──
[[ -f "$TOKEN_FILE" ]] || die "token file not found: $TOKEN_FILE"
TOKEN_LINE="$(grep -E '^REPORTS_PATH_TOKEN=' "$TOKEN_FILE" | head -n1 || true)"
[[ -n "$TOKEN_LINE" ]] || die "REPORTS_PATH_TOKEN line not found in $TOKEN_FILE"
TOKEN="$(printf '%s' "$TOKEN_LINE" | cut -d= -f2- | tr -d ' \t\r\n')"
[[ -n "$TOKEN" ]] || die "REPORTS_PATH_TOKEN is empty in $TOKEN_FILE"
[[ "$TOKEN" =~ ^[A-Za-z0-9_-]{16,}$ ]] || die "REPORTS_PATH_TOKEN has unexpected format"

REPORT_DIR="$REPORTS_BASE/$TOKEN"

# ── Path safety ──
case "$GUARD" in
  ""|"/"|"/opt"|"/opt/shahrzad-devops") die "unsafe guard '$GUARD'" ;;
esac
case "$REPORTS_BASE" in
  ""|"/"|"/opt"|"/opt/shahrzad-devops") die "unsafe reports base '$REPORTS_BASE'" ;;
esac
real_guard="$(readlink -f "$GUARD" 2>/dev/null || echo "$GUARD")"
real_base="$(readlink -f "$REPORTS_BASE" 2>/dev/null || echo "$REPORTS_BASE")"
real_dir="$(readlink -f "$REPORT_DIR" 2>/dev/null || echo "$REPORT_DIR")"
# Reject the canonical dangerous roots outright.
case "$real_dir" in
  ""|"/"|"/opt"|"/opt/shahrzad-devops") die "refusing unsafe target '$real_dir'" ;;
esac
[[ "$real_base" == "$real_guard" || "$real_base" == "$real_guard"/* ]] \
  || die "reports base '$real_base' is outside guard '$real_guard'"
[[ "$real_dir" == "$real_guard"/* ]] \
  || die "report dir '$real_dir' is outside guard '$real_guard'"
[[ "$real_dir" != "$real_base" && "$real_dir" != "$real_guard" ]] \
  || die "refusing to operate on the reports root itself ('$real_dir')"
[[ -d "$real_dir" ]] || die "token report dir not found: $real_dir"

# ── Collect targets (null-delimited → safe for spaces/odd names) ──
declare -a expired_dirs=() empty_dirs=() expired_zips=()
while IFS= read -r -d '' p; do expired_dirs+=("$p"); done \
  < <(find "$real_dir" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -print0)
while IFS= read -r -d '' p; do empty_dirs+=("$p"); done \
  < <(find "$real_dir" -mindepth 1 -maxdepth 1 -type d -empty -print0)
while IFS= read -r -d '' p; do expired_zips+=("$p"); done \
  < <(find "$real_dir" -mindepth 1 -maxdepth 1 -type f -name '*.zip' -mtime +"$RETENTION_DAYS" -print0)

retained=$(find "$real_dir" -mindepth 1 -maxdepth 1 -type d -not -mtime +"$RETENTION_DAYS" ! -empty | wc -l | tr -d ' ')

removed_dirs=0 removed_empty=0 removed_zips=0 failed=0
declare -A handled_zip=()   # zip paths already accounted for (dedup dry-run + real)

rm_path() { # $1 = path; respects --dry-run; counts failures
  local p="$1"
  if (( DRY_RUN )); then echo "[dry-run] would remove: $p"; return 0; fi
  if rm -rf -- "$p"; then return 0; else echo "cleanup-reports: FAILED to remove: $p" >&2; return 1; fi
}

# Expired report dirs — remove the dir AND its sibling <dir>.zip as a unit.
if (( ${#expired_dirs[@]} )); then
  for d in "${expired_dirs[@]}"; do
    if rm_path "$d"; then removed_dirs=$((removed_dirs+1)); else failed=$((failed+1)); fi
    z="$d.zip"
    if [[ -f "$z" ]]; then
      handled_zip["$z"]=1
      if rm_path "$z"; then removed_zips=$((removed_zips+1)); else failed=$((failed+1)); fi
    fi
  done
fi

# Orphan expired zips (no matching dir, or dir already gone) not handled above.
if (( ${#expired_zips[@]} )); then
  for z in "${expired_zips[@]}"; do
    [[ -n "${handled_zip[$z]:-}" ]] && continue   # already accounted with its dir
    [[ -e "$z" ]] || continue                     # already removed
    if rm_path "$z"; then removed_zips=$((removed_zips+1)); else failed=$((failed+1)); fi
  done
fi

# Already-hollow historical dirs (any age) — the old-bug residue.
if (( ${#empty_dirs[@]} )); then
  for d in "${empty_dirs[@]}"; do
    [[ -e "$d" ]] || continue   # already removed as expired
    if rm_path "$d"; then removed_empty=$((removed_empty+1)); else failed=$((failed+1)); fi
  done
fi

echo "cleanup-reports: ttl_days=$RETENTION_DAYS dir=$real_dir dry_run=$DRY_RUN"
echo "cleanup-reports: removed_expired_dirs=$removed_dirs removed_zips=$removed_zips removed_empty_dirs=$removed_empty retained_intact=$retained failed=$failed"

if (( failed > 0 )); then
  exit 1
fi
exit 0
