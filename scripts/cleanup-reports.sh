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
# Usage:  cleanup-reports.sh [--dry-run]
# Env:
#   BOT_REPORT_RETENTION_DAYS  retention in days (default 15; must be >= 1)
#   BOT_DATA_ROOT              default /opt/shahrzad-devops
#   BOT_REPORTS               reports base   (default $BOT_DATA_ROOT/reports)
#   BOT_CONFIGS_DIR           configs dir    (default $BOT_DATA_ROOT/configs)
#   REPORTS_TOKEN_FILE        token env file (default $BOT_CONFIGS_DIR/reports-token.env)
#
# The path token is READ from the token file (key REPORTS_PATH_TOKEN); it is
# never embedded in this script. Exits non-zero on unsafe config or on any
# deletion failure.

set -euo pipefail

RETENTION_DAYS="${BOT_REPORT_RETENTION_DAYS:-15}"
DATA_ROOT="${BOT_DATA_ROOT:-/opt/shahrzad-devops}"
REPORTS_BASE="${BOT_REPORTS:-$DATA_ROOT/reports}"
CONFIGS_DIR="${BOT_CONFIGS_DIR:-$DATA_ROOT/configs}"
TOKEN_FILE="${REPORTS_TOKEN_FILE:-$CONFIGS_DIR/reports-token.env}"

# Hard guard: we will only ever delete UNDER this root, never at/above it.
# Overridable ONLY so the test suite can point at a throwaway tree; production
# never sets it, so the default keeps the guard pinned to the real reports root.
REPORTS_ROOT_GUARD="${REPORTS_ROOT_GUARD:-/opt/shahrzad-devops/reports}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
elif [[ -n "${1:-}" ]]; then
  echo "cleanup-reports: ERROR: unknown argument '$1' (only --dry-run)" >&2
  exit 2
fi

die() { echo "cleanup-reports: ERROR: $*" >&2; exit 2; }

# ── Validate retention ──
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( RETENTION_DAYS >= 1 )) \
  || die "invalid BOT_REPORT_RETENTION_DAYS='$RETENTION_DAYS' (need integer >= 1)"

# ── Load the path token (never embedded) ──
[[ -f "$TOKEN_FILE" ]] || die "token file not found: $TOKEN_FILE"
TOKEN="$(grep -E '^REPORTS_PATH_TOKEN=' "$TOKEN_FILE" | head -n1 | cut -d= -f2- | tr -d ' \t\r\n')"
[[ -n "$TOKEN" ]] || die "REPORTS_PATH_TOKEN missing/empty in $TOKEN_FILE"
[[ "$TOKEN" =~ ^[A-Za-z0-9_-]{16,}$ ]] || die "REPORTS_PATH_TOKEN has unexpected format"

REPORT_DIR="$REPORTS_BASE/$TOKEN"

# ── Path safety: never operate on /, empty, the reports root, or outside it ──
case "$REPORTS_BASE" in
  ""|"/") die "unsafe reports base '$REPORTS_BASE'" ;;
esac
real_base="$(readlink -f "$REPORTS_BASE" 2>/dev/null || echo "$REPORTS_BASE")"
real_dir="$(readlink -f "$REPORT_DIR" 2>/dev/null || echo "$REPORT_DIR")"
[[ "$real_base" == "$REPORTS_ROOT_GUARD" || "$real_base" == "$REPORTS_ROOT_GUARD"/* ]] \
  || die "reports base '$real_base' is outside $REPORTS_ROOT_GUARD"
[[ "$real_dir" == "$REPORTS_ROOT_GUARD"/* ]] \
  || die "report dir '$real_dir' is outside $REPORTS_ROOT_GUARD"
[[ "$real_dir" != "/" && "$real_dir" != "$real_base" && "$real_dir" != "$REPORTS_ROOT_GUARD" ]] \
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
