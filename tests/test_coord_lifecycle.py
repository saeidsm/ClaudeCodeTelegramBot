"""CoordRun truthful lifecycle in the shared store (Correction 4, bot-level).

Offline: drives the CoordRun context manager against a temp store; no engines,
no worktrees, no subprocesses."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
from coordination import CoordinationStore


def _mk(tmp_path, monkeypatch, engine="codex"):
    store = CoordinationStore(str(tmp_path / "c.json"))
    monkeypatch.setattr(bot, "COORD", store)
    monkeypatch.setattr(bot, "COORDINATION_FILE", str(tmp_path / "c.json"))
    s = bot.Session(id="5:x", label="x", color_emoji="🔵", session_uuid="u")
    s.engine = engine
    return store, s


async def test_queued_then_running_then_completed(tmp_path, monkeypatch):
    store, s = _mk(tmp_path, monkeypatch)
    cr = bot.CoordRun(s, str(tmp_path / "repo"), "dev", "do work")
    await cr.__aenter__()
    assert store.all_entries()[0]["status"] == "queued"     # queued BEFORE slot
    assert store.all_entries()[0]["branch"] == "dev"
    wt = tmp_path / "wt"; wt.mkdir()
    await cr.running(str(wt))
    e = store.all_entries()[0]
    assert e["status"] == "running" and e["worktree"] == str(wt)
    cr.mark_ok()
    await cr.__aexit__(None, None, None)
    assert store.all_entries()[0]["status"] == "completed"


async def test_failed_when_not_ok(tmp_path, monkeypatch):
    store, s = _mk(tmp_path, monkeypatch)
    cr = bot.CoordRun(s, str(tmp_path / "repo"), "main", "x")
    await cr.__aenter__()
    await cr.__aexit__(None, None, None)                    # no mark_ok
    assert store.all_entries()[0]["status"] == "failed"


async def test_cancelled_status(tmp_path, monkeypatch):
    store, s = _mk(tmp_path, monkeypatch)
    cr = bot.CoordRun(s, str(tmp_path / "repo"), "main", "x")
    await cr.__aenter__()
    await cr.__aexit__(asyncio.CancelledError, asyncio.CancelledError(), None)
    assert store.all_entries()[0]["status"] == "cancelled"


async def test_running_writes_safe_worktree_view(tmp_path, monkeypatch):
    store, s = _mk(tmp_path, monkeypatch)
    cr = bot.CoordRun(s, str(tmp_path / "repo"), "main", "publish me")
    await cr.__aenter__()
    wt = tmp_path / "wt"; wt.mkdir()
    await cr.running(str(wt))
    view = (wt / "AGENT_COORDINATION.md")
    assert view.is_file()
    body = view.read_text()
    # publish instructions present, via the injected absolute-path env var
    assert "5:x" in body and "$AGENT_COORD_PUBLISHER" in body
    assert "snapshot" in body                              # labelled point-in-time


def test_coord_env_is_session_scoped(tmp_path, monkeypatch):
    store, s = _mk(tmp_path, monkeypatch)
    env = bot._coord_env(s)
    assert env["AGENT_COORD_SESSION"] == "5:x"
    assert env["AGENT_COORD_STORE"] == str(tmp_path / "c.json")
