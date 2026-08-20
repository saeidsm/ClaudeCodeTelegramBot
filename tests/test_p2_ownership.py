"""Blocking finding 4 — Phase-2 callback tokens are unguessable and owner-bound.

Drives the REAL on_callback central gate (not just _handle_p2) to prove:
  * tokens are cryptographically random, not sequential (unguessable);
  * every action family is refused from the WRONG chat before any side effect;
  * a guessed/adjacent token fails closed;
  * create actions are one-shot: a double-click / replay cannot spawn or replace
    a second same-label session;
  * expiry / eviction / restart loss all fail closed with the expired-button UX.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot


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
        self.answers = []

    async def answer(self, *a, **k):
        self.answers.append(a)

    async def edit_message_text(self, text, **k):
        self.message.edits.append(text)


class FakeUpdate:
    def __init__(self, q):
        self.callback_query = q
        self.effective_chat = q.message.chat
        self.message = None


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "CHAT_SESSIONS_DIR", str(tmp_path / "chat"))
    monkeypatch.setattr(bot, "ALLOWED_IDS", set())     # allow every chat (auth off)
    monkeypatch.setattr(bot, "save_state", lambda: None)
    bot.SM.sessions.clear()
    bot.ACTIVE_SESSION.clear()
    bot._P2_REGISTRY.clear()
    yield


async def _click(token, chat_id):
    q = FakeQuery(token, chat_id)
    await bot.on_callback(FakeUpdate(q), None)
    return q


# ── unguessable ────────────────────────────────────────────────────────────
def test_tokens_are_random_not_sequential():
    a = bot.cb2("neng", 1, engine="claude", label="x", has_name=True)
    b = bot.cb2("neng", 1, engine="claude", label="x", has_name=True)
    assert a != b
    # not p2:1 / p2:2 style; body is high-entropy base64url
    assert not a.split(":", 1)[1].isdigit()
    assert len(set([bot.cb2("neng", 1, engine="c", label="y", has_name=True)
                    for _ in range(50)])) == 50     # no collisions/enumeration


async def test_guessed_adjacent_token_fails_closed():
    real = bot.cb2("neng", 1, engine="claude", label="x", has_name=True)
    q = await _click("p2:1", 1)                       # naive guess
    assert "expired" in q.message.edits[-1].lower()
    assert bot.cb2_resolve(real) is not None          # real one untouched


# ── wrong-chat rejection for every action family ───────────────────────────
@pytest.mark.parametrize("kind,fields", [
    ("neng", dict(engine="claude", label="l", has_name=True)),
    ("newproj", dict(engine="claude", label="l", project="p")),
    ("newneed", dict(engine="claude", label="l", project="p")),
    ("skipname", dict(engine="claude", label="l", project="p")),
    ("addproj", dict(engine="claude", label="l", has_name=True)),
    ("cmk", dict(label="l", has_name=True, model="z-ai/glm-5.2")),
    ("cmsearch", dict(label="l", has_name=True)),
    ("emod", dict(model="m")),
    ("emsearch", dict()),
    ("emcustom", dict()),
    ("emodset", dict(session_key="100:c", model="m")),
])
async def test_wrong_chat_rejected_before_action(kind, fields):
    owner, attacker = 100, 200
    tok = bot.cb2(kind, owner, **fields)
    q = await _click(tok, attacker)
    assert "isn't for this chat" in q.message.edits[-1]
    # nothing was created in the attacker's chat
    assert not any(sid.startswith(f"{attacker}:") for sid in bot.SM.sessions)
    # token survives (owner may still use it); it was not consumed by the attacker
    assert bot.cb2_resolve(tok) is not None


async def test_owner_click_is_allowed():
    tok = bot.cb2("newproj", 100, engine="claude", label="mine", project="proj")
    q = await _click(tok, 100)
    assert "100:mine" in bot.SM.sessions
    assert "created" in q.message.edits[-1].lower()


# ── one-shot create (double-click / replay) ────────────────────────────────
async def test_double_click_create_is_one_shot():
    tok = bot.cb2("newproj", 100, engine="claude", label="dup", project="proj")
    await _click(tok, 100)
    assert "100:dup" in bot.SM.sessions
    first = bot.SM.sessions["100:dup"]
    # second (replay) click of the SAME token
    q2 = await _click(tok, 100)
    assert "expired" in q2.message.edits[-1].lower()
    # still exactly one session with that key, and it's the SAME object (not replaced)
    assert bot.SM.sessions["100:dup"] is first
    assert sum(1 for sid in bot.SM.sessions if sid == "100:dup") == 1


# ── expiry / eviction / restart loss ───────────────────────────────────────
async def test_eviction_fails_closed():
    tok = bot.cb2("neng", 1, engine="claude", label="x", has_name=True)
    bot._P2_REGISTRY.pop(tok)                          # simulate LRU eviction
    q = await _click(tok, 1)
    assert "expired" in q.message.edits[-1].lower()


async def test_restart_loss_fails_closed():
    tok = bot.cb2("neng", 1, engine="claude", label="x", has_name=True)
    bot._P2_REGISTRY.clear()                           # simulate process restart
    q = await _click(tok, 1)
    assert "expired" in q.message.edits[-1].lower()
