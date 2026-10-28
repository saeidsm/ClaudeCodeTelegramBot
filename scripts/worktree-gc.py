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

**Dirty-tree protection (owner R-revisit item 1)**: worktrees now persist
across a session's turns (BOT_WORKTREE_REUSE), so this GC is their main
deletion path. A DIRTY tree is never rm -rf'd — it is QUARANTINED (moved
under $BOT_DATA_ROOT/quarantine/<sid>/stale-<stamp>/ with its git index
metadata copied in) and left for the owner. A tree that cannot be proven
clean, or whose quarantine fails, is skipped — never deleted.

Run via systemd timer (preferred) or cron.
"""
import os
import subprocess
import sys
import time
import glob

WORKTREE_ROOT = os.environ.get("BOT_WORKTREE_ROOT", "/tmp/claude-sessions")
REPOS = os.environ.get("BOT_REPOS", "/opt/shahrzad-devops/repos")
# Single source of truth for "old enough to reap". Every age test below derives
# from this - see is_active_session() check 3, which used to hardcode `-mtime -1`
# and agreed with the old 24h value only by coincidence.
AGE_THRESHOLD_DAYS = int(os.environ.get("BOT_WORKTREE_GC_AGE_DAYS", "8"))
AGE_THRESHOLD_SECONDS = AGE_THRESHOLD_DAYS * 24 * 3600

now = time.time()
cleaned = 0
failed = 0
skipped_young = 0
skipped_active = 0
quarantined_dirty = 0

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

    # 3. Check for recent file modifications (anywhere in worktree).
    #    Derived from AGE_THRESHOLD_SECONDS, not hardcoded. This said `-mtime -1`,
    #    which matched the old 24h threshold BY COINCIDENCE and would have
    #    silently disagreed the moment the threshold moved - which is exactly
    #    what raising it to 8 days does. `-newermt @<epoch>` takes an absolute
    #    cutoff, so it also avoids -mtime's whole-day rounding.
    try:
        cutoff = f"@{int(now - AGE_THRESHOLD_SECONDS)}"
        result = subprocess.run(
            ["find", worktree_path, "-type", "f", "-not", "-path", "*/.git/*",
             "-newermt", cutoff, "-print", "-quit"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        pass

    return False


def _quarantine_dirty(path: str, owner: str) -> str:
    """Move a dirty tree into the quarantine root (files + git index meta),
    reusing mcp_bridge.quarantine_stale_worktree. '' on failure — the
    caller then refuses the destructive path."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import mcp_bridge
        sid = os.path.basename(path).replace(":", "_")
        return mcp_bridge.quarantine_stale_worktree(path, sid, owner or "")
    except Exception:
        return ""


def _is_dirty(path: str):
    """True when the tree has uncommitted/untracked work (or is not a
    regular git worktree at all — treat as untouchable, not as clean)."""
    if not os.path.isfile(os.path.join(path, ".git")):
        return None
    try:
        r = subprocess.run(["git", "-C", path, "status", "--porcelain=v1", "--ignored=matching", "-z"],
                           capture_output=True, timeout=30)
        return bool(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:
        return None


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
            if not os.path.exists(f"{repo_path}/.git"):
                continue
            try:
                r = subprocess.run(
                    ["git", "-C", repo_path, "worktree", "list", "--porcelain"],
                    capture_output=True, text=True, timeout=10)
            except Exception:
                continue
            if r.returncode == 0 and f"worktree {path}" in r.stdout.splitlines():
                owner = repo_path
                break

    # Dirty-tree protection (owner R-revisit item 1): never delete what was
    # not proven clean — quarantine it (files + git index) instead. This
    # does NOT depend on finding the owner repo: quarantine needs only the
    # path. A quarantine failure skips the tree (never deletes it).
    dirty = _is_dirty(path)
    if dirty is None:
        print(f"worktree-gc: {path} status unknown — skipped (NOT removed)")
        failed += 1
        continue
    if dirty:
        q = _quarantine_dirty(path, owner or "")
        if q:
            print(f"worktree-gc: quarantined dirty tree {path} -> {q}")
            if owner:
                subprocess.run(["git", "-C", owner, "worktree", "prune"],
                               capture_output=True, timeout=30)
            quarantined_dirty += 1
        else:
            print(f"worktree-gc: dirty tree {path} NOT quarantinable — "
                  f"skipped (NOT removed)")
            failed += 1
        continue

    if owner:
        result = subprocess.run(
            ["git", "-C", owner, "worktree", "remove", "--force", path],
            capture_output=True, timeout=30)
        if result.returncode == 0:
            cleaned += 1
        else:
            failed += 1
    else:
        failed += 1

print(f"worktree-gc: cleaned={cleaned} quarantined_dirty={quarantined_dirty} "
      f"failed={failed} skipped_young={skipped_young} "
      f"skipped_active={skipped_active} root={WORKTREE_ROOT}")
