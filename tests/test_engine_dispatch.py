"""Engine dispatch routing, Chat has no worktree, independent chat cap,
per-engine persistence, unknown-engine recovery (§B/§C/§F/§G/§K)."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import engines.base as eb


def _mk(engine, label="s", model=""):
    s = bot.Session(id=f"9:{label}", label=label, color_emoji="🔵",
                    session_uuid="u", project="P")
    s.engine = engine
    s.model = model
    return s


async def test_dispatch_routes_by_engine(monkeypatch):
    seen = []
    async def fake_claude(prompt, project, session, files=None, _rl_mode="flow"):
        seen.append(("claude", session.id)); return "C"
    async def fake_codex(prompt, project, session, files=None):
        seen.append(("codex", session.id)); return "X"
    async def fake_chat(prompt, session, files=None):
        seen.append(("chat", session.id)); return "H"
    monkeypatch.setattr(bot, "run_claude", fake_claude)
    monkeypatch.setattr(bot, "run_codex", fake_codex)
    monkeypatch.setattr(bot, "run_chat", fake_chat)

    assert await bot.dispatch_turn("p", "P", _mk("claude", "a")) == "C"
    assert await bot.dispatch_turn("p", "P", _mk("codex", "b")) == "X"
    assert await bot.dispatch_turn("p", "P", _mk("chat", "c")) == "H"
    assert [x[0] for x in seen] == ["claude", "codex", "chat"]


async def test_unknown_engine_recovers(monkeypatch):
    out = await bot.dispatch_turn("p", "P", _mk("ghost", "z"))
    assert "not" in out.lower() and "/new" in out


async def test_chat_creates_no_worktree(monkeypatch, tmp_path):
    # If Chat ever constructs a WorktreeSession, blow up.
    def boom(*a, **k):
        raise AssertionError("Chat must not create a worktree")
    monkeypatch.setattr(bot, "WorktreeSession", boom)
    bot.CONC.init(max_running=2, max_chat=2)
    # temp chat root (never the real /opt path) + offline preloaded catalog
    monkeypatch.setattr(bot, "CHAT_SESSIONS_DIR", str(tmp_path / "chat-root"))
    from engines.base import ModelInfo
    bot.CHAT_CATALOG._models = [ModelInfo("z-ai/glm-5.2", "GLM 5.2")]
    bot.CHAT_CATALOG._fetched_at = 1e18  # far future -> never refetches (offline)

    async def fake_chat_call(model, messages, timeout=120.0, max_retries=3):
        return "chat reply"
    monkeypatch.setattr(bot._or_client, "chat", fake_chat_call)

    s = _mk("chat", "cc", model="z-ai/glm-5.2")
    bot._apply_engine(s, "chat", "z-ai/glm-5.2")   # derives safe dir under temp root
    bot.SM.sessions[s.id] = s
    try:
        out = await bot.run_chat("hello", s)
    finally:
        bot.SM.sessions.pop(s.id, None)
        bot.CHAT_CATALOG._models = []; bot.CHAT_CATALOG._fetched_at = 0.0
    assert out == "chat reply"
    # history persisted under the safe derived dir inside the temp root
    hist = json.loads(Path(s.chat_history_ref, "history.json").read_text())
    assert hist[-1]["content"] == "chat reply"
    assert str(tmp_path) in s.chat_history_ref


async def test_chat_cap_independent_of_heavy(monkeypatch):
    # heavy cap = 1 (a held heavy permit blocks a 2nd heavy) but chat still runs.
    bot.CONC.init(max_running=1, max_chat=2)
    held = asyncio.Event()
    release = asyncio.Event()

    heavy_s = _mk("codex", "heavy")
    bot.SM.sessions[heavy_s.id] = heavy_s

    async def hold_heavy():
        async with bot.execution_slot(heavy_s.id, heavy_s):
            held.set()
            await release.wait()

    t = asyncio.create_task(hold_heavy())
    await asyncio.wait_for(held.wait(), 2)
    assert bot.CONC.running == 1

    chat_s = _mk("chat", "chatty")
    bot.SM.sessions[chat_s.id] = chat_s
    entered = asyncio.Event()

    async def chat_turn():
        async with bot.execution_slot(chat_s.id, chat_s, heavy=False):
            entered.set()

    ct = asyncio.create_task(chat_turn())
    # chat proceeds despite the heavy permit being fully held
    await asyncio.wait_for(entered.wait(), 2)
    assert bot.CONC.running == 1

    release.set()
    await asyncio.wait_for(asyncio.gather(t, ct), 2)
    bot.SM.sessions.pop(heavy_s.id, None); bot.SM.sessions.pop(chat_s.id, None)


def test_engine_persistence_roundtrip(tmp_path, monkeypatch):
    state = {
        "version": 1, "saved_at": time.time(),
        "sessions": {
            "3:cod": {"id": "3:cod", "label": "cod", "color_emoji": "🟢",
                      "session_uuid": "u", "project": "P", "status": "idle",
                      "started_at": time.time(), "last_active": time.time(),
                      "message_ids": [], "anchor_message_id": None, "tasks": 0,
                      "claude_created": False, "claude_model": "",
                      "engine": "codex", "model": "gpt-5.6-sol",
                      "provider_session_id": "thread-xyz", "chat_history_ref": ""},
            "3:cha": {"id": "3:cha", "label": "cha", "color_emoji": "🟡",
                      "session_uuid": "u2", "project": "chat", "status": "idle",
                      "started_at": time.time(), "last_active": time.time(),
                      "message_ids": [], "anchor_message_id": None, "tasks": 0,
                      "claude_created": False, "claude_model": "",
                      "engine": "chat", "model": "z-ai/glm-5.2",
                      "provider_session_id": "", "chat_history_ref": "/tmp/x"},
        },
        "msg_to_session": {}, "color_index": {}, "active_session": {},
    }
    sf = tmp_path / "s.json"; sf.write_text(json.dumps(state))
    monkeypatch.setattr(bot, "STATE_FILE", str(sf))
    saved = dict(bot.SM.sessions)
    try:
        bot.SM.sessions.clear(); bot.load_state()
        cod = bot.SM.sessions["3:cod"]; cha = bot.SM.sessions["3:cha"]
        assert cod.engine == "codex" and cod.model == "gpt-5.6-sol"
        assert cod.provider_session_id == "thread-xyz"
        assert cha.engine == "chat" and cha.model == "z-ai/glm-5.2"
        # re-save keeps the fields
        bot.save_state()
        reloaded = json.loads(sf.read_text())["sessions"]["3:cod"]
        assert reloaded["engine"] == "codex" and reloaded["provider_session_id"] == "thread-xyz"
    finally:
        bot.SM.sessions.clear(); bot.SM.sessions.update(saved)
