"""Section 1 — post-cancellation worktree reuse race (per-path cleanup barrier).

The real race (not merely "cancel then wait for cleanup, then reuse"):

  1. turn A is cancelled while its enter thread is STILL running;
  2. B for the same path starts IMMEDIATELY (before A's late enter+cleanup);
  3. A's enter finishes late and creates the tree;
  4. B must not enter until A's cleanup completes;
  5. A's late cleanup must never remove B's worktree;
  6. the barrier registry is empty afterwards;
  7. a bounded timeout fails B safely instead of hanging;
  8. different paths proceed concurrently.

All mocked — no real git. Reuses the FakeWork gating harness style.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


class FakeWork:
    def __init__(self, path, fail=False):
        self.path = str(path)
        self.enter_started = threading.Event()
        self.enter_gate = threading.Event()
        self.exit_calls = 0
        self.fail = fail

    def enter_sync(self):
        self.enter_started.set()
        self.enter_gate.wait(timeout=5)
        if self.fail:
            raise RuntimeError("enter failed")
        os.makedirs(self.path, exist_ok=True)
        return self.path

    def exit_sync(self, *a):
        self.exit_calls += 1
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def _wt(tmp_path, sid):
    """A WorktreeSession whose enter/exit are FakeWork-gated. A and B built with
    the SAME sid share the SAME worktree_path (that is the race)."""
    wt = bot.WorktreeSession(str(tmp_path / "repo"), "main", sid)
    fake = FakeWork(tmp_path / "wt" / sid)
    wt.worktree_path = fake.path
    wt._enter_sync = fake.enter_sync
    wt._exit_sync = fake.exit_sync
    return wt, fake


async def _drive_until(pred, tries=200, delay=0.01):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


@pytest.mark.asyncio
async def test_immediate_same_path_race_B_waits_and_A_never_removes_B(tmp_path):
    # Repeat to expose scheduling flakiness.
    for i in range(25):
        sid = f"race{i}"
        canonical = bot._canonical_worktree_path(str(tmp_path / "wt" / sid))

        A, fakeA = _wt(tmp_path, sid)

        async def useA():
            async with A:
                await asyncio.sleep(5)

        tA = asyncio.create_task(useA())
        assert await _drive_until(fakeA.enter_started.is_set)   # A enter thread running
        tA.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tA
        # A's enter thread is STILL gated (not released); its cleanup barrier is
        # now registered for this path.
        assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is not None

        # B starts IMMEDIATELY on the same path. Its enter would proceed at once
        # if it weren't blocked on A's barrier.
        B, fakeB = _wt(tmp_path, sid)
        fakeB.enter_gate.set()
        enteredB = asyncio.Event()

        async def useB():
            async with B as p:
                enteredB.set()
                assert os.path.isdir(p)
                await asyncio.sleep(0.02)

        tB = asyncio.create_task(useB())
        await asyncio.sleep(0.05)
        # (4) B cannot enter until A's cleanup completes.
        assert not enteredB.is_set(), "B entered before A cleanup — race not closed"
        assert fakeB.exit_calls == 0

        # Release A's late enter → it creates the tree, then cleanup removes it
        # (A still owns the generation because B is still blocked) and resolves
        # the barrier.
        fakeA.enter_gate.set()
        assert await _drive_until(lambda: fakeA.exit_calls >= 1)
        await tB
        assert enteredB.is_set()

        # (5) A's cleanup removed ONLY A's tree (exactly once); B's own normal
        # exit removed B's tree. A never touched B's worktree.
        assert fakeA.exit_calls == 1
        assert fakeB.exit_calls == 1
        assert not os.path.exists(fakeB.path)
        # (6) registry empty for this path afterwards.
        assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is None


@pytest.mark.asyncio
async def test_barrier_timeout_fails_B_safely(tmp_path, monkeypatch):
    sid = "tmo"
    canonical = bot._canonical_worktree_path(str(tmp_path / "wt" / sid))
    monkeypatch.setattr(bot, "WORKTREE_BARRIER_TIMEOUT", 0.2)

    A, fakeA = _wt(tmp_path, sid)

    async def useA():
        async with A:
            await asyncio.sleep(5)

    tA = asyncio.create_task(useA())
    assert await _drive_until(fakeA.enter_started.is_set)
    tA.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tA
    assert bot._WORKTREE_PATH_BARRIERS.get(canonical) is not None

    # A's enter thread stays gated → its barrier never resolves in time. B must
    # FAIL SAFELY (RuntimeError), not hang and not create/use the path.
    B, fakeB = _wt(tmp_path, sid)
    fakeB.enter_gate.set()
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="barrier_timeout"):
        async with B:
            pass
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0                       # bounded, ~0.2s
    assert fakeB.exit_calls == 0               # B never created a tree

    # A's own lifecycle is not corrupted — release it and it cleans normally.
    fakeA.enter_gate.set()
    assert await _drive_until(lambda: fakeA.exit_calls >= 1)
    assert await _drive_until(lambda: bot._WORKTREE_PATH_BARRIERS.get(canonical) is None)


@pytest.mark.asyncio
async def test_different_paths_proceed_concurrently(tmp_path):
    A, fakeA = _wt(tmp_path, "pathA")
    B, fakeB = _wt(tmp_path, "pathB")   # different sid → different path
    fakeA.enter_gate.set()
    fakeB.enter_gate.set()
    enteredA = enteredB = False
    async with A as pa:
        enteredA = os.path.isdir(pa)
        async with B as pb:
            enteredB = os.path.isdir(pb)
    assert enteredA and enteredB
    assert fakeA.exit_calls == 1 and fakeB.exit_calls == 1


@pytest.mark.asyncio
async def test_enter_failure_after_cancel_resolves_barrier(tmp_path):
    """A cancelled enter that then FAILS must still resolve/remove its barrier so
    a later same-path run is not blocked forever."""
    sid = "failbar"
    canonical = bot._canonical_worktree_path(str(tmp_path / "wt" / sid))
    A, fakeA = _wt(tmp_path, sid)
    fakeA.fail = True

    async def useA():
        async with A:
            await asyncio.sleep(5)

    tA = asyncio.create_task(useA())
    assert await _drive_until(fakeA.enter_started.is_set)
    tA.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tA
    fakeA.enter_gate.set()                      # enter now RAISES
    assert await _drive_until(lambda: bot._WORKTREE_PATH_BARRIERS.get(canonical) is None)
    assert fakeA.exit_calls == 0                # nothing created → nothing cleaned
