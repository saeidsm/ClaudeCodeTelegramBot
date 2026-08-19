"""Engine-aware /model selection (Correction 2): real catalogs + validation,
per-session isolation, resume-identity preservation, save-on-change."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import engines.base as eb


class FakeQuery:
    def __init__(self, chat_id):
        self.message = type("M", (), {"chat": type("C", (), {"id": chat_id})(),
                                      "message_id": 1})()
        self.edits = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **k):
        self.edits.append(text)


def _reset(monkeypatch):
    bot.SM.sessions.clear(); bot.ACTIVE_SESSION.clear(); bot._P2_REGISTRY.clear()
    monkeypatch.setattr(bot, "save_state", lambda: bot._saves.append(1))
    bot._saves = []


async def _emod(session, model):
    # emod is now bound to the rendered session (session_key + engine in payload).
    chat_id = int(session.id.split(":")[0])
    q = FakeQuery(chat_id)
    await bot._handle_p2(q, None, chat_id, {
        "k": "emod", "model": model,
        "session_key": session.id, "engine": session.engine})
    return q


async def _emod_unbound(chat_id, model, session_key=""):
    q = FakeQuery(chat_id)
    await bot._handle_p2(q, None, chat_id, {
        "k": "emod", "model": model, "session_key": session_key, "engine": ""})
    return q


def _mkactive(chat_id, label, engine, **kw):
    s = bot.Session(id=f"{chat_id}:{label}", label=label, color_emoji="🔵",
                    session_uuid="uuid-keep")
    s.engine = engine
    for k, v in kw.items():
        setattr(s, k, v)
    bot.SM.sessions[s.id] = s
    bot.ACTIVE_SESSION[chat_id] = s.id
    return s


async def test_claude_emod_sets_claude_model_validated(monkeypatch):
    _reset(monkeypatch)
    s = _mkactive(1, "a", "claude")
    await _emod(s, "claude-opus-4-8")
    assert s.claude_model == "claude-opus-4-8"
    assert s.session_uuid == "uuid-keep"      # resume identity preserved
    assert bot._saves                          # save_state called


async def test_claude_emod_rejects_cross_engine(monkeypatch):
    _reset(monkeypatch)
    s = _mkactive(1, "a", "claude", claude_model="opus")
    q = await _emod(s, "gpt-5.6-sol")          # a Codex slug
    assert s.claude_model == "opus"            # unchanged
    assert "isn't valid" in q.edits[-1]


async def test_codex_emod_sets_model_preserves_thread(monkeypatch):
    _reset(monkeypatch)
    s = _mkactive(2, "c", "codex", provider_session_id="thread-1")
    await _emod(s, "gpt-5.6-luna")
    assert s.model == "gpt-5.6-luna"
    assert s.provider_session_id == "thread-1"  # thread identity preserved


async def test_codex_emod_rejects_openrouter_id(monkeypatch):
    _reset(monkeypatch)
    s = _mkactive(2, "c", "codex", model="gpt-5.6-sol")
    q = await _emod(s, "z-ai/glm-5.2")
    assert s.model == "gpt-5.6-sol"
    assert "isn't valid" in q.edits[-1]


async def test_model_change_isolated_to_active_session(monkeypatch):
    _reset(monkeypatch)
    a = _mkactive(3, "a", "claude", claude_model="")
    b = bot.Session(id="3:b", label="b", color_emoji="🟢", session_uuid="u2")
    b.engine = "claude"; b.claude_model = "sonnet"
    bot.SM.sessions[b.id] = b
    bot.ACTIVE_SESSION[3] = a.id               # a is active
    await _emod(a, "claude-fable-5")
    assert a.claude_model == "claude-fable-5"
    assert b.claude_model == "sonnet"          # sibling untouched


async def test_emod_no_active_session_fails_closed(monkeypatch):
    _reset(monkeypatch)
    q = await _emod_unbound(9, "claude-opus-4-8")
    assert "out of date" in q.edits[-1]


def test_claude_catalog_has_named_models():
    ids = [m.id for m in eb.get_adapter("claude").list_models()]
    for want in ("", "claude-opus-5", "claude-opus-5[1m]", "claude-sonnet-5",
                 "claude-sonnet-5[1m]", "claude-fable-5", "claude-haiku-4-5"):
        assert want in ids
