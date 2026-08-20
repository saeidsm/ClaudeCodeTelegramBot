"""Shared coordination store: locks/parallel/stale/path safety (§H)."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordination import CoordinationStore, CoordEntry, canonical_repo, COORD_FILENAME


def _store(tmp_path):
    return CoordinationStore(str(tmp_path / "coord.json"))


def test_upsert_and_repo_query(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "claude", "opus", str(tmp_path), branch="dev",
                        summary="task A"))
    s.upsert(CoordEntry("1:b", "codex", "gpt-5.6-sol", "/other/repo"))
    got = s.entries_for_repo(str(tmp_path))
    assert len(got) == 1 and got[0]["session_id"] == "1:a"
    assert got[0]["repo"] == canonical_repo(str(tmp_path))


def test_set_status_and_remove(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "claude", "", str(tmp_path)))
    s.set_status("1:a", "completed")
    assert s.all_entries()[0]["status"] == "completed"
    s.remove("1:a")
    assert s.all_entries() == []


def test_reconcile_marks_stale_but_keeps_active(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:live", "claude", "", str(tmp_path), status="running"))
    s.upsert(CoordEntry("1:dead", "codex", "", str(tmp_path), status="running"))
    s.upsert(CoordEntry("1:done", "claude", "", str(tmp_path), status="completed"))
    n = s.reconcile(active_ids={"1:live"})
    by = {e["session_id"]: e for e in s.all_entries()}
    assert n == 1
    assert by["1:live"]["status"] == "running"   # active untouched
    assert by["1:dead"]["status"] == "stale"     # orphan marked
    assert by["1:done"]["status"] == "completed" # not active/queued -> untouched
    assert len(by) == 3                            # nothing deleted


def test_render_markdown(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "claude", "opus", str(tmp_path), branch="main",
                        summary="pipe|and\nnewline", status="running"))
    md = s.render_markdown()
    assert "AGENT COORDINATION" in md
    assert "1:a" in md and "claude" in md
    assert "\n" in md and "pipe/and newline" in md  # sanitized cell


def test_render_empty(tmp_path):
    assert "No active agents" in _store(tmp_path).render_markdown()


def test_link_into_worktree_writes_file(tmp_path):
    s = _store(tmp_path)
    s.upsert(CoordEntry("1:a", "claude", "", str(tmp_path), worktree=str(tmp_path)))
    wt = tmp_path / "wt"
    wt.mkdir()
    dest = s.link_into_worktree(str(wt))
    assert dest == str(wt / COORD_FILENAME)
    assert os.path.isfile(dest)
    assert "AGENT COORDINATION" in Path(dest).read_text()


def test_link_into_worktree_rejects_missing_dir(tmp_path):
    s = _store(tmp_path)
    assert s.link_into_worktree(str(tmp_path / "does-not-exist")) is None


def test_parallel_upserts_do_not_corrupt(tmp_path):
    s = _store(tmp_path)

    def worker(i):
        s.upsert(CoordEntry(f"1:{i}", "codex", "", str(tmp_path)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = s.all_entries()
    assert len(entries) == 25                # no lost writes / corruption
    assert len({e["session_id"] for e in entries}) == 25
