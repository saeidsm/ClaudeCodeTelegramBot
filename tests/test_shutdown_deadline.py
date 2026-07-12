"""Section 2 — shutdown timeouts are strictly bounded.

A task that ignores cancellation must NOT let shutdown exceed the configured
deadline. asyncio.wait_for(gather(...)) could, because it awaits the gather's
own cancellation; asyncio.wait(tasks, timeout=...) returns AT the deadline with
(done, pending) and never awaits pending. This test proves the near-timeout
return with a genuinely non-cooperative task, using monotonic time, and cleans
the task up so pytest leaks nothing.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


@pytest.mark.asyncio
async def test_cancel_and_await_run_tasks_returns_near_timeout(capsys):
    sid = "shutdown_deadline_test"
    timeout = 0.5
    max_elapsed = 0.0
    for _ in range(5):
        gate = asyncio.Event()
        released = asyncio.Event()

        async def stubborn():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Ignore cancellation until the external test gate is released.
                await gate.wait()
                released.set()
                return  # cooperate afterwards

        bot.CONC.session_tasks.pop(sid, None)
        t = asyncio.ensure_future(stubborn())
        bot.CONC.session_tasks[sid].add(t)
        await asyncio.sleep(0.05)  # let it reach the sleep()

        t0 = time.monotonic()
        summary = await bot._cancel_and_await_run_tasks(timeout=timeout)
        elapsed = time.monotonic() - t0
        max_elapsed = max(max_elapsed, elapsed)

        assert summary["timed_out"] is True
        assert summary["non_cooperative"] == 1
        assert summary["awaited"] == 0
        # Returns NEAR the deadline, never hangs on the stubborn task.
        assert timeout - 0.1 <= elapsed < timeout + 0.8, f"elapsed={elapsed}"

        # Release the gate, await the task → no pytest leak.
        gate.set()
        await asyncio.wait_for(released.wait(), timeout=2)
        await asyncio.wait_for(t, timeout=2)
        bot.CONC.session_tasks.pop(sid, None)

    with capsys.disabled():
        print(f"\n[SHUTDOWN_DEADLINE] max_observed_elapsed={max_elapsed:.3f}s "
              f"(timeout={timeout}s, 5 runs)")


@pytest.mark.asyncio
async def test_cooperative_tasks_awaited_and_marked_clean():
    sid = "shutdown_coop_test"

    async def cooperative():
        await asyncio.sleep(3600)  # cancels cleanly

    bot.CONC.session_tasks.pop(sid, None)
    t = asyncio.ensure_future(cooperative())
    bot.CONC.session_tasks[sid].add(t)
    await asyncio.sleep(0.02)
    summary = await bot._cancel_and_await_run_tasks(timeout=1.0)
    assert summary["cancelled"] == 1
    assert summary["awaited"] == 1
    assert summary["timed_out"] is False
    assert summary["non_cooperative"] == 0
    assert t.cancelled()
    bot.CONC.session_tasks.pop(sid, None)


@pytest.mark.asyncio
async def test_reap_active_procs_one_total_deadline():
    """_reap_active_procs must use ONE total deadline across all processes, not a
    fresh full timeout per process (which stacks to N*timeout)."""

    class FakeProc:
        def __init__(self):
            self._ev = asyncio.Event()  # never set → wait() blocks forever

        async def wait(self):
            await self._ev.wait()
            return 0

    n = 4
    fakes = {1000 + i: FakeProc() for i in range(n)}
    bot.ACTIVE_PROCS.clear()
    bot.ACTIVE_PROCS.update(fakes)

    # _kill_tree would try real os.kill on our fake pids — neutralise it.
    orig_kill = bot._kill_tree
    bot._kill_tree = lambda pid: None
    try:
        per_proc_timeout = 0.4
        t0 = time.monotonic()
        await bot._reap_active_procs(timeout=per_proc_timeout)
        elapsed = time.monotonic() - t0
    finally:
        bot._kill_tree = orig_kill
        bot.ACTIVE_PROCS.clear()

    # With a per-process timeout the old code would take ~n*0.4 = 1.6s; the one
    # total deadline keeps it near a single 0.4s.
    assert elapsed < per_proc_timeout * 2, f"elapsed={elapsed} (looks per-process)"
    assert not bot.ACTIVE_PROCS  # cleared
