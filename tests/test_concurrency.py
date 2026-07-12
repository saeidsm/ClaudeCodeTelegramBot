"""Tests for Phase 1 bounded execution concurrency (Section F).

Uses fakes/events only — never invokes real Claude or external APIs.
Covers: global cap of 2, same-session serialization, cross-session overlap,
permit release on success/exception/cancellation, kill-cancels-queued, kill of
one session leaves another, and shutdown cancels queued tasks.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


class FakeSession:
    def __init__(self, sid, status="idle"):
        self.id = sid
        self.status = status


class Counter:
    def __init__(self):
        self.cur = 0
        self.max = 0
    def enter(self):
        self.cur += 1
        self.max = max(self.max, self.cur)
    def exit(self):
        self.cur -= 1


@pytest.mark.asyncio
async def test_global_cap_two():
    bot.CONC.init(max_running=2)
    gate = asyncio.Event()
    counter = Counter()
    admitted = 0
    admitted_ev = asyncio.Event()

    async def run(i):
        nonlocal admitted
        async with bot.execution_slot(f"sess{i}", FakeSession(f"sess{i}")):
            counter.enter()
            admitted += 1
            if admitted >= 2:
                admitted_ev.set()
            try:
                await gate.wait()
            finally:
                counter.exit()

    tasks = [asyncio.create_task(run(i)) for i in range(4)]
    await asyncio.wait_for(admitted_ev.wait(), timeout=2)
    await asyncio.sleep(0.05)          # give any extra tasks a chance to (wrongly) enter
    assert counter.max == 2            # never more than MAX_RUNNING_AGENTS
    assert counter.cur == 2
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert bot.CONC.running == 0


@pytest.mark.asyncio
async def test_same_session_serializes():
    bot.CONC.init(max_running=2)   # 2 permits free, but same session must serialize
    order = []
    gate1 = asyncio.Event()
    in1 = asyncio.Event()
    in2 = asyncio.Event()

    async def first():
        async with bot.execution_slot("same:1", FakeSession("same:1")):
            order.append("1-start"); in1.set()
            await gate1.wait()
            order.append("1-end")

    async def second():
        async with bot.execution_slot("same:1", FakeSession("same:1")):
            order.append("2-start"); in2.set()

    t1 = asyncio.create_task(first())
    await asyncio.wait_for(in1.wait(), timeout=2)
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    assert not in2.is_set()             # second blocked on the per-session lock
    gate1.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2)
    assert order == ["1-start", "1-end", "2-start"]


@pytest.mark.asyncio
async def test_different_sessions_overlap():
    bot.CONC.init(max_running=2)
    in_a = asyncio.Event(); in_b = asyncio.Event(); gate = asyncio.Event()

    async def run(sid, ev):
        async with bot.execution_slot(sid, FakeSession(sid)):
            ev.set()
            await gate.wait()

    ta = asyncio.create_task(run("a:1", in_a))
    tb = asyncio.create_task(run("b:1", in_b))
    await asyncio.wait_for(asyncio.gather(in_a.wait(), in_b.wait()), timeout=2)  # both in at once
    gate.set()
    await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2)


@pytest.mark.asyncio
async def test_permit_released_on_success():
    bot.CONC.init(max_running=1)
    async with bot.execution_slot("x:1", FakeSession("x:1")):
        assert bot.CONC.running == 1
    assert bot.CONC.running == 0
    # a follow-up run must be able to acquire immediately
    async with bot.execution_slot("y:1", FakeSession("y:1")):
        assert bot.CONC.running == 1
    assert bot.CONC.running == 0


@pytest.mark.asyncio
async def test_permit_released_on_exception():
    bot.CONC.init(max_running=1)
    with pytest.raises(ValueError):
        async with bot.execution_slot("x:1", FakeSession("x:1")):
            raise ValueError("boom")
    assert bot.CONC.running == 0
    # slot is free again
    async with bot.execution_slot("z:1", FakeSession("z:1")):
        assert bot.CONC.running == 1


@pytest.mark.asyncio
async def test_permit_released_on_cancellation():
    bot.CONC.init(max_running=1)
    started = asyncio.Event()
    gate = asyncio.Event()

    async def run():
        async with bot.execution_slot("c:1", FakeSession("c:1")):
            started.set()
            await gate.wait()

    t = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), timeout=2)
    assert bot.CONC.running == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert bot.CONC.running == 0


@pytest.mark.asyncio
async def test_kill_queued_prevents_later_run():
    bot.CONC.init(max_running=1)
    holder_in = asyncio.Event(); holder_gate = asyncio.Event()
    victim_entered = asyncio.Event()

    async def holder():
        async with bot.execution_slot("holder:1", FakeSession("holder:1")):
            holder_in.set()
            await holder_gate.wait()

    async def victim():
        async with bot.execution_slot("victim:1", FakeSession("victim:1")):
            victim_entered.set()        # must NEVER happen after cancel
            await asyncio.sleep(0.1)

    th = asyncio.create_task(holder())
    await asyncio.wait_for(holder_in.wait(), timeout=2)   # single permit taken
    tv = asyncio.create_task(victim())
    await asyncio.sleep(0.05)                              # victim now queued on semaphore
    n = bot.CONC.cancel_session_tasks("victim:1")         # == SM.kill path
    assert n == 1
    with pytest.raises(asyncio.CancelledError):
        await tv
    holder_gate.set()
    await asyncio.wait_for(th, timeout=2)
    await asyncio.sleep(0.05)
    assert not victim_entered.is_set()                    # queued task never began


@pytest.mark.asyncio
async def test_kill_one_does_not_cancel_another():
    bot.CONC.init(max_running=1)
    a_in = asyncio.Event(); a_gate = asyncio.Event()
    b_ran = asyncio.Event()

    async def a():
        async with bot.execution_slot("a:1", FakeSession("a:1")):
            a_in.set(); await a_gate.wait()

    async def b():
        async with bot.execution_slot("b:1", FakeSession("b:1")):
            b_ran.set()

    ta = asyncio.create_task(a())
    await asyncio.wait_for(a_in.wait(), timeout=2)
    tb = asyncio.create_task(b())
    await asyncio.sleep(0.05)                 # b queued
    bot.CONC.cancel_session_tasks("victim-not-b")  # cancel unrelated → no effect on b
    a_gate.set()                              # free the permit
    await asyncio.wait_for(tb, timeout=2)     # b runs to completion
    assert b_ran.is_set()
    await asyncio.wait_for(ta, timeout=2)


@pytest.mark.asyncio
async def test_sm_kill_cancels_registered_task():
    bot.CONC.init(max_running=1)
    sess = bot.SM.create(999999, "killme")     # real Session in SM
    started = asyncio.Event(); gate = asyncio.Event()

    async def run():
        async with bot.execution_slot(sess.id, sess):
            started.set(); await gate.wait()

    t = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), timeout=2)
    assert bot.SM.kill(999999, "killme") is True
    with pytest.raises(asyncio.CancelledError):
        await t
    assert bot.CONC.running == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_queued():
    bot.CONC.init(max_running=1)
    holder_in = asyncio.Event(); holder_gate = asyncio.Event()

    async def holder():
        async with bot.execution_slot("h:1", FakeSession("h:1")):
            holder_in.set(); await holder_gate.wait()

    async def queued():
        async with bot.execution_slot("q:1", FakeSession("q:1")):
            await asyncio.sleep(0.1)

    th = asyncio.create_task(holder())
    await asyncio.wait_for(holder_in.wait(), timeout=2)
    tq = asyncio.create_task(queued())
    await asyncio.sleep(0.05)
    # emulate graceful_shutdown's cancellation sweep
    cancelled = 0
    for sid in list(bot.CONC.session_tasks.keys()):
        cancelled += bot.CONC.cancel_session_tasks(sid)
    assert cancelled >= 1
    holder_gate.set()
    for t in (th, tq):
        try:
            await asyncio.wait_for(t, timeout=2)
        except asyncio.CancelledError:
            pass
