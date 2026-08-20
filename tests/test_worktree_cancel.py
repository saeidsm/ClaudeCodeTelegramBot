"""Section 4 — cancellation during WorktreeSession create/remove.

asyncio.to_thread cannot stop the underlying thread, so a cancelled __aenter__
may still finish creating a worktree. These mocked tests prove the bounded design
cleans up the created tree exactly once (or leaves nothing to clean), and that a
later run on the same path succeeds. No real git is used.
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


def _wt(tmp_path, sid="s_1", fail=False):
    wt = bot.WorktreeSession(str(tmp_path / "repo"), "main", sid)
    fake = FakeWork(tmp_path / "wt" / sid, fail=fail)
    wt.worktree_path = fake.path
    wt._enter_sync = fake.enter_sync
    wt._exit_sync = fake.exit_sync
    return wt, fake


async def _drive_until(pred, tries=100, delay=0.02):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


@pytest.mark.asyncio
async def test_cancel_while_enter_running_then_cleaned_once(tmp_path):
    wt, fake = _wt(tmp_path)

    async def use():
        async with wt:
            await asyncio.sleep(5)

    t = asyncio.create_task(use())
    assert await _drive_until(fake.enter_started.is_set)      # enter thread is running
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    # release the thread — it now finishes and CREATES the worktree post-cancel
    fake.enter_gate.set()
    assert await _drive_until(lambda: fake.exit_calls >= 1 and not os.path.exists(fake.path))
    assert fake.exit_calls == 1                               # cleaned EXACTLY once
    assert not os.path.exists(fake.path)                      # orphan removed


@pytest.mark.asyncio
async def test_enter_fails_after_cancel_no_crash(tmp_path):
    wt, fake = _wt(tmp_path, fail=True)

    async def use():
        async with wt:
            await asyncio.sleep(5)

    t = asyncio.create_task(use())
    assert await _drive_until(fake.enter_started.is_set)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    fake.enter_gate.set()                                     # enter now RAISES
    await asyncio.sleep(0.2)
    assert fake.exit_calls == 0                               # nothing created → nothing to clean
    assert not os.path.exists(fake.path)


@pytest.mark.asyncio
async def test_next_run_same_path_succeeds(tmp_path):
    # First run is cancelled and cleaned...
    wt1, fake1 = _wt(tmp_path, sid="reuse")

    async def use1():
        async with wt1:
            await asyncio.sleep(5)

    t = asyncio.create_task(use1())
    assert await _drive_until(fake1.enter_started.is_set)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    fake1.enter_gate.set()
    assert await _drive_until(lambda: fake1.exit_calls >= 1)

    # ...a fresh run on the SAME worktree path completes normally.
    wt2, fake2 = _wt(tmp_path, sid="reuse")
    fake2.enter_gate.set()                                    # let enter proceed immediately
    ran = False
    async with wt2 as p:
        ran = os.path.isdir(p)
    assert ran
    assert fake2.exit_calls == 1                              # normal exit removed it
    assert not os.path.exists(fake2.path)


@pytest.mark.asyncio
async def test_normal_lifecycle_no_orphan_scheduling(tmp_path):
    wt, fake = _wt(tmp_path, sid="normal")
    fake.enter_gate.set()
    async with wt as p:
        assert os.path.isdir(p)
    assert fake.exit_calls == 1
    assert wt._orphan_cleanup_scheduled is False             # never triggered on success
