"""Tests for video_module.jobs — atomic JSON job persistence."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_module import jobs  # noqa: E402


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    store = jobs.JobStore(tmp_path)
    store.save(42, {"step": "headline", "brand": "AlumGlass"})
    assert store.load(42) == {"step": "headline", "brand": "AlumGlass"}


def test_load_missing_returns_none(tmp_path: Path) -> None:
    store = jobs.JobStore(tmp_path)
    assert store.load(99) is None


def test_delete_removes_file(tmp_path: Path) -> None:
    store = jobs.JobStore(tmp_path)
    store.save(7, {"x": 1})
    assert store.load(7) == {"x": 1}
    store.delete(7)
    assert store.load(7) is None


def test_atomic_write_uses_tmp_rename(tmp_path: Path) -> None:
    """The on-disk file should never appear partially written —
    a crashed write must leave the previous state intact."""
    store = jobs.JobStore(tmp_path)
    store.save(1, {"v": "old"})
    # Simulate by writing again; tmp file must not exist afterwards
    store.save(1, {"v": "new"})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert store.load(1) == {"v": "new"}
