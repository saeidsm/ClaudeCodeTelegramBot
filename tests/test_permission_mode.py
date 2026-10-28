"""Phase 0 / D3: `skip` mode must refuse to start without an explicit opt-in.

`--dangerously-skip-permissions` bypasses settings.json `permissions.deny`
entirely, so the deny list protects nothing in skip mode. These tests pin the
"two deliberate acts" rule: mode alone must not be enough to disarm it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


@pytest.mark.parametrize("mode", ["allowlist", "strict"])
def test_safe_modes_start(monkeypatch, mode):
    monkeypatch.setattr(bot, "BOT_TOOL_PERMISSION_MODE", mode)
    monkeypatch.setattr(bot, "BOT_ALLOW_SKIP_MODE", False)
    bot._assert_permission_mode()          # must not raise


def test_skip_without_optin_refuses(monkeypatch):
    monkeypatch.setattr(bot, "BOT_TOOL_PERMISSION_MODE", "skip")
    monkeypatch.setattr(bot, "BOT_ALLOW_SKIP_MODE", False)
    with pytest.raises(SystemExit):
        bot._assert_permission_mode()


def test_skip_with_optin_starts(monkeypatch):
    monkeypatch.setattr(bot, "BOT_TOOL_PERMISSION_MODE", "skip")
    monkeypatch.setattr(bot, "BOT_ALLOW_SKIP_MODE", True)
    bot._assert_permission_mode()          # two deliberate acts → allowed


def test_optin_alone_is_not_skip(monkeypatch):
    """BOT_ALLOW_SKIP_MODE=1 on its own must not change a safe mode."""
    monkeypatch.setattr(bot, "BOT_TOOL_PERMISSION_MODE", "allowlist")
    monkeypatch.setattr(bot, "BOT_ALLOW_SKIP_MODE", True)
    bot._assert_permission_mode()          # must not raise
