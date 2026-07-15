"""Tests for _kill_tree() — recursive process-tree kill on timeout//kill.

Regression context (2026-06-05): claude's Bash tool calls run as their own
session leaders (`Ss`), so killing only the claude pid (or even its process
group) leaves `until pgrep ...; do sleep; done` loops alive forever. They
reparent to PID 1 and poison every future pgrep-based wait loop.
_kill_tree must walk /proc parentage and SIGKILL every descendant,
including ones that called setsid().
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        # signal 0 = existence check; reap zombies first so they count as dead
        import os

        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False


def test_kill_tree_kills_session_leader_descendants(tmp_path):
    """Spawn  parent bash → setsid'd loop bash (own session, like a claude
    Bash tool call) → sleep child.  _kill_tree(parent) must kill ALL of them."""
    pidfile = tmp_path / "loop.pid"
    parent = subprocess.Popen(
        ["bash", "-c",
         f"setsid bash -c 'echo $$ > {pidfile}; while true; do sleep 300; done' & wait"],
    )
    loop_pid = None
    try:
        # wait for the setsid'd loop to write its pid
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        loop_pid = int(pidfile.read_text().strip())

        assert _alive(parent.pid)
        assert _alive(loop_pid)

        bot._kill_tree(parent.pid)
        parent.wait(timeout=5)
        # the setsid'd loop is NOT in parent's session/pgroup — only a /proc
        # descendant walk reaches it
        deadline = time.time() + 5
        while time.time() < deadline and _alive(loop_pid):
            time.sleep(0.1)

        assert not _alive(loop_pid), "session-leader descendant survived _kill_tree"
        assert not _alive(parent.pid)
    finally:
        # If any assertion//setup failed, do NOT leak an immortal loop on the
        # host (the very first red run of this test did exactly that).
        import os
        import signal as _sig

        for pid in (parent.pid, loop_pid):
            if pid:
                try:
                    os.kill(pid, _sig.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        parent.wait(timeout=5)


def test_kill_tree_gone_pid_is_noop():
    """Killing an already-dead pid must not raise."""
    p = subprocess.Popen(["true"])
    p.wait()
    bot._kill_tree(p.pid)  # must not raise


def _survivors_with(mark: str) -> list[int]:
    """Live (non-zombie) pids whose cmdline contains `mark`. Used to detect
    orphaned synthetic children without touching any unrelated process."""
    import os
    out = []
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            cl = Path(f"/proc/{e}/cmdline").read_bytes()
        except OSError:
            continue
        if mark.encode() in cl:
            try:
                st = Path(f"/proc/{e}/stat").read_text()
                if st[st.rindex(")") + 2:].split()[0] != "Z":
                    out.append(int(e))
            except (OSError, ValueError):
                pass
    return out


def test_kill_tree_no_leak_when_descendant_forks_during_collection(tmp_path):
    """Diagnosed failure class: a session-leader descendant that keeps forking
    children *during* the /proc collection window. A plain snapshot+kill lets any
    child spawned after the snapshot reparent to init and survive; the freeze
    -then-kill walk must leave ZERO of them alive.

    Marker = a unique `sleep <N>` duration, so we only ever see/kill our own
    synthetic children."""
    import os
    import signal as _sig

    mark = str(310000 + (os.getpid() % 100000))
    pidfile = tmp_path / "loop.pid"
    parent = subprocess.Popen(
        ["bash", "-c",
         f"setsid bash -c 'echo $$ > {pidfile}; while true; do sleep {mark} & done' & wait"])
    loop_pid = None
    try:
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        loop_pid = int(pidfile.read_text().strip())
        time.sleep(0.4)                       # let a pile of forking children build up
        assert _survivors_with(mark), "setup: expected live forked children before kill"

        bot._kill_tree(parent.pid)
        # bounded wait for teardown/reparent to settle
        deadline = time.time() + 5
        while time.time() < deadline and _survivors_with(mark):
            time.sleep(0.1)

        leaked = _survivors_with(mark)
        assert not leaked, f"{len(leaked)} descendant(s) forked during collection survived _kill_tree"
    finally:
        for pid in ([parent.pid, loop_pid] if loop_pid else [parent.pid]):
            try:
                os.kill(pid, _sig.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in _survivors_with(mark):     # sweep any marked survivor by identity
            try:
                os.kill(pid, _sig.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            parent.wait(timeout=5)
        except Exception:
            pass


def test_kill_tree_kills_descendant_first_root_last(tmp_path, monkeypatch):
    """Ordering guarantee: leaves die before their parents and the root dies
    last, so an intermediate death never orphans a not-yet-killed node."""
    import os

    pidfile = tmp_path / "loop.pid"
    parent = subprocess.Popen(
        ["bash", "-c",
         f"setsid bash -c 'echo $$ > {pidfile}; while true; do sleep 300; done' & wait"])
    loop_pid = None
    killed_order: list[int] = []
    real_kill = os.kill

    def recording_kill(pid, sig):
        import signal as _sig
        if sig == _sig.SIGKILL:
            killed_order.append(pid)
        return real_kill(pid, sig)

    try:
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        loop_pid = int(pidfile.read_text().strip())
        time.sleep(0.2)                       # ensure the sleep grandchild exists

        monkeypatch.setattr(bot.os, "kill", recording_kill)
        bot._kill_tree(parent.pid)
        monkeypatch.undo()

        # root must be the last SIGKILL; loop (its descendant) must precede it
        assert killed_order, "no SIGKILLs recorded"
        assert killed_order[-1] == parent.pid, f"root not killed last: {killed_order}"
        assert killed_order.index(loop_pid) < killed_order.index(parent.pid), \
            f"session-leader descendant not killed before root: {killed_order}"
    finally:
        import signal as _sig
        for pid in ([parent.pid, loop_pid] if loop_pid else [parent.pid]):
            try:
                real_kill(pid, _sig.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            parent.wait(timeout=5)
        except Exception:
            pass


def test_kill_tree_skips_pid_reused_since_freeze(monkeypatch):
    """PID-reuse guard: if a collected pid's start time no longer matches what we
    recorded at freeze time, it has been reused by an unrelated process and must
    NOT be signalled. Innocent bystander (`sleep 30`, ours) must survive."""
    import os
    import signal as _sig

    innocent = subprocess.Popen(["sleep", "30"])
    try:
        # _collect_tree yields only the innocent pid; start time "changes" between
        # the freeze read and the pre-kill re-check -> guard must skip the SIGKILL.
        monkeypatch.setattr(bot, "_collect_tree", lambda pid: [innocent.pid])
        calls = {"n": 0}

        def fake_start(pid):
            calls["n"] += 1
            return "1000" if calls["n"] == 1 else "2000"   # mismatch -> reused

        monkeypatch.setattr(bot, "_proc_starttime", fake_start)

        real_kill = os.kill
        sigkilled = []

        def watch_kill(p, s):
            if s == _sig.SIGKILL:
                sigkilled.append(p)
            return real_kill(p, s)

        monkeypatch.setattr(bot.os, "kill", watch_kill)
        bot._kill_tree(innocent.pid)
        monkeypatch.undo()

        assert innocent.pid not in sigkilled, "reused pid was SIGKILLed despite start-time mismatch"
        assert innocent.poll() is None, "innocent bystander was killed"
    finally:
        try:
            innocent.kill()
        except Exception:
            pass
        innocent.wait(timeout=5)


def test_kill_tree_tolerates_pid_vanishing_before_kill(monkeypatch):
    """procfs race tolerance: a pid present at collection but gone by kill time
    must not raise from the cleanup path."""
    dead = subprocess.Popen(["true"])
    dead.wait()                               # already gone, pid still in our injected list
    monkeypatch.setattr(bot, "_collect_tree", lambda pid: [dead.pid])
    bot._kill_tree(dead.pid)                  # must not raise


def test_proc_starttime_identifies_incarnation_and_gone():
    """Helper behaves: a live pid yields a numeric start time; a gone pid None."""
    p = subprocess.Popen(["sleep", "5"])
    try:
        st = bot._proc_starttime(p.pid)
        assert st is not None and st.isdigit(), f"expected numeric start time, got {st!r}"
    finally:
        p.kill()
        p.wait(timeout=5)
    assert bot._proc_starttime(p.pid) is None  # gone -> None


def test_spawn_timeout_default_is_120_minutes():
    assert bot.SPAWN_TIMEOUT_SECONDS == 7200
