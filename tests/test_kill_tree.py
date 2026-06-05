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


def test_spawn_timeout_default_is_120_minutes():
    assert bot.SPAWN_TIMEOUT_SECONDS == 7200
