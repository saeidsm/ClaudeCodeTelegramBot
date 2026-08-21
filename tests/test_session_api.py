"""Tests for the read-only session query API (Phase 0 A3 Task 2).

Covers: token auth (missing / wrong / correct, constant-time compare), the
response shape (required fields present, no prompt/transcript/path leakage),
and counts/active_session aggregation — all via aiohttp's
make_mocked_request, no real TCP listener needed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402

TOKEN = "test-session-api-token"


def _req(token: str | None = None):
    headers = {"X-Session-API-Token": token} if token is not None else {}
    return make_mocked_request("GET", "/sessions", headers=headers)


def test_authorized_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", TOKEN)
    assert bot._sessapi_authorized(_req()) is False


def test_authorized_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", TOKEN)
    assert bot._sessapi_authorized(_req("nope")) is False


def test_authorized_rejects_when_secret_unset(monkeypatch):
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", "")
    assert bot._sessapi_authorized(_req(TOKEN)) is False


def test_authorized_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", TOKEN)
    assert bot._sessapi_authorized(_req(TOKEN)) is True


@pytest.mark.asyncio
async def test_sessions_endpoint_requires_auth(monkeypatch):
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", TOKEN)
    resp = await bot._sessapi_sessions(_req("wrong"))
    assert resp.status == 403
    body = json.loads(resp.body)
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_sessions_endpoint_shape_and_no_leakage(monkeypatch):
    monkeypatch.setattr(bot, "BOT_SESSION_API_TOKEN", TOKEN)
    s1 = bot.Session(id="1:a", label="a", color_emoji="🔵", session_uuid="uuid-1",
                      project="ZigguratKids4", status="running", engine="claude",
                      claude_model="opus", out="SECRET PROMPT TEXT",
                      chat_history_ref="/tmp/claude-sessions/1_a/history")
    s2 = bot.Session(id="1:b", label="b", color_emoji="🟢", session_uuid="uuid-2",
                      project="Shahrzad-Manager", status="idle", engine="codex",
                      model="gpt-5.6-sol", provider_session_id="thread-xyz")
    monkeypatch.setattr(bot.SM, "sessions", {"1:a": s1, "1:b": s2})
    monkeypatch.setattr(bot, "ACTIVE_SESSION", {1: "1:a"})

    resp = await bot._sessapi_sessions(_req(TOKEN))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True

    by_id = {s["id"]: s for s in body["sessions"]}
    assert set(by_id) == {"1:a", "1:b"}
    assert by_id["1:a"]["engine"] == "claude" and by_id["1:a"]["model"] == "opus"
    assert by_id["1:b"]["engine"] == "codex" and by_id["1:b"]["model"] == "gpt-5.6-sol"
    for s in body["sessions"]:
        # EXACT set, deliberately: the payload is a reviewed contract and a new
        # field is a security decision, not a feature decision. Amend this only
        # together with the review note in _sessapi_sessions.
        # Phase 2D added two, both reviewed there:
        #   status_reason - distinguishes a restart marker from a real failure
        #   has_error     - whether error_text is worth fetching
        # Neither carries a path, a uuid, prompt text or a transcript.
        assert set(s) == {"id", "label", "engine", "model", "project",
                           "status", "status_reason", "has_error",
                           "started_at", "last_active"}

    assert body["active_session"] == {"1": "1:a"}
    assert body["counts"] == {"total": 2, "running": 1, "idle": 1}

    # No prompt text, transcripts, worktree paths, thread ids, or .env values
    # anywhere in the serialized response.
    raw = resp.body.decode()
    assert "SECRET PROMPT TEXT" not in raw
    assert "/tmp/claude-sessions" not in raw
    assert "thread-xyz" not in raw
    assert "uuid-1" not in raw and "uuid-2" not in raw
