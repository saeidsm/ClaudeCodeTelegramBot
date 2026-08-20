"""Prove blocking work offloaded via asyncio.to_thread does not freeze the event
loop (Section G / 'blocking behavior'). This is the pattern the report/health/
deploy/worktree paths now use instead of a bare subprocess.run."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402  (import for parity with the suite / sanity)


def _slow_blocking():
    time.sleep(0.3)     # stands in for a slow subprocess.run
    return "done"


@pytest.mark.asyncio
async def test_to_thread_does_not_block_loop():
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    result = await asyncio.to_thread(_slow_blocking)
    t.cancel()
    assert result == "done"
    # The loop kept scheduling the ticker during the 0.3s blocking call.
    assert ticks >= 5


@pytest.mark.asyncio
async def test_worktree_session_is_async_context_manager():
    # Guard: WorktreeSession must expose async enter/exit so its git/rsync work
    # runs off-loop (the Phase 1 change). A plain 'with' would reintroduce the
    # event-loop stall.
    assert hasattr(bot.WorktreeSession, "__aenter__")
    assert hasattr(bot.WorktreeSession, "__aexit__")
