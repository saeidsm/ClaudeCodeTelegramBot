"""The console audit log must record SUCCESS, not only rejection.

Why this test exists: on 2026-08-21 an empty audit log was read as "no console
message has ever been sent". The log turned out to be correct, but the reasoning
was not - nothing had ever proven the log captures a *successful* send, so an
empty file was equally consistent with "it happened and we lost the record".

An audit log that silently misses successful commands is worse than no log: it
makes "nothing happened" and "something happened and we lost it" look identical.
That is the same failure class as block-env-edits.sh failing open for months.

So: assert the success path writes exactly one `send` and exactly one
`completed`, and that both land in the file the code actually uses.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


class FakeReq:
    """Minimal aiohttp-request stand-in."""

    def __init__(self, body, token, bot_obj=None):
        self._b = body
        self.headers = {"X-Session-API-Token": token}
        self.query = {}
        self.app = {"bot": bot_obj}

    async def json(self):
        return self._b


class FakeMsg:
    message_id = 4242


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, cid, text, **kw):
        self.sent.append((cid, text))
        return FakeMsg()


def _events(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


@pytest.fixture
def audit(tmp_path, monkeypatch):
    """Point the audit log at a temp file so the real one is untouched."""
    monkeypatch.setattr(bot, "ALLOWED_IDS", [106021080])
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", "synthetic-audit-token")
    monkeypatch.setattr(bot, "SM", bot.SessionManager())
    monkeypatch.setattr(bot, "STATE_FILE", str(tmp_path / "state.json"))
    p = tmp_path / "console-audit.log"
    monkeypatch.setattr(bot, "CONSOLE_AUDIT_LOG", str(p))
    return p


def _fresh_nonce():
    # strictly ABOVE the current high-water mark - _console_duplicate() rejects
    # anything <=, so returning the mark itself is always a duplicate.
    return bot._CONSOLE_NONCE_HWM + 1


def test_successful_send_writes_exactly_one_send_and_one_completed(audit, monkeypatch):
    cid = bot._console_chat_id()
    assert cid, "a bound chat is required for this test"

    session = bot.SM.create(cid, "audit-probe")
    session.project = "ClaudeCodeTelegramBot"
    session.status = "idle"

    ran = {}

    async def fake_execute(update, ctx, prompt, sess, files=None, is_voice=False):
        ran["prompt"] = prompt
        ran["label"] = sess.label

    monkeypatch.setattr(bot, "execute", fake_execute)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)

    fake_bot = FakeBot()
    req = FakeReq({"label": "audit-probe", "message": "hello from the console",
                   "nonce": _fresh_nonce()}, bot.BOT_SESSION_API_TOKEN, fake_bot)

    async def drive():
        resp = await bot._sessapi_send(req)
        # the handler fires execute() as a background task; let it finish
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        return resp

    resp = asyncio.run(drive())
    assert resp.status == 200, resp.body

    ev = _events(audit)
    kinds = [e["event"] for e in ev]
    assert kinds.count("send") == 1, f"expected exactly one 'send', got {kinds}"
    assert kinds.count("completed") == 1, f"expected exactly one 'completed', got {kinds}"

    sent = next(e for e in ev if e["event"] == "send")
    assert sent["session_label"] == "audit-probe"
    assert sent["message"] == "hello from the console"
    assert sent["ts"]

    # the enqueue really happened, so 'completed' is not a lie
    assert ran["prompt"] == "hello from the console"
    assert ran["label"] == "audit-probe"
    assert fake_bot.sent, "the console message should be echoed to Telegram"

    bot.SM.kill(cid, "audit-probe")


def test_audit_records_a_failed_run_as_failed_not_completed(audit, monkeypatch):
    cid = bot._console_chat_id()
    session = bot.SM.create(cid, "audit-fail")
    session.project = "ClaudeCodeTelegramBot"
    session.status = "idle"

    async def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(bot, "execute", boom)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)

    req = FakeReq({"label": "audit-fail", "message": "will fail",
                   "nonce": _fresh_nonce()}, bot.BOT_SESSION_API_TOKEN, FakeBot())

    async def drive():
        r = await bot._sessapi_send(req)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        return r

    asyncio.run(drive())
    kinds = [e["event"] for e in _events(audit)]
    assert kinds.count("send") == 1
    assert kinds.count("failed") == 1, f"a crash must be recorded, got {kinds}"
    assert "completed" not in kinds, "a failed run must not be logged as completed"

    bot.SM.kill(cid, "audit-fail")


def test_audit_path_is_the_one_the_code_uses(monkeypatch, tmp_path):
    """Guards against the log being written somewhere nobody reads."""
    p = tmp_path / "x.log"
    monkeypatch.setattr(bot, "CONSOLE_AUDIT_LOG", str(p))
    bot._console_audit("probe", "lbl", "msg", "extra")
    assert p.exists(), "audit wrote nothing"
    ev = _events(p)
    assert ev[0]["event"] == "probe" and ev[0]["session_label"] == "lbl"


def test_audit_failure_never_breaks_the_request(monkeypatch):
    """Auditing is best-effort at the call site, but must not raise."""
    monkeypatch.setattr(bot, "CONSOLE_AUDIT_LOG", "/nonexistent-dir/nope.log")
    bot._console_audit("probe", "lbl", "msg")   # must not raise
