"""Coordination file safety + truthful semantics (Correction 4).

Symlink / tracked-file refusal (with outside sentinels), bounded per-session
updates (cooperative identity — store integrity, not access control), and
sibling visibility during concurrent active runs."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordination import (
    CoordinationStore, CoordEntry, COORD_FILENAME, MAX_SUMMARY, MAX_PATHS,
    MAX_ETA, _git_tracked,
)


def _store(tmp_path):
    return CoordinationStore(str(tmp_path / "coord.json"))


def test_link_refuses_symlink_dest_keeps_outside(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "claude", "", str(tmp_path)))
    wt = tmp_path / "wt"; wt.mkdir()
    outside = tmp_path / "OUTSIDE.md"; outside.write_text("KEEP")
    # plant AGENT_COORDINATION.md as a symlink to the outside file
    (wt / COORD_FILENAME).symlink_to(outside)
    dest = s.link_into_worktree(str(wt))
    assert dest is None                       # refused
    assert outside.read_text() == "KEEP"      # outside sentinel untouched
    assert os.path.islink(wt / COORD_FILENAME)  # symlink left as-is, not written through


def test_link_refuses_directory_dest(tmp_path):
    s = _store(tmp_path)
    wt = tmp_path / "wt"; wt.mkdir()
    (wt / COORD_FILENAME).mkdir()             # a directory occupies the name
    assert s.link_into_worktree(str(wt)) is None


def test_link_refuses_tracked_file(tmp_path):
    s = _store(tmp_path)
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@x"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
    tracked = repo / COORD_FILENAME
    tracked.write_text("REPO OWNED")
    subprocess.run(["git", "-C", str(repo), "add", COORD_FILENAME], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)
    assert _git_tracked(str(repo), COORD_FILENAME) is True
    assert s.link_into_worktree(str(repo)) is None      # refused
    assert tracked.read_text() == "REPO OWNED"          # not clobbered


def test_link_writes_when_safe(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "codex", "sol", str(tmp_path), status="running",
                        summary="task"))
    wt = tmp_path / "wt"; wt.mkdir()
    dest = s.link_into_worktree(str(wt), instructions="### How to coordinate\nrun it")
    assert dest and os.path.isfile(dest)
    body = Path(dest).read_text()
    assert "AGENT COORDINATION" in body and "How to coordinate" in body
    assert not os.path.islink(dest)


def test_update_claim_only_own_session_and_bounded(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "codex", "", str(tmp_path), status="running"))
    # unknown session -> no-op (can't touch others)
    assert s.update_claim("9:x", summary="hack") is False
    # own session -> bounded
    assert s.update_claim("1:a", summary="Z" * 999,
                          expected_paths=["p"] * 999, eta="E" * 99, status="running")
    e = s.entries_for_repo(str(tmp_path))[0]
    assert len(e["summary"]) <= MAX_SUMMARY
    assert len(e["expected_paths"]) <= MAX_PATHS
    assert len(e["eta"]) <= MAX_ETA


def test_update_claim_rejects_invalid_status(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "codex", "", str(tmp_path), status="running"))
    s.update_claim("1:a", status="bogus-status")
    assert s.entries_for_repo(str(tmp_path))[0]["status"] == "running"  # unchanged


def test_lifecycle_transitions(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "claude", "", str(tmp_path), status="queued"))
    assert s.all_entries()[0]["status"] == "queued"
    s.upsert(CoordEntry("1:a", "claude", "", str(tmp_path), status="running",
                        worktree="/wt"))
    assert s.all_entries()[0]["status"] == "running"
    assert s.all_entries()[0]["worktree"] == "/wt"
    s.set_status("1:a", "completed")
    assert s.all_entries()[0]["status"] == "completed"


def test_sibling_visibility_while_both_active(tmp_path):
    s = _store(tmp_path)
    repo = str(tmp_path / "repo")
    s.upsert(CoordEntry("1:a", "claude", "", repo, status="running", summary="A work"))
    s.upsert(CoordEntry("2:b", "codex", "", repo, status="running", summary="B work"))
    # a third agent rendering the view sees BOTH active siblings
    md = s.render_markdown(repo)
    assert "1:a" in md and "2:b" in md and "A work" in md and "B work" in md


def test_parallel_update_claim_no_corruption(tmp_path):
    s = _store(tmp_path)
    for i in range(20):
        s.upsert(CoordEntry(f"1:{i}", "codex", "", str(tmp_path), status="running"))

    def worker(i):
        s.update_claim(f"1:{i}", summary=f"s{i}", eta="5m")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    entries = {e["session_id"]: e for e in s.all_entries()}
    assert len(entries) == 20
    for i in range(20):
        assert entries[f"1:{i}"]["summary"] == f"s{i}"
