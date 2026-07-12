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


def assert_bookkeeping_empty(session_id=None):
    """Assert no stale execution-slot bookkeeping remains.

    With a session_id: checks only THAT session's task registration + lock ref +
    lock state (safe to call while OTHER sessions are still active).
    Without one: also asserts global state — running counter at 0 and the
    semaphore restored to full capacity (call only when nothing else runs).
    """
    if session_id is not None:
        assert session_id not in bot.CONC.session_tasks, "stale task registration"
        assert bot.CONC.session_lock_refs.get(session_id, 0) == 0, "stale lock ref"
        lk = bot.CONC.session_locks.get(session_id)
        assert lk is None or not lk.locked(), "session lock left locked"
        return
    assert not bot.CONC.session_tasks, "stale task registrations remain"
    assert not any(v for v in bot.CONC.session_lock_refs.values()), "stale lock refs"
    assert all(not lk.locked() for lk in bot.CONC.session_locks.values()), "a lock left locked"
    assert bot.CONC.running == 0, f"running counter not restored: {bot.CONC.running}"
    # semaphore restored to full capacity (no permits still held)
    assert bot.CONC.semaphore._value == bot.CONC.max_running, \
        f"semaphore capacity not restored: {bot.CONC.semaphore._value}/{bot.CONC.max_running}"


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
    # complete cleanup: no stale victim bookkeeping, and a NEW victim turn runs
    assert_bookkeeping_empty("victim:1")
    ran_again = asyncio.Event()

    async def victim2():
        async with bot.execution_slot("victim:1", FakeSession("victim:1")):
            ran_again.set()

    await asyncio.wait_for(asyncio.create_task(victim2()), timeout=2)
    assert ran_again.is_set()
    assert_bookkeeping_empty("victim:1")


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


# ══════════════════════════════════════════════════════════════════════════
#  Section 1 — cancellation DURING execution_slot.__aenter__ rolls back cleanly
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cancel_while_waiting_for_session_lock():
    """(1) A second same-session turn cancelled while blocked on the per-session
    lock must not leak a lock ref / task / status."""
    bot.CONC.init(max_running=2)
    in1 = asyncio.Event(); gate1 = asyncio.Event()
    sess = FakeSession("s:1")

    async def holder():
        async with bot.execution_slot("s:1", sess):
            in1.set(); await gate1.wait()

    t1 = asyncio.create_task(holder())
    await asyncio.wait_for(in1.wait(), timeout=2)
    waiter_sess = FakeSession("s:1")

    async def waiter():
        async with bot.execution_slot("s:1", waiter_sess):
            pass

    t2 = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)                 # t2 now blocked on the session lock
    assert waiter_sess.status == "queued"
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2
    # holder still holds its lock/permit; but the waiter left NO residue
    assert bot.CONC.session_lock_refs.get("s:1", 0) == 1   # only the holder's ref
    gate1.set()
    await asyncio.wait_for(t1, timeout=2)
    assert_bookkeeping_empty("s:1")


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_semaphore():
    """(2) Cancelled after acquiring the session lock but while waiting on the
    global semaphore — must release the lock and roll back."""
    bot.CONC.init(max_running=1)
    hin = asyncio.Event(); hgate = asyncio.Event()

    async def holder():
        async with bot.execution_slot("h:1", FakeSession("h:1")):
            hin.set(); await hgate.wait()

    th = asyncio.create_task(holder())
    await asyncio.wait_for(hin.wait(), timeout=2)   # single permit taken by holder
    victim_sess = FakeSession("v:1")

    async def victim():
        async with bot.execution_slot("v:1", victim_sess):
            pass

    tv = asyncio.create_task(victim())
    await asyncio.sleep(0.05)     # v:1 holds its own session lock, waits on semaphore
    assert bot.CONC.session_locks["v:1"].locked() is True
    tv.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tv
    assert_bookkeeping_empty("v:1")               # its lock was released, ref dropped
    hgate.set()
    await asyncio.wait_for(th, timeout=2)
    assert_bookkeeping_empty()


@pytest.mark.asyncio
async def test_cancel_immediately_after_admission():
    """(3) Cancellation right after admission still releases the permit+lock."""
    bot.CONC.init(max_running=1)
    admitted = asyncio.Event(); gate = asyncio.Event()

    async def run():
        async with bot.execution_slot("a:1", FakeSession("a:1")):
            admitted.set(); await gate.wait()

    t = asyncio.create_task(run())
    await asyncio.wait_for(admitted.wait(), timeout=2)
    assert bot.CONC.running == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert_bookkeeping_empty("a:1")


@pytest.mark.asyncio
async def test_cancel_several_queued_for_one_session():
    """(4) Several queued turns for one session all cancel with no residue."""
    bot.CONC.init(max_running=1)
    hin = asyncio.Event(); hgate = asyncio.Event()

    async def holder():
        async with bot.execution_slot("m:1", FakeSession("m:1")):
            hin.set(); await hgate.wait()

    th = asyncio.create_task(holder())
    await asyncio.wait_for(hin.wait(), timeout=2)
    waiters = [asyncio.create_task(_queued_waiter("m:1")) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert len(bot.CONC.session_tasks["m:1"]) == 4          # holder + 3 waiters
    for w in waiters:
        w.cancel()
    for w in waiters:
        with pytest.raises(asyncio.CancelledError):
            await w
    hgate.set()
    await asyncio.wait_for(th, timeout=2)
    assert_bookkeeping_empty("m:1")                         # (5) full registry cleanup
    # (6) a fresh turn for the same session runs afterward
    ran = asyncio.Event()

    async def again():
        async with bot.execution_slot("m:1", FakeSession("m:1")):
            ran.set()
    await asyncio.wait_for(asyncio.create_task(again()), timeout=2)
    assert ran.is_set()
    assert_bookkeeping_empty("m:1")


async def _queued_waiter(sid):
    async with bot.execution_slot(sid, FakeSession(sid)):
        await asyncio.sleep(0.2)


# ══════════════════════════════════════════════════════════════════════════
#  Section 3 — the real shutdown helper _cancel_and_await_run_tasks
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_shutdown_helper_queued_task():
    bot.CONC.init(max_running=1)
    hin = asyncio.Event(); hgate = asyncio.Event()

    async def holder():
        async with bot.execution_slot("h:1", FakeSession("h:1")):
            hin.set(); await hgate.wait()

    async def queued():
        async with bot.execution_slot("q:1", FakeSession("q:1")):
            await asyncio.sleep(5)

    th = asyncio.create_task(holder())
    await asyncio.wait_for(hin.wait(), timeout=2)
    tq = asyncio.create_task(queued())
    await asyncio.sleep(0.05)
    hgate.set()                                 # let the holder finish naturally
    summary = await bot._cancel_and_await_run_tasks(timeout=2)
    assert summary["cancelled"] >= 1 and summary["timed_out"] is False
    for t in (th, tq):
        try: await asyncio.wait_for(t, timeout=2)
        except asyncio.CancelledError: pass
    assert_bookkeeping_empty()


@pytest.mark.asyncio
async def test_shutdown_helper_running_task():
    bot.CONC.init(max_running=2)
    rin = asyncio.Event()

    async def running():
        async with bot.execution_slot("r:1", FakeSession("r:1")):
            rin.set(); await asyncio.sleep(5)

    tr = asyncio.create_task(running())
    await asyncio.wait_for(rin.wait(), timeout=2)
    summary = await bot._cancel_and_await_run_tasks(timeout=2)
    assert summary["cancelled"] == 1 and summary["awaited"] == 1
    try: await asyncio.wait_for(tr, timeout=2)
    except asyncio.CancelledError: pass
    assert_bookkeeping_empty()


@pytest.mark.asyncio
async def test_shutdown_helper_mixed():
    bot.CONC.init(max_running=1)
    rin = asyncio.Event()

    async def running():
        async with bot.execution_slot("r:1", FakeSession("r:1")):
            rin.set(); await asyncio.sleep(5)

    async def queued():
        async with bot.execution_slot("q:1", FakeSession("q:1")):
            await asyncio.sleep(5)

    tr = asyncio.create_task(running())
    await asyncio.wait_for(rin.wait(), timeout=2)
    tq = asyncio.create_task(queued())
    await asyncio.sleep(0.05)
    summary = await bot._cancel_and_await_run_tasks(timeout=2)
    assert summary["cancelled"] == 2
    for t in (tr, tq):
        try: await asyncio.wait_for(t, timeout=2)
        except asyncio.CancelledError: pass
    assert_bookkeeping_empty()


@pytest.mark.asyncio
async def test_shutdown_helper_timeout():
    """A misbehaving task that swallows cancellation → helper reports timed_out
    without hanging."""
    bot.CONC.init(max_running=1)
    started = asyncio.Event()

    async def stubborn():
        try:
            async with bot.execution_slot("s:1", FakeSession("s:1")):
                started.set(); await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)            # ignores cancel briefly
            raise

    t = asyncio.create_task(stubborn())
    await asyncio.wait_for(started.wait(), timeout=2)
    summary = await bot._cancel_and_await_run_tasks(timeout=0.1)
    assert summary["timed_out"] is True
    try: await asyncio.wait_for(t, timeout=2)
    except asyncio.CancelledError: pass


@pytest.mark.asyncio
async def test_shutdown_helper_isolates_unrelated_task():
    bot.CONC.init(max_running=2)
    unrelated_done = asyncio.Event()

    async def unrelated():                       # NOT registered in session_tasks
        await asyncio.sleep(0.2)
        unrelated_done.set()

    tu = asyncio.create_task(unrelated())
    rin = asyncio.Event()

    async def running():
        async with bot.execution_slot("r:1", FakeSession("r:1")):
            rin.set(); await asyncio.sleep(5)

    tr = asyncio.create_task(running())
    await asyncio.wait_for(rin.wait(), timeout=2)
    summary = await bot._cancel_and_await_run_tasks(timeout=2)
    assert summary["cancelled"] == 1
    await asyncio.wait_for(tu, timeout=2)        # unrelated task ran to completion
    assert unrelated_done.is_set()
    try: await asyncio.wait_for(tr, timeout=2)
    except asyncio.CancelledError: pass


@pytest.mark.asyncio
async def test_shutdown_helper_empty():
    bot.CONC.init(max_running=2)
    summary = await bot._cancel_and_await_run_tasks(timeout=1)
    assert summary == {"cancelled": 0, "awaited": 0, "timed_out": False}
