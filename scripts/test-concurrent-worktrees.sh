#!/bin/bash
# Phase B smoke test: verify 5 concurrent worktrees on the same repo do
# not collide and the shared repo's HEAD is untouched. Run AFTER deploy.
set -euo pipefail

REPO="${REPO:-/opt/shahrzad-devops/repos/ClaudeCodeTelegramBot}"
BASE=/tmp/claude-test-$(date +%s)

if [ ! -d "$REPO/.git" ]; then
  echo "FAIL: $REPO is not a git repo" >&2
  exit 1
fi

cleanup() {
  for i in 1 2 3 4 5; do
    git -C "$REPO" worktree remove --force "$BASE/sid-$i" 2>/dev/null || true
    rm -rf "$BASE/sid-$i" 2>/dev/null || true
  done
  rm -rf "$BASE"
}
trap cleanup EXIT

git -C "$REPO" fetch origin --quiet
MAIN=$(git -C "$REPO" rev-parse origin/main)
SHARED_BEFORE=$(git -C "$REPO" rev-parse HEAD)

mkdir -p "$BASE"

PIDS=()
for i in 1 2 3 4 5; do
  WT="$BASE/sid-$i"
  ( git -C "$REPO" worktree add --detach "$WT" origin/main >/dev/null 2>&1 ) &
  PIDS+=($!)
done
for p in "${PIDS[@]}"; do
  wait "$p"
done

# All 5 on origin/main detached?
for i in 1 2 3 4 5; do
  WT="$BASE/sid-$i"
  if [ ! -d "$WT/.git" ] && [ ! -f "$WT/.git" ]; then
    echo "FAIL: worktree $i missing or not a git work tree"
    exit 1
  fi
  HEAD=$(git -C "$WT" rev-parse HEAD)
  if [ "$HEAD" != "$MAIN" ]; then
    echo "FAIL: worktree $i HEAD=$HEAD expected=$MAIN"
    exit 1
  fi
done

# Shared repo HEAD untouched?
SHARED_AFTER=$(git -C "$REPO" rev-parse HEAD)
if [ "$SHARED_BEFORE" != "$SHARED_AFTER" ]; then
  echo "FAIL: shared repo HEAD moved ($SHARED_BEFORE -> $SHARED_AFTER)"
  exit 1
fi

# Touch a file in worktree 1, ensure worktree 2 is unaffected
echo "test" > "$BASE/sid-1/.wt-isolation-marker"
if [ -f "$BASE/sid-2/.wt-isolation-marker" ]; then
  echo "FAIL: marker leaked from sid-1 to sid-2"
  exit 1
fi

echo "PASS: 5 concurrent worktrees verified, shared HEAD untouched, isolation OK"
