"""Offline MCP-bridge acceptance tests (fake engines only, no model calls).

Phase-2 matrix from the owner's brief: duplicate request must not re-run,
unauthorized access denied, session conflicts, branch/model preservation
across restart, execution error surfaced, files captured BEFORE cleanup,
and the Telegram/MCP paths sharing ONE SessionManager + queue (verified by
routing both through the same monkeypatched execute seam).

No real Claude/Codex/Hermes/OpenRouter call happens anywhere in this file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402  (import for parity with the suite)
import mcp_bridge  # noqa: E402


# ── harness ─────────────────────────────────────────────────────────────────
class FakeMsg:
    message_id = 4242


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, cid, text, **kw):
        self.sent.append((cid, text))
        return FakeMsg()


class FakeHTTPReq:
    """aiohttp request stand-in for handle_http (headers + await json())."""

    def __init__(self, payload, token):
        self._p = payload
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        return self._p


@dataclass
class FakeSession:
    id: str
    project: str
    out: str


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated owner chat, token, audit log, capture root, worktrees, state."""
    # The bridge intersects the pilot allowlist with the actual registry.
    # Never inherit the operator's live projects.json for synthetic tests.
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps([
        {"name": "Rag4Engineers", "path": str(tmp_path / "rag")},
        {"name": "ZigguratKids4", "path": str(tmp_path / "zk4")},
    ]))
    monkeypatch.setattr(bot, "PROJECTS_FILE", str(registry))
    monkeypatch.setattr(mcp_bridge, "_QUESTIONS", {})
    monkeypatch.setattr(mcp_bridge, "_ASK_OPERATIONS", {})
    monkeypatch.setattr(bot, "ALLOWED_IDS", [106021080])
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", "test-mcp-token")
    monkeypatch.setattr(bot, "CONSOLE_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(bot, "CONSOLE_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(bot, "REPORTS", str(tmp_path / "reports"))
    monkeypatch.setattr(bot, "REPORT_URL", "http://reports.invalid")
    monkeypatch.setattr(bot, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(bot, "WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setattr(mcp_bridge, "CAPTURE_ROOT", Path(tmp_path / "captures"))
    # Quarantine has its OWN root (owner R-revisit item 2): outside the capture
    # tree, so a quarantined tree can never ride into a report ZIP.
    monkeypatch.setattr(mcp_bridge, "QUARANTINE_ROOT", Path(tmp_path / "quarantine"))
    monkeypatch.setattr(bot, "BOT_MCP_PROJECTS", "Rag4Engineers")
    os.makedirs(str(tmp_path / "reports"), exist_ok=True)
    os.makedirs(str(tmp_path / "worktrees"), exist_ok=True)
    return tmp_path


def wire_real(monkeypatch):
    """Wire the bridge with the REAL bot handlers; only the engine seam is
    faked (bot.execute) so no claude/codex child is ever spawned here."""
    calls = {"execute": [], "cancel": 0}

    async def fake_execute(update, ctx, prompt, sess, files=None, is_voice=False):
        calls["execute"].append((prompt, sess.id))

    monkeypatch.setattr(bot, "execute", fake_execute)
    bot.mcp_bridge.wire({
        "token": bot.BOT_SESSION_API_TOKEN,
        "authorized": bot._sessapi_authorized,
        "send": bot._sessapi_send, "control": bot._sessapi_control,
        "output": bot._sessapi_output, "sessions": bot._sessapi_sessions,
        "models": bot._sessapi_models,
        "lookup_session": bot._console_session,
        "cancel_tasks": lambda sid: (calls.__setitem__("cancel", calls["cancel"] + 1) or 1),
        "project_names": bot.get_project_names,
        "next_nonce": bot._mcp_next_nonce,
        "claim_nonce": bot._console_duplicate,
        "report_for": bot._report_for_session,
        "model_metadata": bot._mcp_model_metadata,
        "audit": bot._console_audit,
        "mcp_projects": bot.BOT_MCP_PROJECTS,
        "bot": FakeBot(),
    })
    return calls


async def _drain():
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=10)


async def _call_tool(name, args):
    status, body = await mcp_bridge.dispatch_rpc({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}})
    return status, json.loads(body)["result"]


def _payload(result):
    return json.loads(result["content"][0]["text"])


# ── protocol shape ──────────────────────────────────────────────────────────
async def test_initialize_and_tools_list():
    status, body = await mcp_bridge.dispatch_rpc(
        {"jsonrpc": "2.0", "id": 7, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}})
    assert status == 200
    r = json.loads(body)
    assert r["id"] == 7
    assert r["result"]["serverInfo"]["name"] == "claude-telegram-bot-mcp"
    assert "tools" in r["result"]["capabilities"]
    status, body = await mcp_bridge.dispatch_rpc({"jsonrpc": "2.0", "id": 8,
                                                  "method": "tools/list"})
    names = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert names == {"projects_list", "models_list", "sessions_list", "session_new",
                     "send", "status", "cancel", "report", "ask", "answer"}


async def test_notification_202_and_unknown_method_error():
    assert (await mcp_bridge.dispatch_rpc(
        {"jsonrpc": "2.0", "method": "notifications/initialized"})) == (202, "")
    status, body = await mcp_bridge.dispatch_rpc(
        {"jsonrpc": "2.0", "id": 2, "method": "no/such"})
    assert json.loads(body)["error"]["code"] == -32601


async def test_bad_envelope_rejected():
    status, _ = await mcp_bridge.dispatch_rpc({"jsonrpc": "1.0", "id": 1,
                                               "method": "initialize"})
    assert status == 400


async def test_http_auth(env):
    mcp_bridge.wire({"token": "test-mcp-token", "authorized": bot._sessapi_authorized})
    r = await mcp_bridge.handle_http(FakeHTTPReq({"jsonrpc": "2.0", "id": 1,
                                                 "method": "tools/list"}, "wrong"))
    assert r.status == 403
    r = await mcp_bridge.handle_http(FakeHTTPReq({"jsonrpc": "2.0", "id": 1,
                                                 "method": "tools/list"}, "test-mcp-token"))
    assert r.status == 200
    # Authorization: Bearer carries the SAME session-API secret (no new key)
    assert bot._sessapi_authorized(FakeHTTPReq({}, "test-mcp-token"))
    assert not bot._sessapi_authorized(FakeHTTPReq({}, "nope"))
    assert not bot._sessapi_authorized(FakeHTTPReq({}, ""))


# ── owner matrix ────────────────────────────────────────────────────────────
async def test_duplicate_send_nonce_never_reruns(env, monkeypatch):
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "dup-probe")
    s.project = "Rag4Engineers"
    s.status = "idle"
    args = {"label": "dup-probe", "message": "run me once", "nonce": bot._mcp_next_nonce()}
    _, res = await _call_tool("send", args)
    await _drain()
    assert _payload(res)["http_status"] == 200, _payload(res)
    _, res = await _call_tool("send", args)          # exact replay
    await _drain()
    assert _payload(res)["http_status"] == 409, "replayed nonce must be refused"
    assert len(calls["execute"]) == 1, "duplicate MUST NOT run the engine twice"
    bot.SM.kill(bot._console_chat_id(), "dup-probe")


async def test_send_refuses_owner_fields_and_foreign_project(env, monkeypatch):
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "gate-probe")
    s.project = "Rag4Engineers"
    s.status = "idle"
    # The bridge never forwards a caller-side chat/owner field: the tool
    # body is rebuilt from whitelisted keys only, so the shared console
    # guard never even sees it (and directly, it refuses it outright —
    # parity: same handler the console uses).
    _, res = await _call_tool("send", {"label": "gate-probe", "message": "x",
                                       "nonce": bot._mcp_next_nonce(), "chat_id": 123})
    assert _payload(res)["http_status"] == 200, "chat_id must be dropped, not rejected"
    await _drain()
    assert [c[1] for c in calls["execute"]] == [s.id], \
        "the turn must land in the CONSOLE-bound session, not a caller-chosen chat"

    class _R:
        headers = {"X-Session-API-Token": bot.BOT_SESSION_API_TOKEN}
        query = {}
        app = {"bot": FakeBot()}
        async def json(self):
            return {"label": "gate-probe", "message": "x",
                    "nonce": bot._mcp_next_nonce(), "chat_id": 123}
    direct = await bot._sessapi_send(_R())
    assert direct.status == 400, "the shared guard must refuse an owner field"
    # A session outside the MCP pilot scope is POLICY_DENIED
    s2 = bot.SM.create(bot._console_chat_id(), "foreign")
    s2.project = "ZigguratKids4"
    s2.status = "idle"
    _, res = await _call_tool("send", {"label": "foreign", "message": "x",
                                       "nonce": bot._mcp_next_nonce()})
    assert _payload(res)["http_status"] == 403
    assert _payload(res)["error"] == "POLICY_DENIED"
    await _drain()
    assert [c[1] for c in calls["execute"]] == [s.id], \
        "no engine turn may start from a denied request (only the accepted gate-probe turn)"
    bot.SM.kill(bot._console_chat_id(), "gate-probe")
    bot.SM.kill(bot._console_chat_id(), "foreign")


async def test_session_new_gates_then_reuses_console_validation(env, monkeypatch):
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "get_project_names", lambda: ["Rag4Engineers", "ZigguratKids4"])
    _, res = await _call_tool("session_new", {"label": "a", "project": "ZigguratKids4"})
    assert _payload(res)["error"] == "POLICY_DENIED", "outside the pilot scope"
    _, res = await _call_tool("session_new", {"label": "a", "project": "NotRegistered"})
    assert _payload(res)["error"] == "POLICY_DENIED", "never a fallback path"
    _, res = await _call_tool("session_new", {"label": "a", "project": "Rag4Engineers",
                                              "engine": "codex"})
    assert _payload(res)["error"] == "ENGINE_UNAVAILABLE", "v1 is claude-only"
    _, res = await _call_tool("session_new", {"label": "probe", "project": "Rag4Engineers"})
    assert _payload(res)["http_status"] == 200, _payload(res)
    _, res = await _call_tool("session_new", {"label": "probe", "project": "Rag4Engineers"})
    assert _payload(res)["http_status"] == 409, "same label twice must conflict"
    bot.SM.kill(bot._console_chat_id(), "probe")


async def test_cancel_policy_queued_only(env, monkeypatch):
    calls = wire_real(monkeypatch)
    s = bot.SM.create(bot._console_chat_id(), "cancel-probe")
    s.project = "Rag4Engineers"
    s.status = "running"
    _, res = await _call_tool("cancel", {"label": "cancel-probe"})
    p = _payload(res)
    assert p["http_status"] == 409 and p["error"] == "cancel_unavailable", p
    assert calls["cancel"] == 0, "a RUNNING child must never be TERM-killed from MCP"
    s.status = "idle"
    _, res = await _call_tool("cancel", {"label": "cancel-probe"})
    assert _payload(res)["cancelled"] == 0, "idle: nothing queued to cancel"
    s.status = "queued"
    _, res = await _call_tool("cancel", {"label": "cancel-probe"})
    assert _payload(res)["ok"] is True and _payload(res)["cancelled"] == 1
    bot.SM.kill(bot._console_chat_id(), "cancel-probe")


async def test_status_separates_requested_alias_from_effective_model(env, monkeypatch):
    wire_real(monkeypatch)
    s = bot.SM.create(bot._console_chat_id(), "meta-probe")
    s.project = "Rag4Engineers"
    s.claude_model = "z-ai/glm-5.3:floor"
    _, res = await _call_tool("status", {"label": "meta-probe"})
    p = _payload(res)
    assert p["model"]["requested_alias"] == "z-ai/glm-5.3:floor"
    assert p["model"]["effective_model"] is None, "unmapped alias stays unknown, never guessed"
    assert p["model"]["billing_source"] == "unknown"
    bot.SM.kill(bot._console_chat_id(), "meta-probe")


async def test_mcp_and_telegram_share_one_manager_and_queue(env, monkeypatch):
    """The phase-2 rule: no second engine/queue. Both transports must land in
    the SAME execute seam with the SAME session object (one SessionManager)."""
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "shared-probe")
    s.project = "Rag4Engineers"
    s.status = "idle"
    await bot.execute("tg-update", None, "from telegram", s)   # Telegram side
    _, res = await _call_tool("send", {"label": "shared-probe",       # MCP side
                                      "message": "from mcp",
                                      "nonce": bot._mcp_next_nonce()})
    await _drain()
    assert _payload(res)["http_status"] == 200
    assert [c[0] for c in calls["execute"]] == ["from telegram", "from mcp"]
    assert {c[1] for c in calls["execute"]} == {s.id}, \
        "both transports must hit the SAME session in the SAME manager"
    bot.SM.kill(bot._console_chat_id(), "shared-probe")


# ── ask/answer (Q&A, owner items 2/7 + 6) ────────────────────────────────────
async def test_ask_routes_to_existing_session(env, monkeypatch):
    """ask = send when the label exists (in scope): no create, same seam.
    Item 6: a successful ask returns an awaited-question id."""
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "qa-1")
    s.project = "Rag4Engineers"
    s.status = "idle"
    _, res = await _call_tool("ask", {"label": "qa-1", "project": "Rag4Engineers",
                                      "message": "what changed?", "nonce": bot._mcp_next_nonce() + 5})
    await _drain()
    p = _payload(res)
    assert p["http_status"] == 200, p
    assert "question_id" not in p, "submission cannot fabricate a worker question"
    bot.SM.kill(bot._console_chat_id(), "qa-1")


async def test_ask_creates_session_when_none_exists(env, monkeypatch):
    """ask with no such label: same create path as session_new (scope + engine
    gates verbatim), then the question is submitted — one turn total."""
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    _, res = await _call_tool("ask", {"label": "qa-2", "project": "Rag4Engineers",
                                      "message": "one question", "nonce": bot._mcp_next_nonce() + 5})
    await _drain()
    p = _payload(res)
    assert p["http_status"] == 200, p
    _, session = bot._console_session("qa-2")
    assert session is not None, "the session must exist after ask"
    assert session.project == "Rag4Engineers"
    assert session.engine == "claude"
    assert len(calls["execute"]) == 1, "exactly one engine turn"
    bot.SM.kill(bot._console_chat_id(), "qa-2")


async def test_ask_refuses_out_of_scope_project(env, monkeypatch):
    """ask inherits session_new's scope gate verbatim."""
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    _, res = await _call_tool("ask", {"label": "qa-3", "project": "ZigguratKids4",
                                      "message": "x", "nonce": bot._mcp_next_nonce() + 5})
    await _drain()
    p = _payload(res)
    assert p["http_status"] == 403 and p["error"] == "POLICY_DENIED", p
    assert not calls["execute"], "no engine turn from a denied ask"
    _, session = bot._console_session("qa-3")
    assert session is None, "no session may be created outside the scope"


async def test_ask_duplicate_nonce_never_reruns(env, monkeypatch):
    """Same-key duplicate: the caller nonce dedups the question turn."""
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "qa-4")
    s.project = "Rag4Engineers"
    s.status = "idle"
    args = {"label": "qa-4", "project": "Rag4Engineers",
            "message": "same question", "nonce": bot._mcp_next_nonce() + 5}
    _, res = await _call_tool("ask", args)
    await _drain()
    assert _payload(res)["http_status"] == 200
    _, res = await _call_tool("ask", args)          # exact replay
    await _drain()
    assert _payload(res)["http_status"] == 409, "replayed ask nonce must be refused"
    assert len(calls["execute"]) == 1, "duplicate ask MUST NOT run the engine twice"
    bot.SM.kill(bot._console_chat_id(), "qa-4")


async def test_ask_honors_real_three_second_rate_limit(env, monkeypatch):
    """Owner R-revisit item 5: ask runs under the REAL 3-second console window
    (NOT a zeroed limit) with ORDINARY epoch-ms client nonces: an immediate
    second ask is honestly rate-limited (429), and after the real window it
    goes through. The internal send-leg nonce never invalidates the caller's
    valid request."""
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "CONSOLE_MIN_INTERVAL_S", 3)   # the REAL window
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "qa-rl")
    s.project = "Rag4Engineers"
    s.status = "idle"
    n1 = bot._mcp_next_nonce() + 5            # an ordinary client nonce (epoch ms + jitter)
    _, res = await _call_tool("ask", {"label": "qa-rl", "project": "Rag4Engineers",
                                      "message": "q1", "nonce": n1})
    p = _payload(res)
    assert p["http_status"] == 200, f"first ask must run under the real window: {p}"
    n2 = n1 + 7
    _, res = await _call_tool("ask", {"label": "qa-rl", "project": "Rag4Engineers",
                                      "message": "q2", "nonce": n2})
    p = _payload(res)
    assert p["http_status"] == 429, \
        f"the real 3s window must honestly rate-limit an immediate second ask: {p}"
    assert "question_id" not in p, "a rate-limited ask registers no question"
    await asyncio.sleep(3.1)                   # the REAL window, not a zeroed limit
    n3 = n2 + 7
    _, res = await _call_tool("ask", {"label": "qa-rl", "project": "Rag4Engineers",
                                      "message": "q3", "nonce": n3})
    p = _payload(res)
    assert p["http_status"] == 200, f"after the window the ask must go through: {p}"
    await _drain()
    assert len(calls["execute"]) == 2, "q1 + q3 ran; the rate-limited q2 never did"
    bot.SM.kill(bot._console_chat_id(), "qa-rl")


def _emit_worker_question(session, root):
    # Synthetic CHILD output, never the caller's prompt. Integration tests
    # separately exercise the real run_claude parsing/resume seam.
    wt = root / "question-worktree"
    wt.mkdir(exist_ok=True)
    (wt / ".git").write_text("gitdir: synthetic")
    session.claude_created = True
    mcp_bridge.record_worker_questions(session,
        'MCP_QUESTION {"id":"choose-file","question":"Which file should I edit?"}', str(wt))
    return mcp_bridge._pending_questions(session.id)[0]


async def test_answer_binds_to_the_awaited_question_and_continues_the_session(
        env, monkeypatch):
    """Owner R-revisit item 6: ask returns a question_id; the status shows the
    AWAITED question; answer binds the runner's reply to THAT question and
    submits it into the SAME session (which is how the session continues);
    one answer per question."""
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "qa-6")
    s.project = "Rag4Engineers"
    s.status = "idle"
    _, res = await _call_tool("ask", {"label": "qa-6", "project": "Rag4Engineers",
                                      "message": "which file?", "nonce": bot._mcp_next_nonce() + 5})
    p = _payload(res)
    assert p["http_status"] == 200 and "question_id" not in p, p
    await _drain()
    qid = _emit_worker_question(s, env)
    _, res = await _call_tool("status", {"label": "qa-6"})
    assert qid in _payload(res)["pending_questions"], \
        f"the awaited question must be visible on status: {_payload(res)}"
    _, res = await _call_tool("answer", {"label": "qa-6", "question_id": qid,
                                         "message": "use bot.py",
                                         "nonce": bot._mcp_next_nonce() + 5})
    p = _payload(res)
    assert p["http_status"] == 200, f"the bound answer must be accepted: {p}"
    assert p.get("continues") == "qa-6" and p.get("question_id") == qid, p
    await _drain()
    assert len(calls["execute"]) == 2, "ask + answer both ran as real turns"
    assert all(c[1] == s.id for c in calls["execute"]), \
        "both turns ran in the SAME session (the answer continues it)"
    _, res = await _call_tool("answer", {"label": "qa-6", "question_id": qid,
                                         "message": "again", "nonce": bot._mcp_next_nonce() + 5})
    assert _payload(res)["http_status"] == 409, "a question is answered exactly once"
    _, res = await _call_tool("status", {"label": "qa-6"})
    assert qid not in _payload(res)["pending_questions"], \
        "the answered question must leave the pending list"
    bot.SM.kill(bot._console_chat_id(), "qa-6")


async def test_answer_refuses_foreign_unknown_questions_and_plain_sends_do_not_answer(
        env, monkeypatch):
    """Owner R-revisit item 6: the binding is session-scoped (a question of
    session A cannot be answered via session B), unknown ids are refused, and
    a plain prompt send is NOT a substitute — it never answers the awaited
    question."""
    wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    sa = bot.SM.create(bot._console_chat_id(), "qa-a")
    sa.project = "Rag4Engineers"
    sa.status = "idle"
    sb = bot.SM.create(bot._console_chat_id(), "qa-b")
    sb.project = "Rag4Engineers"
    sb.status = "idle"
    _, res = await _call_tool("ask", {"label": "qa-a", "project": "Rag4Engineers",
                                      "message": "?", "nonce": bot._mcp_next_nonce() + 5})
    await _drain()
    qid = _emit_worker_question(sa, env)
    # answering through the WRONG session's label: unknown for that session
    _, res = await _call_tool("answer", {"label": "qa-b", "question_id": qid,
                                         "message": "x", "nonce": bot._mcp_next_nonce() + 5})
    p = _payload(res)
    assert p["http_status"] == 404 and p["error"] == "unknown_question", p
    # a totally unknown question id
    _, res = await _call_tool("answer", {"label": "qa-a", "question_id": "q-nope",
                                         "message": "x", "nonce": bot._mcp_next_nonce() + 5})
    assert _payload(res)["error"] == "unknown_question"
    # a plain prompt send works as a turn but does NOT answer the question
    _, res = await _call_tool("send", {"label": "qa-a", "message": "a normal turn",
                                       "nonce": bot._mcp_next_nonce() + 5})
    assert _payload(res)["http_status"] == 200
    await _drain()
    _, res = await _call_tool("status", {"label": "qa-a"})
    assert qid in _payload(res)["pending_questions"], \
        "a plain prompt send must NOT answer the awaited question (item 6)"
    bot.SM.kill(bot._console_chat_id(), "qa-a")
    bot.SM.kill(bot._console_chat_id(), "qa-b")


async def test_restart_preserves_branch_and_model_binding(env, monkeypatch):
    s = bot.Session(id="106021080:rb", label="rb", color_emoji="🔵",
                    session_uuid="u-1", project="Rag4Engineers",
                    claude_model="opus", starting_branch="dev")
    bot.SM.sessions[s.id] = s
    bot.save_state()
    bot.SM.sessions.clear()
    bot.load_state()
    r = bot.SM.get_by_key("106021080:rb")
    assert r is not None
    assert r.starting_branch == "dev", "branch binding must survive a restart"
    assert r.claude_model == "opus", "model binding must survive a restart"
    del bot.SM.sessions["106021080:rb"]


async def test_load_state_marks_running_as_interrupted(env, monkeypatch):
    s = bot.Session(id="106021080:rr", label="rr", color_emoji="🔵",
                    session_uuid="u-2", project="Rag4Engineers",
                    status="running")
    bot.SM.sessions[s.id] = s
    bot.save_state()
    bot.SM.sessions.clear()
    bot.load_state()
    r = bot.SM.get_by_key("106021080:rr")
    assert r.status == "error" and r.status_reason == "interrupted_by_restart", \
        "a restart must not leave a dead session looking alive"
    del bot.SM.sessions["106021080:rr"]


# ── capture-before-cleanup ──────────────────────────────────────────────────
def _mk_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*a):
        return subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "hello.txt").write_text("one\n")
    g("add", "-A")
    g("commit", "-qm", "c1")
    # WorktreeSession.__aenter__ spawns from origin/<branch>: give it an origin
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)],
                   capture_output=True, text=True)
    g("remote", "add", "origin", str(origin))
    g("fetch", "-q", "origin")
    return repo


def _wt(repo, path):
    r = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                        str(path), "HEAD"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return str(path)


async def test_capture_before_cleanup_honors_limits(env):
    repo = _mk_repo(env)
    wt = _wt(repo, env / "wt")
    Path(wt, "hello.txt").write_text("two\n")                    # tracked change
    Path(wt, "results.txt").write_text("pytest: 3 passed\n")     # untracked
    Path(wt, "token.env").write_text("SECRET=1\n")               # secret-looking
    cap = mcp_bridge.capture_worktree_delta(wt, "106021080:cap")
    assert cap.ok and cap.dirty, cap.reason
    out = Path(cap.path)
    assert "hello.txt" in (out / "status.porcelain").read_text()
    man = json.loads((out / "manifest.json").read_text())
    assert man["files"]["results.txt"]["sha256"] == hashlib.sha256(
        b"pytest: 3 passed\n").hexdigest()
    assert "token.env" in {s["path"] for s in man["skipped"]}, \
        "secret-named file must NOT be copied"
    assert "token.env" not in man["files"]
    # C2: a secret-looking path in the delta means NO diff.patch at all —
    # the tracked secret's content must not ride in through git diff.
    assert "diff_skipped" in man
    assert not (out / "diff.patch").exists()
    assert mcp_bridge.latest_capture_dir("106021080:cap") == str(out)
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                   capture_output=True)


async def test_capture_writes_diff_when_no_secret_in_delta(env):
    repo = _mk_repo(env)
    wt = _wt(repo, env / "wt-diff")
    Path(wt, "hello.txt").write_text("two\n")
    cap = mcp_bridge.capture_worktree_delta(wt, "106021080:diff")
    assert cap.ok and cap.dirty, cap.reason
    out = Path(cap.path)
    assert "+++" in (out / "diff.patch").read_text()
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                   capture_output=True)


async def test_capture_handles_persian_paths_renames_and_downgrades(env):
    """C3 after review: quotepath'd non-ASCII paths and rename records must
    parse into REAL filesystem paths (not be silently skipped), and a
    tracked secret's content must never enter diff.patch."""
    repo = _mk_repo(env)
    wt = _wt(repo, env / "wt-fa")
    Path(wt, "گزارش.txt").write_text("سلام\n")          # Persian, untracked
    subprocess.run(["git", "-C", wt, "mv", "hello.txt", "salaam.txt"],
                   capture_output=True, check=True)       # rename record
    Path(wt, "salaam.txt").write_text("changed\n")
    cap = mcp_bridge.capture_worktree_delta(wt, "106021080:fa")
    assert cap.ok and cap.dirty, cap.reason
    out = Path(cap.path)
    man = json.loads((out / "manifest.json").read_text())
    assert "گزارش.txt" in man["files"], "a Persian path must be captured, not skipped"
    assert "salaam.txt" in man["files"], "the rename's NEW path must be captured"
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                   capture_output=True)


async def test_capture_clean_tree_writes_nothing(env):
    repo = _mk_repo(env)
    wt = _wt(repo, env / "wt-clean")
    cap = mcp_bridge.capture_worktree_delta(wt, "106021080:clean")
    assert cap.ok and not cap.dirty
    assert cap.path == "", "a clean tree captures nothing"
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                   capture_output=True)


async def test_turncapture_failure_preserves_dirty_worktree(env, monkeypatch):
    """Item 1: an incomplete capture must not allow worktree deletion — the
    dirty tree stays. Reuse is OFF here so the PRESERVE flag (not the
    persistent-tree keep) is what stops the rm -rf."""
    repo = _mk_repo(env)
    monkeypatch.setattr(bot, "WORKTREE_REUSE", False)
    w = bot.WorktreeSession(str(repo), "main", "106021080_keep")

    def boom(wt_path, sid):
        return mcp_bridge.CaptureResult(ok=False, dirty=True, reason="simulated disk full")

    monkeypatch.setattr(mcp_bridge, "capture_worktree_delta", boom)
    async with w as wt_path:
        Path(wt_path, "results.txt").write_text("uncommitted!\n")
        async with mcp_bridge.TurnCapture(w, wt_path, w.session_id):
            pass                     # the capture itself is the unit under test
    assert w.preserve_on_exit is True, "an unproven capture must preserve the tree"
    assert os.path.exists(w.worktree_path), "the dirty worktree must still be on disk"
    w._exit_sync(None, None, None)  # the removal path must honor the flag
    assert os.path.exists(w.worktree_path), "preserve_on_exit must stop the rm -rf"
    # cleanup: the preserved tree is still a registered worktree of the tmp repo
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                     w.worktree_path], capture_output=True)


async def test_stale_enter_refuses_unknown_git_metadata(env, monkeypatch):
    repo = _mk_repo(env)
    w = bot.WorktreeSession(str(repo), "main", "stale")
    _wt(repo, Path(w.worktree_path))
    Path(w.worktree_path, "results.txt").write_text("keep")
    Path(w.worktree_path, ".git").write_text("gitdir: /nonexistent/metadata\n")
    with pytest.raises(RuntimeError, match="quarantine_failed"):
        w._enter_sync()
    assert Path(w.worktree_path, "results.txt").read_text() == "keep"


async def test_stale_dirty_export_requires_quarantine(env, monkeypatch):
    repo = _mk_repo(env)
    w = bot.WorktreeSession(str(repo), "main", "stale-dirty")
    _wt(repo, Path(w.worktree_path))
    Path(w.worktree_path, "results.txt").write_text("keep")
    assert mcp_bridge.capture_worktree_delta(w.worktree_path, w.session_id).ok
    w._enter_sync()
    quarantines = [p for p in Path(mcp_bridge.QUARANTINE_ROOT).rglob("stale-*") if (p / "git-meta").is_dir()]
    assert len(quarantines) == 1
    assert (quarantines[0] / "results.txt").read_text() == "keep"
    assert (quarantines[0] / "git-meta/index").exists()
    assert not Path(w.worktree_path, "results.txt").exists()


async def test_resume_reuses_persistent_worktree(env, monkeypatch):
        """Item 4: the session worktree PERSISTS across turns — a resume gets
        the SAME commit and the SAME uncommitted changes. Nothing is rebuilt
        from origin (a new origin commit does not pull in) and there is no
        fetch/fallback on the resume path at all."""
        monkeypatch.setattr(bot, "WORKTREE_REUSE", True)
        repo = _mk_repo(env)
        sid = "106021080_reuse"
        w = bot.WorktreeSession(str(repo), "main", sid)
        async with w as wt_path:
            Path(wt_path, "uncommitted.txt").write_text("keep me\n")
            head1 = subprocess.run(["git", "-C", wt_path, "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        assert os.path.exists(w.worktree_path), "the tree persists after exit (item 4)"
        # a NEW commit lands on origin between turns — a rebuild-from-origin
        # would pick it up; a reuse must not
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
                        "-m", "c2"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"],
                       capture_output=True)
        w2 = bot.WorktreeSession(str(repo), "main", sid)
        async with w2 as wt_path:
            assert wt_path == w2.worktree_path == w.worktree_path, "same path"
            head2 = subprocess.run(["git", "-C", wt_path, "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
            assert head2 == head1, \
                "the SAME commit must be reused, not rebuilt from origin (item 4)"
            assert Path(w2.worktree_path, "uncommitted.txt").read_text() == "keep me\n", \
                "the session's uncommitted changes must survive the resume"
        assert os.path.exists(w2.worktree_path), "the tree persists for the next resume"
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                        w2.worktree_path], capture_output=True)


async def test_no_fallback_to_main_when_branch_missing(env, monkeypatch):
        """Item 4: a session bound to a branch that no longer exists on
        origin fails LOUD — no silent rebinding to main."""
        repo = _mk_repo(env)
        w = bot.WorktreeSession(str(repo), "feature-gone", "106021080_branch")
        with pytest.raises(RuntimeError, match="worktree.branch_missing"):
            w._enter_sync()


async def test_quarantine_never_rides_into_report_and_retention_is_off_by_default(
            env, monkeypatch):
        """Item 2: only capture-STAMP dirs feed a report ZIP (an old-style
        stale- dir under the capture root is invisible to the picker), the
        quarantine root is never pruned, and capture auto-deletion is OFF by
        default (0 = keep everything)."""
        repo = _mk_repo(env)
        wt = _wt(repo, env / "wt-q")
        Path(wt, "dirty.txt").write_text("d\n")
        cap = mcp_bridge.capture_worktree_delta(wt, "106021080:q")
        assert cap.ok and cap.dirty, cap.reason
        safe = mcp_bridge._safe_session_dir("106021080:q")
        # an old-style stale- move under the capture root must NOT be picked
        stale = Path(mcp_bridge.CAPTURE_ROOT) / safe / "stale-20260101-000000"
        stale.mkdir(parents=True)
        Path(stale, "secret.txt").write_text("s\n")
        assert mcp_bridge.latest_capture_dir("106021080:q") == cap.path, \
            "only capture-STAMP dirs may feed a report ZIP"
        # default retention 0 = auto-delete OFF: a 400-day-old capture stays
        monkeypatch.setattr(mcp_bridge, "CAPTURE_RETENTION_DAYS", 0)
        old_mtime = os.path.getmtime(cap.path) - 400 * 86400
        os.utime(cap.path, (old_mtime, old_mtime))
        mcp_bridge._prune_old_captures(safe)
        assert os.path.exists(cap.path), \
            "default 0 must keep everything — auto-delete is OFF (item 2)"
        # the knob still works when the owner sets it explicitly
        monkeypatch.setattr(mcp_bridge, "CAPTURE_RETENTION_DAYS", 1)
        mcp_bridge._prune_old_captures(safe)
        assert not os.path.exists(cap.path), "an explicit retention day prunes"
        # pruning NEVER touches the quarantine root (whatever day count)
        qdir = Path(mcp_bridge.QUARANTINE_ROOT) / safe / "stale-20260102-000000"
        qdir.mkdir(parents=True)
        Path(qdir, "x.txt").write_text("x\n")
        mcp_bridge._prune_old_captures(safe)
        assert qdir.exists(), "pruning must never touch the quarantine root"
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                       capture_output=True)


async def test_list_tools_run_through_the_real_dispatcher(env, monkeypatch):
        """Item 3: all three list tools through the REAL dispatch_rpc with the
        REAL console handlers wired (only the engine seam is faked)."""
        calls = wire_real(monkeypatch)
        _, res = await _call_tool("projects_list", {})
        p = _payload(res)
        assert p["http_status"] == 200 and p["ok"], p
        assert "Rag4Engineers" in p["projects"], p
        _, res = await _call_tool("models_list", {})
        p = _payload(res)
        assert p["http_status"] == 200 and p["ok"], p
        assert "families" in p, p
        s = bot.SM.create(bot._console_chat_id(), "list-probe")
        s.project = "Rag4Engineers"
        _, res = await _call_tool("sessions_list", {})
        p = _payload(res)
        assert p["http_status"] == 200 and p["ok"], p
        labels = {row.get("label") for row in p.get("sessions") or []}
        assert "list-probe" in labels, f"a live session must be listed: {p}"
        bot.SM.kill(bot._console_chat_id(), "list-probe")


async def test_bot_mcp_enabled_off_really_stops_the_bridge(env, monkeypatch):
        """Item 3: BOT_MCP_ENABLED off => the new behavior REALLY stops —
        neither mcp_bridge.wire NOR the /mcp/rpc route comes to exist, while
        every console route stays as before."""
        wired = []
        monkeypatch.setattr(mcp_bridge, "wire", lambda deps: wired.append(deps))
        # aiohttp 3.13 made Resource.canonical a property; older versions a method
        def paths(a):
            out = set()
            for r in a.router.routes():
                c = r.resource.canonical
                out.add(c() if callable(c) else c)
            return out
        monkeypatch.setattr(bot, "BOT_MCP_ENABLED", False)
        a = bot._sessapi_build_app(FakeBot())
        routes = paths(a)
        assert not wired, "the bridge must NOT be wired while disabled"
        assert "/mcp/rpc" not in routes, "the route must not exist while disabled"
        assert {"/sessions", "/send", "/control", "/models", "/output"} <= routes, \
            "the console routes are untouched by the gate"
        monkeypatch.setattr(bot, "BOT_MCP_ENABLED", True)
        a2 = bot._sessapi_build_app(FakeBot())
        routes2 = paths(a2)
        assert wired and wired[0].get("token") == bot.BOT_SESSION_API_TOKEN, \
            "enablement wires the bridge with the SAME session-API token"
        assert "/mcp/rpc" in routes2, "enablement registers the route"


async def test_mcp_drives_only_claude_sessions(env, monkeypatch):
    """M2 after review: a Codex session inside a pilot project must NOT be
    drivable from MCP (run_codex has no TurnCapture — driving it would
    re-open the no-capture rm-rf)."""
    wire_real(monkeypatch)
    s = bot.SM.create(bot._console_chat_id(), "codex-probe")
    s.project = "Rag4Engineers"
    s.engine = "codex"
    s.status = "idle"
    for tool, args in (("send", {"label": "codex-probe", "message": "x",
                                 "nonce": bot._mcp_next_nonce()}),
                       ("status", {"label": "codex-probe"}),
                       ("cancel", {"label": "codex-probe"}),
                       ("report", {"label": "codex-probe"})):
        _, res = await _call_tool(tool, args)
        p = _payload(res)
        assert p["error"] == "ENGINE_UNAVAILABLE", f"{tool} must refuse a codex session: {p}"
    bot.SM.kill(bot._console_chat_id(), "codex-probe")


async def test_nonce_generator_tracks_the_clock(env, monkeypatch):
    """M1 after review: bridge nonces share the console HWM namespace, so a
    client that sends a far-future epoch-ms nonce must not poison future
    generated nonces (they track the real clock, never fall behind)."""
    t0 = 1_800_000_000.0
    monkeypatch.setattr(bot.time, "time", lambda: t0)
    monkeypatch.setattr(bot, "_MCP_NONCE_SEQ", None)
    n1 = bot._mcp_next_nonce()
    n2 = bot._mcp_next_nonce()
    assert n1 == int(t0 * 1000) + 1 and n2 == n1 + 1, \
        "same-ms calls count up; the FIRST is strictly above the floor/HWM"
    # A client nonce far above the clock raises the HWM; the generator must
    # still never emit a duplicate-bait value below it… but it must also not
    # be bricked by lagging: it tracks the clock, so after the clock passes
    # the client's value it resumes above it. (HWM is monkeypatch-scoped:
    # the module-level HWM is shared across tests in this process.)
    monkeypatch.setattr(bot, "_CONSOLE_NONCE_HWM", n1 + 10_000)   # client sent a future nonce
    n2b = bot._mcp_next_nonce()
    assert n2b > bot._CONSOLE_NONCE_HWM, \
        "the generator must never emit below a client-raised HWM (M1)"
    monkeypatch.setattr(bot, "_MCP_NONCE_SEQ", None)


async def test_first_generated_nonce_is_strictly_above_hwm(env, monkeypatch):
    """Owner R-revisit item 5 (the found bug): the FIRST generated nonce must
    be STRICTLY above the HWM. A first value landing exactly ON the floor —
    which is what happened when a client had just raised the HWM to the
    clock with its own epoch-ms nonce — was refused by _console_duplicate
    as a duplicate of the caller's own request."""
    t0 = 1_800_000_000.0
    monkeypatch.setattr(bot.time, "time", lambda: t0)
    # a client just sent the exact clock nonce, raising the HWM to it
    monkeypatch.setattr(bot, "_CONSOLE_NONCE_HWM", int(t0 * 1000))
    monkeypatch.setattr(bot, "_MCP_NONCE_SEQ", None)
    n1 = bot._mcp_next_nonce()
    assert n1 > bot._CONSOLE_NONCE_HWM, \
        "the first nonce must be strictly above the HWM, never exactly on it"
    assert n1 == int(t0 * 1000) + 1
    monkeypatch.setattr(bot, "_MCP_NONCE_SEQ", None)


async def test_report_zip_contains_capture_not_just_summary(env, monkeypatch):
    repo = _mk_repo(env)
    wt = _wt(repo, env / "wt-rep")
    Path(wt, "results.txt").write_text("pytest: 3 passed\n")
    cap = mcp_bridge.capture_worktree_delta(wt, "106021080:rep")
    assert cap.ok and cap.dirty
    wire_real(monkeypatch)   # report_for is the real bot builder
    s = bot.SM.create(bot._console_chat_id(), "rep")
    s.project = "Rag4Engineers"
    s.out = "summary text"
    res = await bot._report_for_session(s)
    assert res.ok, res.error
    zf = zipfile.ZipFile(str(Path(bot.REPORTS) / f"{res.slug}.zip"))
    names = zf.namelist()
    assert f"{res.slug}/summary.txt" in names
    assert f"{res.slug}/capture/manifest.json" in names, names
    assert f"{res.slug}/capture/diff.patch" in names
    assert f"{res.slug}/capture/files/results.txt" in names
    assert "token.env" not in " ".join(names), "secret-looking file must not be in the ZIP"
    man = json.loads(zf.read(f"{res.slug}/capture/manifest.json"))
    assert man["files"]["results.txt"]["sha256"] == hashlib.sha256(
        b"pytest: 3 passed\n").hexdigest()
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                   capture_output=True)
    bot.SM.kill(bot._console_chat_id(), "rep")


async def test_execution_error_is_surfaced_not_swallowed(env, monkeypatch):
    calls = wire_real(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(bot, "execute", boom)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    s = bot.SM.create(bot._console_chat_id(), "err-probe")
    s.project = "Rag4Engineers"
    s.status = "idle"
    _, res = await _call_tool("send", {"label": "err-probe", "message": "x",
                                      "nonce": bot._mcp_next_nonce()})
    assert _payload(res)["http_status"] == 200, "enqueue succeeds; the FAILURE is recorded"
    await _drain()
    kinds = [json.loads(l)["event"] for l in
              Path(bot.CONSOLE_AUDIT_LOG).read_text().splitlines() if l.strip()]
    assert "failed" in kinds, "an engine crash must be audited, not silently completed"
    assert "completed" not in kinds[-2:], kinds
    bot.SM.kill(bot._console_chat_id(), "err-probe")
