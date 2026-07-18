"""Round-2 §1 — /model actions stay bound to the session the menu was rendered
FOR, never the live ACTIVE_SESSION at click time.

Drives the real on_callback central gate + _handle_p2, exercising: render-for-A →
switch active to B → click A only changes A; A killed/replaced before click →
nobody changes; wrong chat / wrong engine / expired / replay fail closed; and the
search/custom conversation state stays bound to A after an active-session switch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot


class FakeMsg:
    def __init__(self, chat_id, mid=1):
        self.chat = type("C", (), {"id": chat_id})()
        self.message_id = mid
        self.edits = []

    async def edit_message_text(self, text, **k):
        self.edits.append(text)


class FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = FakeMsg(chat_id)

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **k):
        self.message.edits.append(text)


class FakeUpdate:
    def __init__(self, q):
        self.callback_query = q
        self.effective_chat = q.message.chat
        self.message = None


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_IDS", set())
    monkeypatch.setattr(bot, "save_state", lambda: None)
    bot.SM.sessions.clear()
    bot.ACTIVE_SESSION.clear()
    bot._P2_REGISTRY.clear()
    bot.CONV_STATE.clear()
    yield


def _mk(chat_id, label, engine, **kw):
    s = bot.Session(id=f"{chat_id}:{label}", label=label, color_emoji="🔵",
                    session_uuid=f"u-{label}")
    s.engine = engine
    for k, v in kw.items():
        setattr(s, k, v)
    bot.SM.sessions[s.id] = s
    return s


async def _click(token, chat_id):
    q = FakeQuery(token, chat_id)
    await bot.on_callback(FakeUpdate(q), None)
    return q


# ── core binding ────────────────────────────────────────────────────────────
async def test_emod_targets_rendered_session_not_active(monkeypatch):
    a = _mk(1, "A", "claude", claude_model="")
    b = _mk(1, "B", "claude", claude_model="sonnet")
    bot.ACTIVE_SESSION[1] = a.id
    # menu rendered for A (bound in the token)
    tok = bot.cb2("emod", 1, session_key=a.id, engine="claude", model="claude-opus-4-8")
    # user switches active to B before clicking A's button
    bot.ACTIVE_SESSION[1] = b.id
    await _click(tok, 1)
    assert a.claude_model == "claude-opus-4-8"      # A changed
    assert b.claude_model == "sonnet"               # B untouched despite being active


async def test_emod_killed_session_before_click_changes_nobody(monkeypatch):
    a = _mk(1, "A", "claude", claude_model="x")
    b = _mk(1, "B", "claude", claude_model="sonnet")
    bot.ACTIVE_SESSION[1] = b.id
    tok = bot.cb2("emod", 1, session_key=a.id, engine="claude", model="claude-opus-4-8")
    del bot.SM.sessions[a.id]                        # A killed before click
    q = await _click(tok, 1)
    assert "no longer exists" in q.message.edits[-1]
    assert b.claude_model == "sonnet"               # B (now active) untouched


async def test_emod_engine_migrated_fails_closed(monkeypatch):
    a = _mk(1, "A", "claude", claude_model="x")
    tok = bot.cb2("emod", 1, session_key=a.id, engine="claude", model="claude-opus-4-8")
    a.engine = "codex"                              # A migrated engines after render
    q = await _click(tok, 1)
    assert "engine changed" in q.message.edits[-1]
    assert a.claude_model == "x"                    # unchanged


async def test_emod_wrong_chat_rejected(monkeypatch):
    a = _mk(1, "A", "claude", claude_model="x")
    tok = bot.cb2("emod", 1, session_key=a.id, engine="claude", model="claude-opus-4-8")
    q = await _click(tok, 999)                      # attacker chat
    assert "isn't for this chat" in q.message.edits[-1]
    assert a.claude_model == "x"


async def test_emod_replay_after_success_fails_closed(monkeypatch):
    # emod is not one-shot, but a stale menu after a session change is refused.
    a = _mk(1, "A", "claude", claude_model="")
    tok = bot.cb2("emod", 1, session_key=a.id, engine="claude", model="claude-opus-4-8")
    await _click(tok, 1)
    assert a.claude_model == "claude-opus-4-8"
    del bot.SM.sessions[a.id]                        # A gone; replay must fail closed
    q = await _click(tok, 1)
    assert "no longer exists" in q.message.edits[-1]


# ── search / custom conv-state binding ──────────────────────────────────────
async def test_emsearch_binds_convstate_to_rendered_session(monkeypatch):
    a = _mk(1, "A", "chat", model="z-ai/glm-5.2")
    b = _mk(1, "B", "chat", model="minimax/minimax-m3")
    bot.ACTIVE_SESSION[1] = a.id
    tok = bot.cb2("emsearch", 1, session_key=a.id, engine="chat")
    bot.ACTIVE_SESSION[1] = b.id                    # switch active AFTER render
    await _click(tok, 1)
    assert bot.CONV_STATE[1]["session_key"] == a.id     # bound to A, not B


async def test_emcustom_binds_convstate_to_rendered_session(monkeypatch):
    a = _mk(1, "A", "codex", model="gpt-5.6-sol")
    b = _mk(1, "B", "codex", model="gpt-5.6-luna")
    bot.ACTIVE_SESSION[1] = a.id
    tok = bot.cb2("emcustom", 1, session_key=a.id, engine="codex")
    bot.ACTIVE_SESSION[1] = b.id
    await _click(tok, 1)
    assert bot.CONV_STATE[1]["session_key"] == a.id


async def test_emsearch_killed_session_fails_closed(monkeypatch):
    a = _mk(1, "A", "chat", model="z-ai/glm-5.2")
    tok = bot.cb2("emsearch", 1, session_key=a.id, engine="chat")
    del bot.SM.sessions[a.id]
    q = await _click(tok, 1)
    assert "no longer exists" in q.message.edits[-1]
    assert 1 not in bot.CONV_STATE                  # no dangling conv-state


# ── same-label / same-engine REPLACEMENT rejection (instance nonce) ─────────
def _mk_uuid(chat_id, label, engine, uuid, **kw):
    s = bot.Session(id=f"{chat_id}:{label}", label=label, color_emoji="🔵",
                    session_uuid=uuid)
    s.engine = engine
    for k, v in kw.items():
        setattr(s, k, v)
    bot.SM.sessions[s.id] = s
    return s


async def test_emod_rejects_same_label_same_engine_replacement(monkeypatch):
    # render for the OLD instance
    old = _mk_uuid(1, "A", "claude", "uuid-OLD", claude_model="x")
    tok = bot.cb2("emod", 1, session_key=old.id, engine="claude",
                  uuid="uuid-OLD", model="claude-opus-4-8")
    # A is killed and recreated with the SAME label + SAME engine (new nonce)
    new = _mk_uuid(1, "A", "claude", "uuid-NEW", claude_model="keep")
    q = await _click(tok, 1)
    assert "replaced by a newer one" in q.message.edits[-1]
    assert new.claude_model == "keep"           # the replacement is untouched


async def test_emsearch_convstate_rejects_replacement(monkeypatch):
    old = _mk_uuid(1, "A", "chat", "uuid-OLD", model="z-ai/glm-5.2")
    tok = bot.cb2("emsearch", 1, session_key=old.id, engine="chat", uuid="uuid-OLD")
    await _click(tok, 1)
    assert bot.CONV_STATE[1]["uuid"] == "uuid-OLD"
    # replace the session under the same label/engine, then submit the search text
    _mk_uuid(1, "A", "chat", "uuid-NEW", model="keep")
    conv = bot.CONV_STATE[1]
    target, reason = bot._resolve_rendered_session(1, conv)
    assert target is None and "replaced by a newer one" in reason


async def test_emcustom_convstate_rejects_replacement(monkeypatch):
    old = _mk_uuid(1, "A", "codex", "uuid-OLD", model="gpt-5.6-sol")
    tok = bot.cb2("emcustom", 1, session_key=old.id, engine="codex", uuid="uuid-OLD")
    await _click(tok, 1)
    conv = bot.CONV_STATE[1]
    assert conv["uuid"] == "uuid-OLD"
    _mk_uuid(1, "A", "codex", "uuid-NEW", model="keep")
    target, reason = bot._resolve_rendered_session(1, conv)
    assert target is None and "replaced by a newer one" in reason


async def test_same_uuid_still_works(monkeypatch):
    # sanity: matching nonce still permits the change on the same instance
    s = _mk_uuid(1, "A", "claude", "uuid-SAME", claude_model="")
    tok = bot.cb2("emod", 1, session_key=s.id, engine="claude",
                  uuid="uuid-SAME", model="claude-opus-4-8")
    await _click(tok, 1)
    assert s.claude_model == "claude-opus-4-8"


# ── reserved keys are non-overridable ───────────────────────────────────────
def test_reserved_keys_cannot_be_overridden():
    # a caller field named cid/k must NOT replace the real binding
    tok = bot.cb2("emod", 42, cid=999, k="evil", session_key="42:x", model="m")
    p = bot.cb2_resolve(tok)
    assert p["cid"] == 42 and p["k"] == "emod"
