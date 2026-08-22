#!/usr/bin/env python3
"""Garbage collect orphan claude-session worktrees older than 24h.

A worktree at WORKTREE_ROOT/<session_id> normally cleans itself up via
WorktreeSession.__exit__, but a hard crash (OOM, kill -9, host reboot
mid-spawn) can leave a directory plus a registered git-worktree entry in
the parent repo. This GC scans WORKTREE_ROOT, identifies the owning repo
(by querying every repo's `git worktree list`), and runs `git worktree
remove --force` followed by `rm -rf` as a fallback.

**Active-session protection**: Before removing, checks:
1. Latest commit timestamp in worktree (skip if < AGE_THRESHOLD)
2. Latest file modification in worktree tree (skip if < AGE_THRESHOLD)
3. Presence of `.active` marker file (skip if exists)

Run via systemd timer (preferred) or cron.
"""
import os
import subprocess
import time
import glob

WORKTREE_ROOT = os.environ.get("BOT_WORKTREE_ROOT", "/tmp/claude-sessions")
REPOS = os.environ.get("BOT_REPOS", "/opt/shahrzad-devops/repos")
AGE_THRESHOLD_SECONDS = 24 * 3600

now = time.time()
cleaned = 0
failed = 0
skipped_young = 0
skipped_active = 0

if not os.path.isdir(WORKTREE_ROOT):
    print(f"worktree-gc: WORKTREE_ROOT not present ({WORKTREE_ROOT}) — nothing to do")
    raise SystemExit(0)


def is_active_session(worktree_path: str) -> bool:
    """Check if worktree shows signs of recent activity."""
    # 1. Check for explicit active marker
    if os.path.exists(os.path.join(worktree_path, ".active")):
        return True

    # 2. Check latest commit timestamp
    try:
        result = subprocess.run(
            ["git", "-C", worktree_path, "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            commit_ts = int(result.stdout.strip())
            if (now - commit_ts) < AGE_THRESHOLD_SECONDS:
                return True
    except Exception:
        pass

    # 3. Check for recent file modifications (anywhere in worktree)
    try:
        result = subprocess.run(
            ["find", worktree_path, "-type", "f", "-not", "-path", "*/.git/*",
             "-mtime", "-1", "-print", "-quit"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        pass

    return False


for path in glob.glob(f"{WORKTREE_ROOT}/*"):
    if not os.path.isdir(path):
        continue
    age = now - os.path.getmtime(path)
    if age < AGE_THRESHOLD_SECONDS:
        skipped_young += 1
        continue

    # Check if worktree is still actively used
    if is_active_session(path):
        skipped_active += 1
        continue

    # Find the owning repo by scanning each repo's worktree registry.
    owner = None
    if os.path.isdir(REPOS):
        for repo in os.listdir(REPOS):
            repo_path = f"{REPOS}/{repo}"
            if not os.path.isdir(f"{repo_path}/.git"):
                continue
            try:
                r = subprocess.run(
                    ["git", "-C", repo_path, "worktree", "list"],
                    capture_output=True, text=True, timeout=10)
            except Exception:
                continue
            if path in r.stdout:
                owner = repo_path
                break

    if owner:
        subprocess.run(
            ["git", "-C", owner, "worktree", "remove", "--force", path],
            capture_output=True, timeout=30)

    result = subprocess.run(["rm", "-rf", path], capture_output=True)
    if result.returncode == 0:
        cleaned += 1
    else:
        failed += 1

print(f"worktree-gc: cleaned={cleaned} failed={failed} skipped_young={skipped_young} skipped_active={skipped_active} root={WORKTREE_ROOT}")
