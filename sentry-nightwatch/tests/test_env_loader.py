"""Tests for the zero-dep .env loader in app.main._load_env_file + cli() integration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from app.main import _load_env_file, cli

# ──────────────────────────────────────────────────────────────────────────
# Unit tests for _load_env_file
# ──────────────────────────────────────────────────────────────────────────


def test_loads_simple_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "env"
    f.write_text("SENTRY_AUTH_TOKEN=fixture123\nFOO=bar\n", encoding="utf-8")
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("FOO", raising=False)

    n = _load_env_file(f)
    assert n == 2
    assert os.environ["SENTRY_AUTH_TOKEN"] == "fixture123"
    assert os.environ["FOO"] == "bar"


def test_preexisting_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """setdefault semantics — file value never overwrites a real env var."""
    f = tmp_path / "env"
    f.write_text("SENTRY_AUTH_TOKEN=fromfile\n", encoding="utf-8")
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "preexisting")

    n = _load_env_file(f)
    assert n == 0  # nothing was applied (already in env)
    assert os.environ["SENTRY_AUTH_TOKEN"] == "preexisting"


def test_missing_file_returns_zero(tmp_path: Path) -> None:
    n = _load_env_file(tmp_path / "does-not-exist")
    assert n == 0


def test_skips_blank_and_comment_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "env"
    f.write_text(
        "# this is a comment\n"
        "\n"
        "  # leading whitespace then comment\n"
        "REAL_KEY=real_val\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("REAL_KEY", raising=False)

    n = _load_env_file(f)
    assert n == 1
    assert os.environ["REAL_KEY"] == "real_val"


def test_skips_malformed_no_equal_sign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "env"
    f.write_text("THIS_LINE_HAS_NO_EQUALS\nKEY1=ok\n", encoding="utf-8")
    monkeypatch.delenv("KEY1", raising=False)

    n = _load_env_file(f)
    assert n == 1
    assert os.environ["KEY1"] == "ok"


def test_strips_matching_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "env"
    f.write_text(
        'DOUBLE="hello"\n'
        "SINGLE='world'\n"
        'MISMATCHED="oops\n'
        "PLAIN=plain_val\n",
        encoding="utf-8",
    )
    for k in ("DOUBLE", "SINGLE", "MISMATCHED", "PLAIN"):
        monkeypatch.delenv(k, raising=False)

    _load_env_file(f)
    assert os.environ["DOUBLE"] == "hello"
    assert os.environ["SINGLE"] == "world"
    assert os.environ["MISMATCHED"] == '"oops'  # mismatched quotes preserved
    assert os.environ["PLAIN"] == "plain_val"


def test_value_can_contain_equals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "env"
    f.write_text("URL=https://x.example.com/p?q=1&r=2\n", encoding="utf-8")
    monkeypatch.delenv("URL", raising=False)
    _load_env_file(f)
    assert os.environ["URL"] == "https://x.example.com/p?q=1&r=2"


def test_empty_value_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "env"
    f.write_text("EMPTY_VAR=\n", encoding="utf-8")
    monkeypatch.delenv("EMPTY_VAR", raising=False)
    n = _load_env_file(f)
    assert n == 1
    assert os.environ["EMPTY_VAR"] == ""


# ──────────────────────────────────────────────────────────────────────────
# CLI integration: cli() loads the file referenced by NIGHTWATCH_ENV_FILE
# ──────────────────────────────────────────────────────────────────────────


def test_cli_loads_env_via_envfile_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "env"
    f.write_text("CLI_TEST_VAR=hello_from_cli\n", encoding="utf-8")
    monkeypatch.setenv("NIGHTWATCH_ENV_FILE", str(f))
    monkeypatch.delenv("CLI_TEST_VAR", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, ["__echo_env", "CLI_TEST_VAR"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "hello_from_cli"


def test_cli_handles_missing_envfile_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIGHTWATCH_ENV_FILE", str(tmp_path / "nope"))
    monkeypatch.delenv("CLI_TEST_VAR", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, ["__echo_env", "CLI_TEST_VAR"])
    assert result.exit_code == 0
    assert result.output.strip() == ""  # var not set; no crash
