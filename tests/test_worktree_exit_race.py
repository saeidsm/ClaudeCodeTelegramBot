"""Architect follow-up item 1 — cancellation during a SLOW __aexit__, not only
__aenter__.

The Phase-1 barrier only covered cancellation while the enter thread was
running. A second cancellation delivered while __aexit__ awaits its shielded
exit_task lets __aexit__ unwind immediately (shield only protects the inner
task, not the awaiting coroutine) while _exit_sync keeps running in the
background — a same-path turn B could then start before that removal finishes.

The lifecycle barrier now spans claim -> real completion of EITHER the
enter-orphan cleanup OR the exit, resolved via an unconditional, idempotent
done-callback tied to the actual background work finishing, never to the
coroutine merely unwinding. These tests reproduce the real race (B started
immediately, without waiting for A's cleanup first) and the specific
requirements from the architect review. All mocked — no real git.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


class FakeWork:
    """Gates BOTH enter and exit independently, so tests can hold either phase
    open while asserting on the other side of a same-path race."""

    def __init__(self, path, fail_enter=False):
        self.path = str(path)
        self.enter_started = threading.Event()
        self.enter_gate = threading.Event()
        self.exit_started = threading.Event()
        self.exit_gate = threading.Event()
        self.enter_calls = 0
        self.exit_calls = 0
        self.fail_enter = fail_enter

    def enter_sync(self):
        self.enter_started.set()
        self.enter_gate.wait(timeout=5)
        self.enter_calls += 1
        if self.fail_enter:
            raise RuntimeError("enter failed")
        os.makedirs(self.path, exist_ok=True)
        return self.path

    def exit_sync(self, *a):
        self.exit_started.set()
        self.exit_gate.wait(timeout=5)
        self.exit_calls += 1
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def _wt(tmp_path, sid, fail_enter=False):
    wt = bot.WorktreeSession(str(tmp_path / "repo"), "main", sid)
    fake = FakeWork(tmp_path / "wt" / sid, fail_enter=fail_enter)
    wt.worktree_path = fake.path
    wt._enter_sync = fake.enter_sync
    wt._exit_sync = fake.exit_sync
    return wt, fake


async def _drive_until(pred, tries=300, delay=0.01):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


# ── steps 1-8: the real production-shaped race, repeated for flakiness ──────

@pytest.mark.asyncio
async def test_exit_cancel_race_B_blocked_until_A_exit_completes(tmp_path):
    for i in range(25):
        sid = f"exitrace{i}"
        canonical = bot._canonical_worktree_path(str(tmp_path / "wt" / sid))

        # (1) A enters normally, then begins a deliberately slow exit.
        A, fakeA = _wt(tmp_path, sid)
        fakeA.enter_gate.set()
        entered_a = asyncio.Event()

        async def useA(A=A, entered_a=entered_a):
            async with A as p:
                entered_a.set()
                assert os.path.isdir(p)
                await asyncio.sleep(5)

        tA = asyncio.create_task(useA())
        assert await _drive_until(entered_a.is_set)
        assert os.path.isdir(fakeA.path)

        # (2) Cancel A while its exit is still gated, then deliver a SECOND
        # cancellation while __aexit__ awaits its shielded exit task. This is
        # the production failure mode: tA can unwind and release its outer
        # session lock even though the removal thread remains in flight.
        tA.cancel()
        assert await _drive_until(fakeA.exit_started.is_set)
        tA.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tA
        assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is not None

        # (3) B starts IMMEDIATELY on the same path — WITHOUT releasing A's exit.
        B, fakeB = _wt(tmp_path, sid)
        fakeB.enter_gate.set()
        fakeB.exit_gate.set()  # B's own normal exit must not block on a real gate
        entered_b = asyncio.Event()

        async def useB(B=B, entered_b=entered_b):
            async with B as p:
                entered_b.set()
                assert os.path.isdir(p)

        tB = asyncio.create_task(useB())
        await asyncio.sleep(0.05)
        # (4) B's _enter_sync must NOT have begun.
        assert not fakeB.enter_started.is_set(), "B started before A's exit finished"
        assert not entered_b.is_set()
        assert fakeB.enter_calls == 0

        # (5) Release A's exit — it completes exactly once, even though the
        # outer task has already unwound from its second cancellation.
        fakeA.exit_gate.set()
        assert await _drive_until(lambda: fakeA.exit_calls == 1)
        assert not os.path.exists(fakeA.path)

        # (6) B then enters; A's (already-finished) exit never touches B's tree.
        await tB
        assert entered_b.is_set()
        assert fakeB.enter_calls == 1
        assert fakeB.exit_calls == 1
        assert not os.path.exists(fakeB.path)

        # (7) registries empty afterward.
        assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is None
        assert bot._WORKTREE_PATH_OWNER.get(canonical) is None
    # (8) the 25-iteration loop above is the repeat-for-flakiness run; the
    # focused suite is also repeated by the validation command.


# ── step 9: a different path proceeds while A's exit is blocked ─────────────

@pytest.mark.asyncio
async def test_different_path_enters_while_exit_blocked(tmp_path):
    A, fakeA = _wt(tmp_path, "exitblockA")
    fakeA.enter_gate.set()
    entered_a = asyncio.Event()

    async def useA():
        async with A:
            entered_a.set()
            await asyncio.sleep(5)

    tA = asyncio.create_task(useA())
    assert await _drive_until(entered_a.is_set)
    tA.cancel()
    # A single cancellation makes __aexit__'s shield-await genuinely block tA
    # until exit_sync finishes — so do NOT await tA yet; check exit_started
    # (and exercise the different path) WHILE A's exit is still gated/blocked.
    assert await _drive_until(fakeA.exit_started.is_set)
    assert not tA.done()

    # A DIFFERENT canonical path must proceed freely and concurrently.
    C, fakeC = _wt(tmp_path, "exitblockC")
    fakeC.enter_gate.set()
    fakeC.exit_gate.set()
    async with C as p:
        assert os.path.isdir(p)
    assert fakeC.exit_calls == 1
    assert not os.path.exists(fakeC.path)

    # Now release A's exit and confirm the original cancellation still surfaces.
    fakeA.exit_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await tA
    assert await _drive_until(lambda: fakeA.exit_calls >= 1)


# ── explicit "second cancellation" scenario, isolated and precise ───────────

@pytest.mark.asyncio
async def test_second_cancellation_delivered_mid_exit_is_idempotent(tmp_path):
    sid = "double_cancel"
    A, fakeA = _wt(tmp_path, sid)
    fakeA.enter_gate.set()
    entered = asyncio.Event()

    async def useA():
        async with A:
            entered.set()
            await asyncio.sleep(5)

    tA = asyncio.create_task(useA())
    assert await _drive_until(entered.is_set)

    tA.cancel()  # 1st cancellation -> triggers __aexit__
    # Let the loop run __aexit__ forward to its `await shield(exit_task)` point
    # (exit thread now running, gated) BEFORE fully awaiting tA.
    assert await _drive_until(fakeA.exit_started.is_set)
    tA.cancel()  # 2nd cancellation, delivered while __aexit__ awaits the shield.

    with pytest.raises(asyncio.CancelledError):
        await tA
    # exit_task keeps running under the shield regardless of how many
    # cancellations our await received; releasing it completes exactly once.
    fakeA.exit_gate.set()
    assert await _drive_until(lambda: fakeA.exit_calls >= 1)
    await asyncio.sleep(0.05)
    assert fakeA.exit_calls == 1, "double cancellation caused _exit_sync to run more than once"
    assert not os.path.exists(fakeA.path)
    canonical = bot._canonical_worktree_path(fakeA.path)
    assert await _drive_until(lambda: bot._WORKTREE_PATH_BARRIERS.get(canonical) is None)
    assert bot._WORKTREE_PATH_OWNER.get(canonical) is None


# ── normal (uncancelled) lifecycles must never leak registry state ──────────

@pytest.mark.asyncio
async def test_normal_exit_leaves_no_barrier_or_owner(tmp_path):
    A, fakeA = _wt(tmp_path, "normal_exit")
    fakeA.enter_gate.set()
    fakeA.exit_gate.set()
    canonical = bot._canonical_worktree_path(fakeA.path)
    async with A as p:
        assert os.path.isdir(p)
        assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is not None  # in-flight
        assert bot._WORKTREE_PATH_OWNER.get(canonical) is A._owner_token
    assert fakeA.exit_calls == 1
    assert not os.path.exists(fakeA.path)
    assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is None
    assert bot._WORKTREE_PATH_OWNER.get(canonical) is None


@pytest.mark.asyncio
async def test_enter_normal_exception_resolves_barrier_immediately(tmp_path):
    """A non-cancellation enter failure (e.g. `git worktree add` failing for a
    real reason) must not leave the lifecycle barrier stuck forever — Python
    never calls __aexit__ when __aenter__ raises, so __aenter__ itself must
    release it."""
    sid = "enter_fails_normally"
    A, fakeA = _wt(tmp_path, sid, fail_enter=True)
    fakeA.enter_gate.set()  # enter proceeds immediately and then raises
    canonical = bot._canonical_worktree_path(fakeA.path)

    with pytest.raises(RuntimeError, match="enter failed"):
        async with A:
            pass  # pragma: no cover - never reached

    assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is None
    assert bot._WORKTREE_PATH_OWNER.get(canonical) is None

    # A subsequent attempt on the same path must proceed normally (no stuck
    # barrier from the failed attempt).
    B, fakeB = _wt(tmp_path, sid)
    fakeB.enter_gate.set()
    fakeB.exit_gate.set()
    async with B as p:
        assert os.path.isdir(p)
    assert fakeB.exit_calls == 1
