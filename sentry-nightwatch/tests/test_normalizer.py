"""Tests for app.normalizer — issue + event tag merge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from app.normalizer import (
    extract_event_tags,
    normalize_issue,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ──────────────────────────────────────────────────────────────────────────
# extract_event_tags
# ──────────────────────────────────────────────────────────────────────────


def _load_event() -> dict:
    from app.redactor import expand_test_placeholders
    raw = (FIXTURES / "sentry_event_full.json").read_text(encoding="utf-8")
    return json.loads(expand_test_placeholders(raw))


def test_extract_event_tags_from_fixture() -> None:
    event = _load_event()
    tags = extract_event_tags(event)
    assert tags["release"] == "backend@1.4.7"
    assert tags["environment"] == "production"
    assert tags["server_name"] == "shahrzad-prod-1"
    assert "url" in tags  # other tags preserved


def test_extract_event_tags_none() -> None:
    assert extract_event_tags(None) == {}


def test_extract_event_tags_empty_dict() -> None:
    assert extract_event_tags({}) == {}


def test_extract_event_tags_falls_back_to_top_level() -> None:
    event = {"release": "v1", "environment": "staging", "tags": []}
    tags = extract_event_tags(event)
    assert tags["release"] == "v1"
    assert tags["environment"] == "staging"


def test_extract_event_tags_array_wins_over_top_level() -> None:
    event = {
        "release": "fallback-rel",
        "environment": "fallback-env",
        "tags": [
            {"key": "release", "value": "tag-rel"},
            {"key": "environment", "value": "tag-env"},
        ],
    }
    tags = extract_event_tags(event)
    assert tags["release"] == "tag-rel"
    assert tags["environment"] == "tag-env"


def test_extract_event_tags_skips_malformed() -> None:
    event = {"tags": [None, {"key": "release"}, "bad", {"key": "env", "value": "ok"}]}
    tags = extract_event_tags(event)
    assert tags == {"env": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# normalize_issue with/without event
# ──────────────────────────────────────────────────────────────────────────


def _bare_issue() -> dict:
    """A live-shaped issue (no `tags`, no `isRegression`) — like Sentry's list endpoint."""
    return {
        "id": "5004",
        "shortId": "BACK-4",
        "title": "StripeError: card_declined",
        "level": "error",
        "status": "unresolved",
        "platform": "python",
        "count": "41",
        "userCount": 38,
        "firstSeen": "2026-04-24T05:00:00Z",
        "lastSeen": "2026-04-24T19:01:00Z",
        "culprit": "app.payments.stripe_handler",
        "permalink": "https://sentry.io/x",
        "project": {"slug": "shahrzad-backend"},
    }


def test_normalize_issue_event_none_no_release_or_env() -> None:
    n = normalize_issue(_bare_issue(), "shahrzad-backend", event=None)
    assert n["release"] is None
    assert n["environment"] is None
    assert n["issue_id"] == "5004"


def test_normalize_issue_with_event_pulls_release_env() -> None:
    event = _load_event()
    n = normalize_issue(_bare_issue(), "shahrzad-backend", event=event)
    assert n["release"] == "backend@1.4.7"
    assert n["environment"] == "production"


def test_normalize_issue_with_event_falls_back_to_issue_when_event_missing_field() -> None:
    raw = {
        **_bare_issue(),
        "tags": [
            {"key": "release", "value": "issue-fallback-rel"},
            {"key": "environment", "value": "issue-fallback-env"},
        ],
    }
    event_without_release = {"tags": [{"key": "user", "value": "u1"}]}
    n = normalize_issue(raw, "shahrzad-backend", event=event_without_release)
    assert n["release"] == "issue-fallback-rel"
    assert n["environment"] == "issue-fallback-env"


def test_normalize_issue_event_release_overrides_issue_release_with_warning() -> None:
    raw = {
        **_bare_issue(),
        "tags": [
            {"key": "release", "value": "backend@1.4.6"},
            {"key": "environment", "value": "staging"},
        ],
    }
    event = {
        "tags": [
            {"key": "release", "value": "backend@1.4.7"},
            {"key": "environment", "value": "production"},
        ]
    }
    with structlog.testing.capture_logs() as logs:
        n = normalize_issue(raw, "shahrzad-backend", event=event)
    assert n["release"] == "backend@1.4.7"
    assert n["environment"] == "production"
    rel_warnings = [r for r in logs if r.get("event") == "normalizer.release_conflict"]
    env_warnings = [r for r in logs if r.get("event") == "normalizer.environment_conflict"]
    assert rel_warnings, f"expected release conflict warning; got {logs}"
    assert env_warnings, f"expected environment conflict warning; got {logs}"
    assert rel_warnings[0]["log_level"] == "warning"
    assert rel_warnings[0]["resolution"] == "event_wins"
    assert rel_warnings[0]["issue_release"] == "backend@1.4.6"
    assert rel_warnings[0]["event_release"] == "backend@1.4.7"


def test_normalize_issue_no_warning_when_release_matches() -> None:
    raw = {**_bare_issue(), "tags": [{"key": "release", "value": "backend@1.4.7"}]}
    event = {"tags": [{"key": "release", "value": "backend@1.4.7"}]}
    with structlog.testing.capture_logs() as logs:
        normalize_issue(raw, "shahrzad-backend", event=event)
    rel_warnings = [r for r in logs if r.get("event") == "normalizer.release_conflict"]
    assert not rel_warnings, "no warning expected when release values agree"


@pytest.mark.parametrize(
    "issue_release, event_release, expected",
    [
        (None, "v2", "v2"),     # only event
        ("v1", None, "v1"),     # only issue
        (None, None, None),     # neither
        ("v1", "v1", "v1"),     # agreement
    ],
)
def test_normalize_release_resolution_table(
    issue_release: str | None, event_release: str | None, expected: str | None
) -> None:
    raw = dict(_bare_issue())
    if issue_release is not None:
        raw["tags"] = [{"key": "release", "value": issue_release}]
    event = None
    if event_release is not None:
        event = {"tags": [{"key": "release", "value": event_release}]}
    n = normalize_issue(raw, "shahrzad-backend", event=event)
    assert n["release"] == expected
