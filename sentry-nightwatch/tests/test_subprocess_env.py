"""Regression test for the bot-spawned subprocess bug.

Before this fix, `python -m app.main run` spawned by the bot inherited only
the bot's env (no SENTRY_AUTH_TOKEN), causing the pipeline to silently
degrade to `sentry_unreachable` on every tick. This test reproduces the
spawn pattern (no pre-sourced .env) and asserts the loader populates the
env from NIGHTWATCH_ENV_FILE.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # /opt/sentry-nightwatch


def test_subprocess_picks_up_env_file_via_loader(tmp_path: Path) -> None:
    fixture_env = tmp_path / "fixture.env"
    fixture_env.write_text("NW_TEST_SENTINEL=hello123\n", encoding="utf-8")

    # Spawn with a deliberately *minimal* env — no SENTRY_AUTH_TOKEN, no
    # BOT_IPC_*, no NW_TEST_SENTINEL itself. Only PATH (so python and stdlib
    # find one another) and NIGHTWATCH_ENV_FILE pointing at our fixture.
    minimal_env = {
        "PATH": "/usr/bin:/bin",
        "NIGHTWATCH_ENV_FILE": str(fixture_env),
    }

    result = subprocess.run(
        [sys.executable, "-m", "app.main", "__echo_env", "NW_TEST_SENTINEL"],
        cwd=str(REPO_ROOT),
        env=minimal_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "hello123", result.stdout


def test_subprocess_real_env_overrides_file(tmp_path: Path) -> None:
    """Real env wins (setdefault semantics)."""
    fixture_env = tmp_path / "fixture.env"
    fixture_env.write_text("NW_TEST_SENTINEL=fromfile\n", encoding="utf-8")

    minimal_env = {
        "PATH": "/usr/bin:/bin",
        "NIGHTWATCH_ENV_FILE": str(fixture_env),
        "NW_TEST_SENTINEL": "fromrealenv",  # already in env → loader skips
    }

    result = subprocess.run(
        [sys.executable, "-m", "app.main", "__echo_env", "NW_TEST_SENTINEL"],
        cwd=str(REPO_ROOT),
        env=minimal_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "fromrealenv"
