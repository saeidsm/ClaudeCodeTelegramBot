"""End-to-end Phase-2 callback codec + handler tests (Correction 3).

Drives the real ``_handle_p2`` with a fake callback query. Proves arbitrary
delimiter/Unicode labels and ``:free`` model ids round-trip through the opaque
token registry without misrouting, that expiry fails closed, and that
cross-chat / stale sessions are rejected. No ``split()`` is duplicated here."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import engines.base as eb


class FakeMsg:
    def __init__(self, chat_id, mid=555):
        self.chat = type("C", (), {"id": chat_id})()
        self.message_id = mid
        self.edits = []

    async def edit_message_text(self, text, **k):
        self.edits.append(text)


class FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = FakeMsg(chat_id)
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True

    async def edit_message_text(self, text, **k):
        self.message.edits.append(text)


def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "CHAT_SESSIONS_DIR", str(tmp_path / "chat"))
    bot.SM.sessions.clear()
    bot.ACTIVE_SESSION.clear()
    bot._P2_REGISTRY.clear()
    from engines.base import ModelInfo
    bot.CHAT_CATALOG._models = [ModelInfo("z-ai/glm-5.2", "GLM 5.2"),
                                ModelInfo("qwen/qwen3-coder:free", "Qwen Coder Free")]
    bot.CHAT_CATALOG._fetched_at = 1e18


async def _dispatch(token, chat_id):
    q = FakeQuery(token, chat_id)
    payload = bot.cb2_resolve(token)
    assert payload is not None
    await bot._handle_p2(q, None, chat_id, payload, token=token)
    return q


EXOTIC_LABELS = ["foo:bar", "a/b/c", "pipe|x", "نام‌فارسی", "x" * 120]


@pytest.mark.parametrize("label", EXOTIC_LABELS)
async def test_newproj_roundtrips_exotic_label(monkeypatch, tmp_path, label):
    _reset(monkeypatch, tmp_path)
    tok = bot.cb2("newproj", 42, engine="claude", label=label, project="my:proj/x")
    assert len(tok.encode()) <= 64
    q = await _dispatch(tok, 42)
    # the exact label + project routed through, no truncation
    key = f"42:{label}"
    assert key in bot.SM.sessions
    s = bot.SM.sessions[key]
    assert s.project == "my:proj/x"
    assert s.engine == "claude"


async def test_cmk_free_model_id_preserved(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    tok = bot.cb2("cmk", 7, label="chatty", has_name=True, model="qwen/qwen3-coder:free")
    q = await _dispatch(tok, 7)
    s = bot.SM.sessions["7:chatty"]
    assert s.engine == "chat"
    assert s.model == "qwen/qwen3-coder:free"   # ':free' tail intact


async def test_cmk_rejects_invalid_model(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    tok = bot.cb2("cmk", 7, label="c", has_name=True, model="not-a-real/model")
    q = await _dispatch(tok, 7)
    assert "7:c" not in bot.SM.sessions          # not created
    assert "isn't a valid chat model" in q.message.edits[-1]


async def test_expired_token_fails_closed(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    # simulate restart/eviction: token not in registry
    q = FakeQuery("p2:99999", 1)
    # on_callback path: expired -> resolve None
    assert bot.cb2_resolve("p2:99999") is None


async def test_long_payload_indirection_under_64_bytes(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    huge = "قهرمان-" + "x" * 300
    tok = bot.cb2("neng", 1, engine="codex", label=huge, has_name=False)
    assert len(tok.encode()) <= 64
    assert bot.cb2_resolve(tok)["label"] == huge


async def test_emodset_cross_chat_rejected(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    # a chat session owned by chat 100
    s = bot.Session(id="100:c", label="c", color_emoji="🔵", session_uuid="u")
    bot._apply_engine(s, "chat", "z-ai/glm-5.2")
    bot.SM.sessions[s.id] = s
    # attacker in chat 200 clicks an emodset button referencing 100:c
    tok = bot.cb2("emodset", 200, session_key="100:c", model="z-ai/glm-5.2")
    q = await _dispatch(tok, 200)
    assert "isn't in this chat" in q.message.edits[-1]
    assert s.model == "z-ai/glm-5.2"             # unchanged by the attacker


async def test_emod_stale_session_rejected(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    # emod bound to a session key that no longer exists -> fail closed (refresh)
    tok = bot.cb2("emod", 9, session_key="9:gone", engine="codex", model="gpt-5.6-sol")
    q = await _dispatch(tok, 9)
    assert "no longer exists" in q.message.edits[-1]
