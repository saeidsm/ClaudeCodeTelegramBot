"""Section 2 — _rate_limit_flow always removes its task registration.

Never invokes real Claude: run_claude and the Telegram bot are faked.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


class FakeMsg:
    async def delete(self):
        return True
    async def edit_text(self, *a, **k):
        return self


class FakeBot:
    async def send_message(self, *a, **k):
        return FakeMsg()


def _session():
    return bot.Session(id="123:rl", label="rl", color_emoji="🔵", session_uuid="u")


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    bot.CONC.init(max_running=2)
    monkeypatch.setattr(bot, "BOT", FakeBot())
    monkeypatch.setattr(bot, "RATE_LIMIT_RETRY_DELAY", 5)   # long enough to cancel mid-sleep
    async def _noop_track(*a, **k):
        return None
    monkeypatch.setattr(bot, "track_reply", _noop_track)
    yield


@pytest.mark.asyncio
async def test_cancel_during_retry_sleep(monkeypatch):
    sess = _session()

    async def fake_run(*a, **k):
        return "should-not-run"
    monkeypatch.setattr(bot, "run_claude", fake_run)

    t = asyncio.create_task(bot._rate_limit_flow("p", "proj", sess))
    await asyncio.sleep(0.05)                       # inside the RATE_LIMIT_RETRY_DELAY sleep
    assert t in bot.CONC.session_tasks.get("123:rl", set())
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert "123:rl" not in bot.CONC.session_tasks   # registration removed on cancel


@pytest.mark.asyncio
async def test_cancel_during_retry_execution(monkeypatch):
    sess = _session()
    monkeypatch.setattr(bot, "RATE_LIMIT_RETRY_DELAY", 0)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def fake_run(*a, **k):
        started.set()
        await gate.wait()                           # simulate a long run
        return "x"
    monkeypatch.setattr(bot, "run_claude", fake_run)

    t = asyncio.create_task(bot._rate_limit_flow("p", "proj", sess))
    await asyncio.wait_for(started.wait(), timeout=2)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert "123:rl" not in bot.CONC.session_tasks


@pytest.mark.asyncio
async def test_normal_completion_no_stale_entry(monkeypatch):
    sess = _session()
    monkeypatch.setattr(bot, "RATE_LIMIT_RETRY_DELAY", 0)

    async def fake_run(*a, **k):
        return "short successful output"            # != RL_LIMITED_SENTINEL, < 8000
    monkeypatch.setattr(bot, "run_claude", fake_run)

    await asyncio.wait_for(bot._rate_limit_flow("p", "proj", sess), timeout=3)
    assert "123:rl" not in bot.CONC.session_tasks   # finally discarded it
