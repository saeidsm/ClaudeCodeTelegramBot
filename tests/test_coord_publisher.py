"""Blocking finding 3 — coordination publisher works from a FOREIGN worktree.

The agent runs in a worktree of an arbitrary project that has no relative
``scripts/coord_publish.py``. These tests prove the real helper, invoked by its
absolute path with only the injected environment, updates the central flock
store from such a worktree; that ``--show`` reads the live view; and that the
bot's publisher preflight fails closed on symlinks / tampered bytes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
from coordination import CoordinationStore, CoordEntry

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PUBLISHER = _REPO_ROOT / "scripts" / "coord_publish.py"


def _foreign_worktree(tmp_path) -> Path:
    """A git repo with NO scripts/coord_publish.py — like ZK4 or any project."""
    wt = tmp_path / "foreign_project"
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    (wt / "README.md").write_text("not the bot repo\n")
    assert not (wt / "scripts" / "coord_publish.py").exists()
    return wt


def test_publisher_updates_store_from_foreign_worktree(tmp_path):
    store_path = tmp_path / "c.json"
    store = CoordinationStore(str(store_path))
    # bot has already registered this session's entry (lifecycle owns creation)
    store.upsert(CoordEntry(session_id="9:z", engine="codex", model="m",
                            repo=str(tmp_path / "proj"), status="running"))

    wt = _foreign_worktree(tmp_path)
    env = dict(os.environ)
    env.update({
        "AGENT_COORD_STORE": str(store_path),
        "AGENT_COORD_SESSION": "9:z",
    })
    # Invoke by ABSOLUTE path, cwd = the foreign worktree (relative path absent).
    r = subprocess.run(
        [sys.executable, str(_PUBLISHER),
         "--summary", "refactor auth", "--paths", "a.py,b.py", "--eta", "15m"],
        cwd=wt, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    e = store.all_entries()[0]
    assert e["summary"] == "refactor auth"
    assert e["expected_paths"] == ["a.py", "b.py"]
    assert e["eta"] == "15m"


def test_show_reads_live_central_view_from_foreign_worktree(tmp_path):
    store_path = tmp_path / "c.json"
    store = CoordinationStore(str(store_path))
    repo = str(tmp_path / "proj")
    store.upsert(CoordEntry(session_id="1:a", engine="claude", model="m",
                            repo=repo, summary="sibling work", status="running"))

    wt = _foreign_worktree(tmp_path)
    env = dict(os.environ)
    env.update({
        "AGENT_COORD_STORE": str(store_path),
        "AGENT_COORD_SESSION": "2:b",
        "AGENT_COORD_REPO": os.path.realpath(repo),
    })
    r = subprocess.run(
        [sys.executable, str(_PUBLISHER), "--show"],
        cwd=wt, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "AGENT COORDINATION" in r.stdout
    assert "sibling work" in r.stdout          # sees the sibling's live claim


def test_publisher_no_store_is_noop(tmp_path):
    wt = _foreign_worktree(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_COORD")}
    r = subprocess.run(
        [sys.executable, str(_PUBLISHER), "--summary", "x"],
        cwd=wt, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "not running under bot coordination" in r.stdout


# ── bot-side preflight (fail-closed) ───────────────────────────────────────
def test_valid_publisher_accepts_shipped_source():
    got = bot._valid_coord_publisher(bot._COORD_PUBLISHER_SRC)
    assert got == os.path.abspath(bot._COORD_PUBLISHER_SRC)


def test_valid_publisher_rejects_symlink(tmp_path):
    link = tmp_path / "coord_publish.py"
    link.symlink_to(bot._COORD_PUBLISHER_SRC)
    assert bot._valid_coord_publisher(str(link)) is None


def test_valid_publisher_rejects_tampered_bytes(tmp_path):
    fake = tmp_path / "coord_publish.py"
    fake.write_text("print('not the real publisher')\n")
    assert bot._valid_coord_publisher(str(fake)) is None


def test_valid_publisher_rejects_missing(tmp_path):
    assert bot._valid_coord_publisher(str(tmp_path / "nope.py")) is None


def test_coord_env_injects_validated_publisher(tmp_path, monkeypatch):
    store = CoordinationStore(str(tmp_path / "c.json"))
    monkeypatch.setattr(bot, "COORD", store)
    monkeypatch.setattr(bot, "COORDINATION_FILE", str(tmp_path / "c.json"))
    s = bot.Session(id="7:q", label="q", color_emoji="🔵", session_uuid="u")
    env = bot._coord_env(s)
    assert env["AGENT_COORD_PUBLISHER"] == os.path.abspath(bot._COORD_PUBLISHER_SRC)
    assert "AGENT_COORD_REPO" in env


def test_coord_env_omits_publisher_when_invalid(tmp_path, monkeypatch):
    bad = tmp_path / "coord_publish.py"
    bad.write_text("print('tampered')\n")
    monkeypatch.setattr(bot, "BOT_COORD_PUBLISHER", str(bad))
    s = bot.Session(id="8:w", label="w", color_emoji="🔵", session_uuid="u")
    env = bot._coord_env(s)
    assert "AGENT_COORD_PUBLISHER" not in env      # fail-closed, not injected
