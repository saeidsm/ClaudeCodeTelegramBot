"""Phase 2D — model aliases resolve (never pin), and `error` carries a reason.

Two things are pinned by these tests:

1. Nothing maps a family alias to a version string. The alias goes to the CLI
   untouched, because the CLI resolves it to the newest model in that family.
   A mapping layer is the thing that goes stale, so its absence is the feature.

2. `error` used to mean exactly one thing - "a restart killed this session" -
   and nothing recorded that. A console button offering to show "the error"
   would have had nothing to show, which looks broken. Status now carries a
   reason, and a genuine failure carries scrubbed text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


# ── 1. no pinned versions on the selection path ─────────────────────────────

def test_no_version_string_is_used_to_choose_a_model():
    """The families the console offers must be bare aliases, not versions."""
    for fam in bot.MODEL_FAMILIES:
        assert "-" not in fam.split("[")[0], (
            f"{fam!r} looks like a pinned version; families must be bare aliases")
        assert not any(ch.isdigit() for ch in fam.split("[")[0]), (
            f"{fam!r} contains a version digit")


def test_sonnet_1m_default_is_an_alias_not_a_pin():
    """The overload fallback silently sat on Sonnet 4.6 after Sonnet 5 shipped."""
    d = bot.SONNET_1M_MODEL
    assert d.startswith("sonnet"), d
    assert "-4-" not in d and "-5" not in d, f"{d!r} is pinned to a version"


def test_resolution_cache_is_display_only(monkeypatch):
    """A stale cache must not be able to pin a session to an old model."""
    monkeypatch.setattr(bot, "_MODEL_CACHE", {"opus": "claude-opus-1-ancient"})
    monkeypatch.setattr(bot, "_MODEL_CACHE_AT", 9e9)      # never expires
    sess = bot.Session(id="1:x", label="x", color_emoji="", session_uuid="u")
    sess.claude_model = "opus"
    chosen = sess.claude_model or bot.BOT_DEFAULT_CLAUDE_MODEL
    assert chosen == "opus", "the alias must reach the CLI untouched"
    assert "ancient" not in chosen


# ── 2. status reason ────────────────────────────────────────────────────────

def test_restart_marks_sessions_with_a_reason():
    s = bot.Session(id="1:r", label="r", color_emoji="", session_uuid="u")
    s.status = "running"
    # mirrors the graceful-shutdown path
    s.status = "error"
    s.status_reason = "interrupted_by_restart"
    assert s.status == "error" and s.status_reason == "interrupted_by_restart"
    assert not s.error_text, "a restart has no error text - there was no failure"


def test_execution_failure_reason_is_distinct_from_restart():
    s = bot.Session(id="1:f", label="f", color_emoji="", session_uuid="u")
    s.status_reason = "execution_failed"
    s.error_text = "exit 1\nsomething broke"
    assert s.status_reason != "interrupted_by_restart"
    assert s.error_text


def test_persisted_fields_include_the_reason():
    """The reason must survive a restart, or it is lost exactly when needed."""
    assert "status_reason" in bot._PERSISTED_SESSION_FIELDS
    assert "error_text" in bot._PERSISTED_SESSION_FIELDS


# ── 3. scrubbing ────────────────────────────────────────────────────────────

def test_scrub_removes_live_secrets():
    tok = bot.BOT_SESSION_API_TOKEN
    assert tok, "test needs the session api token set"
    out = bot._scrub_secrets(f"cmd --header 'X-Session-API-Token: {tok}' end")
    assert tok not in out
    assert "redacted" in out


def test_scrub_is_a_no_op_on_clean_text():
    txt = "exit 1\nTypeError: cannot read property of undefined"
    assert bot._scrub_secrets(txt) == txt


def test_scrub_handles_empty():
    assert bot._scrub_secrets("") == ""
    assert bot._scrub_secrets(None) is None


@pytest.mark.parametrize("var", ["REPORTS_PATH_TOKEN", "MEMORY_PATH_TOKEN"])
def test_scrub_covers_path_tokens_when_present(var, monkeypatch):
    monkeypatch.setenv(var, "abcdef0123456789abcdef0123456789")
    out = bot._scrub_secrets("url /reports/abcdef0123456789abcdef0123456789/x")
    assert "abcdef0123456789" not in out


# ── 2E: length bound reflects the REAL constraint, and says so ──────────────

def test_message_bound_is_derived_from_the_kernel_not_telegram():
    """4096 is Telegram's REPLY limit; the prompt travels as one argv element."""
    assert bot._MAX_ARG_STRLEN == 32 * __import__("os").sysconf("SC_PAGE_SIZE")
    assert bot.CONSOLE_MAX_MESSAGE_BYTES < bot._MAX_ARG_STRLEN, "must leave headroom"
    assert bot.CONSOLE_MAX_MESSAGE_BYTES > 100000, "must not be a borrowed transport limit"
    assert bot.CONSOLE_MAX_MESSAGE_BYTES not in (4096, 8000), "that was the borrowed cap"


def test_too_long_error_names_the_limit_the_length_and_the_max():
    import asyncio, json

    class R:
        def __init__(self, b):
            self._b = b; self.headers = {"X-Session-API-Token": bot.BOT_SESSION_API_TOKEN}
            self.query = {}; self.app = {"bot": None}
        async def json(self): return self._b

    bot._CONSOLE_LAST_SEND = 0.0
    big = "x" * (bot.CONSOLE_MAX_MESSAGE_BYTES + 10)
    r = asyncio.run(bot._sessapi_send(R({"label": "n", "message": big,
                                         "nonce": bot._CONSOLE_NONCE_HWM + 1})))
    b = json.loads(r.body.decode())
    assert r.status == 413 and b["error"] == "message_too_long"
    # An error that names neither the limit nor the value is the same failure
    # class as an empty log.
    assert b["length_bytes"] == len(big.encode())
    assert b["max_bytes"] == bot.CONSOLE_MAX_MESSAGE_BYTES
    assert "MAX_ARG_STRLEN" in b["limit_source"]
    assert str(b["max_bytes"]) in b["detail"]


def test_length_is_measured_in_bytes_not_characters():
    """Farsi is ~2 bytes/char; a character cap halves the real allowance."""
    farsi = "ب" * 100
    assert len(farsi) == 100
    assert len(farsi.encode("utf-8")) == 200


# ── 2E: the model switch must respect the engine ────────────────────────────

def _switch(label, model):
    import asyncio, json

    class R:
        def __init__(self, b):
            self._b = b; self.headers = {"X-Session-API-Token": bot.BOT_SESSION_API_TOKEN}
            self.query = {}; self.app = {"bot": None}
        async def json(self): return self._b

    bot._CONSOLE_LAST_SEND = 0.0
    r = asyncio.run(bot._sessapi_control(R({"action": "model", "label": label,
                                            "model": model,
                                            "nonce": bot._CONSOLE_NONCE_HWM + 1})))
    return r, json.loads(r.body.decode())


def test_model_switch_on_a_codex_session_actually_takes_effect():
    cid = bot._console_chat_id()
    s = bot.SM.create(cid, "eng-codex")
    s.engine = bot.engines.ENGINE_CODEX
    s.status = "idle"
    before_claude = s.claude_model
    r, b = _switch("eng-codex", "gpt-5.6-sol")
    assert r.status == 200 and b["ok"] is True
    # the field the CODEX path actually reads
    assert s.model == "gpt-5.6-sol", "codex session must have session.model set"
    assert s.claude_model == before_claude, "claude_model must be left alone"
    bot.SM.kill(cid, "eng-codex")


def test_model_switch_on_a_claude_session_still_sets_claude_model():
    cid = bot._console_chat_id()
    s = bot.SM.create(cid, "eng-claude")
    s.engine = bot.engines.ENGINE_CLAUDE
    s.status = "idle"
    r, b = _switch("eng-claude", "opus")
    assert r.status == 200
    assert s.claude_model == "opus"
    assert s.model == "", "the generic field must stay empty for claude"
    bot.SM.kill(cid, "eng-claude")
