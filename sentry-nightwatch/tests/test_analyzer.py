"""Tests for app.analyzer and app.normalizer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analyzer import detect_decision_required, score_all, score_issue
from app.config import load_rules
from app.normalizer import (
    Cluster,
    cluster_cross_project,
    normalize_issue,
)

FIXTURES = Path(__file__).parent / "fixtures"
RULES = load_rules()
NOW = datetime.now(timezone.utc)


def _issues(slug: str) -> list[dict]:
    sample = json.loads((FIXTURES / "sentry_issues_sample.json").read_text(encoding="utf-8"))
    return sample[slug]


def _all_issues() -> list[dict]:
    sample = json.loads((FIXTURES / "sentry_issues_sample.json").read_text(encoding="utf-8"))
    out = []
    for k, v in sample.items():
        if not k.startswith("_"):
            for raw in v:
                out.append(normalize_issue(raw, k))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Normalizer
# ──────────────────────────────────────────────────────────────────────────


def test_normalize_basic_fields() -> None:
    raw = _issues("shahrzad-backend")[0]
    n = normalize_issue(raw, "shahrzad-backend")
    assert n["issue_id"] == "5001"
    assert n["title"].startswith("DatabaseError")
    assert n["level"] == "fatal"
    assert n["count"] == 412
    assert n["user_count"] == 87
    assert n["release"] == "backend@1.4.7"
    assert n["environment"] == "production"
    assert len(n["stack_signature"]) == 16


def test_normalize_handles_missing_tags() -> None:
    raw = {"id": "x", "title": "t"}
    n = normalize_issue(raw, "shahrzad-backend")
    assert n["release"] is None
    assert n["environment"] is None


def test_clustering_finds_backend_frontend_pair() -> None:
    issues = _all_issues()
    clusters = cluster_cross_project(issues, window_minutes=5)
    # We expect at least one cluster (BACK-1/5/11 + FRONT-2 share /api/story/create + time).
    assert clusters, "expected at least one cross-project cluster"
    # Confidence reasonable.
    assert all(0.0 <= c.confidence <= 1.0 for c in clusters)
    # Members include one backend + one frontend issue id.
    for c in clusters:
        assert len(c.members) == 2


# ──────────────────────────────────────────────────────────────────────────
# Analyzer / scoring
# ──────────────────────────────────────────────────────────────────────────


def test_score_fatal_with_users_high() -> None:
    issues = _all_issues()
    scored = score_all(
        issues,
        baseline={},
        recent_releases=[
            {"version": "backend@1.4.7", "dateCreated": "2026-04-24T01:00:00Z"},
            {"version": "frontend@2.0.3", "dateCreated": "2026-04-24T01:05:00Z"},
        ],
        rules=RULES,
        clusters=cluster_cross_project(issues),
    )
    # The DB pool exhaustion should be near the top (fatal + lots of users).
    by_id = {s["issue_id"]: s for s in scored}
    db = by_id["5001"]
    assert db["severity_score"] >= 50
    assert db["is_user_impacting"] is True


def test_noisy_low_impact_scored_low() -> None:
    issues = _all_issues()
    scored = score_all(
        issues, baseline={}, recent_releases=[], rules=RULES, clusters=[]
    )
    by_id = {s["issue_id"]: s for s in scored}
    # 5013 (BACK-13) — single user, warning, near-stale. Should be very low.
    assert by_id["5013"]["severity_score"] < 30


def test_release_correlation_detected() -> None:
    raw = {
        "id": "9001",
        "title": "X",
        "level": "error",
        "count": 1,
        "userCount": 0,
        "firstSeen": "2026-04-24T01:10:00Z",
        "lastSeen": "2026-04-24T02:00:00Z",
        "tags": [{"key": "release", "value": "backend@1.4.7"}],
    }
    n = normalize_issue(raw, "shahrzad-backend")
    s = score_issue(
        n,
        baseline={},
        recent_releases=[{"version": "backend@1.4.7", "dateCreated": "2026-04-24T01:00:00Z"}],
        rules=RULES,
        now=datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
    )
    assert s["is_release_correlated"]
    assert "release_correlated" in s["reasons"]


def test_decision_release_rollback_review() -> None:
    # Pin "now" to right after the fixture's anchor date so issues are still "new".
    pinned = datetime(2026, 4, 24, 18, 0, tzinfo=timezone.utc)
    issues = _all_issues()
    scored = score_all(
        issues,
        baseline={},
        recent_releases=[
            {"version": "backend@1.4.7", "dateCreated": "2026-04-24T01:00:00Z"},
            {"version": "frontend@2.0.3", "dateCreated": "2026-04-24T01:05:00Z"},
        ],
        rules=RULES,
        clusters=cluster_cross_project(issues),
        now=pinned,
    )
    decisions = detect_decision_required(scored, cluster_cross_project(issues))
    kinds = {d.kind for d in decisions}
    assert "release_rollback_review" in kinds


def test_baseline_spike() -> None:
    raw = {
        "id": "spike1",
        "title": "T",
        "level": "error",
        "count": 200,
        "userCount": 5,
        "firstSeen": "2026-04-23T10:00:00Z",
        "lastSeen": "2026-04-24T10:00:00Z",
    }
    n = normalize_issue(raw, "shahrzad-backend")
    baseline = {n["stack_signature"]: {"daily": {"d": 10}, "avg_daily": 10.0}}
    s = score_issue(n, baseline=baseline, recent_releases=[], rules=RULES)
    assert s["is_spike"]


def test_severity_capped_at_100() -> None:
    raw = {
        "id": "z",
        "title": "T",
        "level": "fatal",
        "count": 99999,
        "userCount": 9999,
        "firstSeen": "2026-04-24T00:00:00Z",
        "lastSeen": "2026-04-24T01:00:00Z",
        "isRegression": True,
        "tags": [{"key": "release", "value": "backend@1.4.7"}],
    }
    n = normalize_issue(raw, "shahrzad-backend")
    s = score_issue(
        n,
        baseline={},
        recent_releases=[{"version": "backend@1.4.7", "dateCreated": "2026-04-24T00:00:00Z"}],
        rules=RULES,
        cluster_member_ids={"z"},
    )
    assert s["severity_score"] == 100


@pytest.mark.parametrize("hours_back, expected_new", [(1, True), (240, False)])
def test_is_new_window(hours_back: int, expected_new: bool) -> None:
    raw = {
        "id": "n1",
        "title": "T",
        "level": "error",
        "count": 1,
        "userCount": 0,
        "firstSeen": (NOW - timedelta(hours=hours_back)).isoformat().replace("+00:00", "Z"),
        "lastSeen": NOW.isoformat().replace("+00:00", "Z"),
    }
    n = normalize_issue(raw, "shahrzad-backend")
    s = score_issue(n, baseline={}, recent_releases=[], rules=RULES, now=NOW)
    assert s["is_new"] == expected_new


def test_cluster_dataclass_to_dict() -> None:
    c = Cluster(members=["a", "b"], confidence=0.7, reason="time", shared_route="/api/x")
    d = c.to_dict()
    assert d["members"] == ["a", "b"]
    assert d["confidence"] == 0.7
