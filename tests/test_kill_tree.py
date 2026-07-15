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
            if p == innocent.pid:
                return                 # never actually signal the real bystander
            return real_kill(p, s)

        monkeypatch.setattr(bot.os, "kill", watch_kill)
        bot._kill_tree(innocent.pid)
        monkeypatch.undo()

        assert innocent.pid not in sigkilled, "reused pid was SIGKILLed despite start-time mismatch"
        assert innocent.poll() is None, "innocent bystander was killed"
        assert _proc_state(innocent.pid) != "T", "innocent bystander left stopped"
    finally:
        try:
            innocent.kill()
        except Exception:
            pass
        innocent.wait(timeout=5)


def _proc_state(pid: int):
    """Single-char process state from /proc/<pid>/stat ('T' = stopped), or None."""
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
        return st[st.rindex(")") + 2:].split()[0]
    except (OSError, ValueError):
        return None


def test_kill_tree_resumes_when_sigkill_fails(monkeypatch):
    """A process is really SIGSTOPped (frozen) but its SIGKILL fails
    (PermissionError/OSError). It must NOT be dropped from the resume set: the
    finally clause must SIGCONT it so it is left running, never stuck in T."""
    import os
    import signal as _sig

    victim = subprocess.Popen(["sleep", "30"])
    real_kill = os.kill
    try:
        # collection yields only the victim; SIGSTOP/SIGCONT are applied for real,
        # but SIGKILL is refused -> victim stays our stopped incarnation, then must
        # be resumed by finally.
        monkeypatch.setattr(bot, "_collect_tree", lambda pid: [victim.pid])

        def kill_mock(p, s):
            if p == victim.pid and s == _sig.SIGKILL:
                raise PermissionError("simulated: cannot SIGKILL")
            return real_kill(p, s)

        monkeypatch.setattr(bot.os, "kill", kill_mock)
        bot._kill_tree(victim.pid)          # must not raise
        monkeypatch.undo()

        assert victim.poll() is None, "victim was killed despite SIGKILL failure"
        # resumed by finally, not parked in STOPPED state
        assert _wait_state_not(victim.pid, "T") != "T", \
            "frozen victim left STOPPED after SIGKILL failure"
    finally:
        try:
            real_kill(victim.pid, _sig.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        victim.wait(timeout=5)


def _wait_state_not(pid: int, unwanted: str, timeout: float = 3.0):
    """Bounded wait for the process to leave `unwanted` state (e.g. resume from 'T')."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _proc_state(pid)
        if s is None or s != unwanted:
            return s
        time.sleep(0.02)
    return _proc_state(pid)


def test_kill_tree_initial_starttime_none_then_readable(monkeypatch):
    """A transient unreadable identity on the first read (None) followed by a
    readable one on a later round must still end with the process KILLED — never
    leaked and never left stopped."""
    import os
    victim = subprocess.Popen(["sleep", "30"])
    try:
        real_start = bot._proc_starttime
        calls = {"n": 0}

        def flaky_start(p):
            if p == victim.pid:
                calls["n"] += 1
                if calls["n"] == 1:
                    return None                 # first read: transient failure
            return real_start(p)

        monkeypatch.setattr(bot, "_collect_tree", lambda pid: [victim.pid])
        monkeypatch.setattr(bot, "_proc_starttime", flaky_start)
        bot._kill_tree(victim.pid)
        monkeypatch.undo()

        assert calls["n"] >= 2, "second round should have re-read the identity"
        # killed within a bounded wait, not left stopped
        deadline = time.time() + 5
        while time.time() < deadline and victim.poll() is None:
            time.sleep(0.05)
        assert victim.poll() is not None, "victim survived despite readable identity on retry"
    finally:
        try:
            os.kill(victim.pid, __import__("signal").SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        victim.wait(timeout=5)


def test_kill_tree_starttime_unavailable_after_sigstop_not_left_stopped(monkeypatch):
    """If identity is never readable after a successful SIGSTOP, the process must
    NOT be treated as frozen and must NOT be left permanently stopped (it is
    resumed on every bounded round and on return)."""
    import os
    victim = subprocess.Popen(["sleep", "30"])
    try:
        monkeypatch.setattr(bot, "_collect_tree", lambda pid: [victim.pid])
        monkeypatch.setattr(bot, "_proc_starttime",
                            lambda p: None if p == victim.pid else bot._proc_starttime(p))
        # sanity: monkeypatched to always-None for the victim
        bot._kill_tree(victim.pid)
        monkeypatch.undo()

        assert victim.poll() is None, "victim unexpectedly died"
        assert _wait_state_not(victim.pid, "T") != "T", "victim left permanently STOPPED"
    finally:
        try:
            os.kill(victim.pid, __import__("signal").SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        victim.wait(timeout=5)


def test_kill_tree_sigstop_permission_or_oserror_not_frozen(monkeypatch):
    """A failed SIGSTOP must NOT be counted as a successful freeze: the process is
    neither recorded, nor SIGKILLed, nor left stopped."""
    import os
    import signal as _sig
    victim = subprocess.Popen(["sleep", "30"])
    real_kill = os.kill
    sigkilled = []
    try:
        def fail_sigstop(p, s):
            if p == victim.pid and s == _sig.SIGSTOP:
                raise PermissionError("simulated: cannot SIGSTOP")
            if s == _sig.SIGKILL:
                sigkilled.append(p)
            return real_kill(p, s)

        monkeypatch.setattr(bot, "_collect_tree", lambda pid: [victim.pid])
        monkeypatch.setattr(bot.os, "kill", fail_sigstop)
        bot._kill_tree(victim.pid)              # must not raise, must not spin unbounded
        monkeypatch.undo()

        assert victim.pid not in sigkilled, "un-frozen (unstoppable) pid was SIGKILLed"
        assert victim.poll() is None, "victim unexpectedly died"
        assert _proc_state(victim.pid) != "T", "victim left stopped after failed SIGSTOP"
    finally:
        try:
            real_kill(victim.pid, _sig.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        victim.wait(timeout=5)


def test_kill_tree_leaves_no_frozen_process_stopped(tmp_path):
    """Every successfully frozen live process in a real tree ends up killed — and
    none is left in the STOPPED (T) state on the host."""
    import os
    import signal as _sig
    pidfile = tmp_path / "loop.pid"
    parent = subprocess.Popen(
        ["bash", "-c",
         f"setsid bash -c 'echo $$ > {pidfile}; while true; do sleep 300; done' & wait"])
    loop_pid = None
    try:
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        loop_pid = int(pidfile.read_text().strip())
        time.sleep(0.2)
        bot._kill_tree(parent.pid)
        deadline = time.time() + 5
        while time.time() < deadline and (_alive(parent.pid) or _alive(loop_pid)):
            time.sleep(0.05)
        assert not _alive(parent.pid) and not _alive(loop_pid), "tree survived"
        # neither is parked in STOPPED state
        assert _proc_state(parent.pid) != "T" and _proc_state(loop_pid) != "T", \
            "a frozen process was left STOPPED"
    finally:
        for pid in ([parent.pid, loop_pid] if loop_pid else [parent.pid]):
            try:
                os.kill(pid, _sig.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            parent.wait(timeout=5)
        except Exception:
            pass


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


def test_collect_tree_finds_setsid_descendants_even_when_parent_stopped(tmp_path):
    """_collect_tree must find a setsid() descendant via the independent PPID walk,
    NOT the parent's per-task `children` file — including when the parent is
    SIGSTOPped (the children file could under-report; the PPID walk reads the
    descendant's own /proc/<pid>/stat, which is reliable regardless)."""
    import os
    import signal as _sig
    pidfile = tmp_path / "loop.pid"
    parent = subprocess.Popen(
        ["bash", "-c",
         f"setsid bash -c 'echo $$ > {pidfile}; while true; do sleep 300; done' & wait"])
    loop_pid = None
    try:
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        loop_pid = int(pidfile.read_text().strip())
        time.sleep(0.2)
        # setsid'd loop is in its OWN session/pgroup but PPID still points at parent
        os.kill(parent.pid, _sig.SIGSTOP)         # even with parent stopped ...
        try:
            tree = bot._collect_tree(parent.pid)
        finally:
            os.kill(parent.pid, _sig.SIGCONT)
        assert parent.pid in tree
        assert loop_pid in tree, "PPID walk missed the setsid session-leader descendant"
    finally:
        for pid in ([parent.pid, loop_pid] if loop_pid else [parent.pid]):
            try:
                os.kill(pid, _sig.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            parent.wait(timeout=5)
        except Exception:
            pass


def test_proc_ppid_reads_parent_and_none_when_gone():
    import os
    p = subprocess.Popen(["sleep", "5"])
    try:
        assert bot._proc_ppid(p.pid) == os.getpid()
    finally:
        p.kill()
        p.wait(timeout=5)
    assert bot._proc_ppid(p.pid) is None


def test_spawn_timeout_default_is_120_minutes():
    assert bot.SPAWN_TIMEOUT_SECONDS == 7200
