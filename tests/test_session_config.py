"""Tests for session-vs-agent limits and config parsing (Section F1 / H)."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


@pytest.mark.parametrize("raw,default,expected", [
    ("9", 4, 9),
    ("2", 5, 2),
    ("", 4, 4),          # unset → default
    ("0", 4, 4),         # non-positive → default
    ("-3", 4, 4),        # negative → default
    ("abc", 4, 4),       # non-int → default
    ("  ", 2, 2),        # blank → default
])
def test_positive_int_env(monkeypatch, raw, default, expected):
    monkeypatch.setenv("BOT_TEST_LIMIT", raw)
    assert bot._positive_int_env("BOT_TEST_LIMIT", default) == expected


def test_positive_int_env_missing_key():
    # a name that is definitely unset
    assert bot._positive_int_env("BOT_DEFINITELY_UNSET_XYZ", 7) == 7


def test_logical_session_limit_nine(monkeypatch):
    monkeypatch.setattr(bot, "MAX_SESSIONS", 9)
    sm = bot.SessionManager()
    chat = 4242
    for i in range(9):
        sm.create(chat, f"s{i}")
    assert len(sm.active_for_chat(chat)) == 9
    assert sm.can_create(chat) is False        # 10th rejected
    # freeing one lets a new one in
    sm.kill(chat, "s0")
    assert sm.can_create(chat) is True


def test_help_text_has_no_stale_max_3():
    src = inspect.getsource(bot)
    assert "(max 3)" not in src                 # stale hardcoded string removed
    # the help line is now derived from MAX_SESSIONS
    assert "(max {MAX_SESSIONS})" in src


def test_six_sessions_still_restore(tmp_path, monkeypatch):
    # Build a saved-state file with six sessions and confirm load_state restores
    # all six (the pre-existing prod state size) after the Phase 1 changes.
    import json, time
    state = {
        "version": 1, "saved_at": time.time(),
        "sessions": {
            f"777:{name}": {
                "id": f"777:{name}", "label": name, "color_emoji": "🔵",
                "session_uuid": f"uuid-{i}", "project": "ZigguratKids4",
                "status": "idle", "started_at": time.time(), "last_active": time.time(),
                "message_ids": [], "anchor_message_id": None, "tasks": 0,
                "claude_created": True, "claude_model": "",
            } for i, name in enumerate(["ZigMusic", "aksalret", "Rag2", "Shaghayeg3", "TelgAppp", "Dastyar"])
        },
        "msg_to_session": {}, "color_index": {}, "active_session": {},
    }
    sf = tmp_path / "bot-state.json"
    sf.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(bot, "STATE_FILE", str(sf))

    saved = dict(bot.SM.sessions)
    try:
        bot.SM.sessions.clear()
        bot.load_state()
        restored = [k for k in bot.SM.sessions if k.startswith("777:")]
        assert len(restored) == 6
    finally:
        bot.SM.sessions.clear()
        bot.SM.sessions.update(saved)


def test_running_status_restored_as_error(tmp_path, monkeypatch):
    import json, time
    state = {
        "version": 1, "saved_at": time.time(),
        "sessions": {
            "888:live": {
                "id": "888:live", "label": "live", "color_emoji": "🟢",
                "session_uuid": "u", "project": "ZigguratKids4", "status": "queued",
                "started_at": time.time(), "last_active": time.time(),
                "message_ids": [], "anchor_message_id": None, "tasks": 0,
                "claude_created": True, "claude_model": "",
            }
        },
        "msg_to_session": {}, "color_index": {}, "active_session": {},
    }
    sf = tmp_path / "s.json"; sf.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(bot, "STATE_FILE", str(sf))
    saved = dict(bot.SM.sessions)
    try:
        bot.SM.sessions.clear()
        bot.load_state()
        # a 'queued' session (dead slot after restart) is downgraded to error
        assert bot.SM.sessions["888:live"].status == "error"
    finally:
        bot.SM.sessions.clear(); bot.SM.sessions.update(saved)
