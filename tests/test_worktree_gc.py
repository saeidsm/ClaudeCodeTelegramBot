"""Owner R-revisit item 1 — the worktree-GC half.

Worktrees now persist across a session's turns (BOT_WORKTREE_REUSE), so the
GC timer is their main deletion path. A dirty tree must be QUARANTINED (with
its git index), never rm -rf'd; a clean tree is removed as before. The GC is
a top-level script (it runs its loop at import), so it is exercised as a
subprocess with an isolated env.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _mk_repo(base: Path) -> Path:
    repo = base / "repos" / "repoA"
    repo.mkdir(parents=True)

    def g(*a):
        return subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "hello.txt").write_text("one\n")
    g("add", "-A")
    g("commit", "-qm", "c1")
    return repo


def _run_gc(env_extra: dict) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(env_extra)
    r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "worktree-gc.py")],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout + r.stderr


def test_gc_quarantines_dirty_and_removes_clean(tmp_path):
    root = tmp_path / "wtroot"
    root.mkdir()
    data = tmp_path / "data"
    repo = _mk_repo(tmp_path)

    def mk_wt(name: str) -> Path:
        wt = root / name
        r = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                            str(wt), "HEAD"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        # age the tree out of every active check
        old = 1_000_000_000
        for p in Path(wt).rglob("*"):
            os.utime(p, (old, old))
        os.utime(wt, (old, old))
        return wt

    dirty = mk_wt("gc-dirty")
    (dirty / "uncommitted.txt").write_text("keep me\n")
    os.utime(dirty / "uncommitted.txt", (1_000_000_000, 1_000_000_000))
    clean = mk_wt("gc-clean")

    out = _run_gc({
        "BOT_WORKTREE_ROOT": str(root),
        "BOT_REPOS": str(tmp_path / "repos"),
        "BOT_DATA_ROOT": str(data),
        # age threshold 0: nothing is young, nothing is active
        "BOT_WORKTREE_GC_AGE_DAYS": "0",
    })
    assert "quarantined_dirty=1" in out, out
    # the CLEAN tree is removed as before
    assert not clean.exists(), "a clean old tree is removed by the GC"
    # the DIRTY tree is QUARANTINED with files AND the git index — never deleted
    assert not dirty.exists(), "the spawn path is freed (tree moved)"
    qdirs = list((data / "quarantine").rglob("stale-*"))
    assert qdirs, out
    q = qdirs[0]
    assert (q / "uncommitted.txt").read_text() == "keep me\n", \
        "the dirty file must survive readable in the quarantine"
    assert (q / "git-meta" / "index").exists(), \
        "item 1: the git index must survive beside the dirty files"


def test_gc_skips_a_tree_it_cannot_quarantine(tmp_path):
    """A dirty tree whose quarantine fails is SKIPPED — never deleted.
    (Failure injection: the quarantine root path runs INTO A FILE, so its
    mkdir raises; works even when tests run as root.)"""
    root = tmp_path / "wtroot2"
    root.mkdir()
    repo = _mk_repo(tmp_path / "r2")
    wt = root / "gc-stuck"
    r = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                        str(wt), "HEAD"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (wt / "uncommitted.txt").write_text("stuck\n")
    os.utime(wt / "uncommitted.txt", (1_000_000_000, 1_000_000_000))
    old = 1_000_000_000
    for p in Path(wt).rglob("*"):
        os.utime(p, (old, old))
    os.utime(wt, (old, old))
    notadir = tmp_path / "notadir"
    notadir.write_text("x")            # quarantine root parent is a FILE
    out = _run_gc({
        "BOT_WORKTREE_ROOT": str(root),
        "BOT_REPOS": str(tmp_path / "r2"),
        "BOT_DATA_ROOT": str(tmp_path / "data2"),
        "BOT_WORKTREE_GC_AGE_DAYS": "0",
        "BOT_MCP_QUARANTINE_DIR": str(notadir / "quarantine"),
    })
    assert "NOT removed" in out, out
    assert wt.exists(), "an unquarantinable dirty tree must be LEFT ALONE"
    assert (wt / "uncommitted.txt").exists()