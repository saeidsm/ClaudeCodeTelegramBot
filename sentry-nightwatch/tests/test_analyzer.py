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
    """2026-04-28 verdict-delta: spike is now measured against count_24h.

    Stats attached so count_24h=200 gets compared to baseline avg=10.
    """
    raw = {
        "id": "spike1",
        "title": "T",
        "level": "error",
        "count": 200,
        "userCount": 5,
        "firstSeen": "2026-04-23T10:00:00Z",
        "lastSeen": "2026-04-24T10:00:00Z",
        "stats": {"24h": [[1, 200]]},
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


# ──────────────────────────────────────────────────────────────────────────
# 2026-04-28 verdict-delta fix: scoring + festering bonus
# ──────────────────────────────────────────────────────────────────────────


def _raw(**overrides) -> dict:
    base = {
        "id": "9100",
        "shortId": "TEST-100",
        "title": "T",
        "level": "error",
        "count": 0,
        "userCount": 0,
        "firstSeen": "2026-04-22T00:00:00Z",
        "lastSeen":  "2026-04-22T01:00:00Z",
        "project": {"slug": "shahrzad-backend"},
    }
    base.update(overrides)
    return base


def test_spike_uses_count_24h_not_lifetime_count() -> None:
    """A festering issue (count=10000, count_24h=2) is NOT a spike.

    Old logic compared lifetime count against baseline; the bug we hit
    on 2026-04-27 had a 817-event lifetime count, count_24h=0, and old
    logic still raised a spike. New logic uses count_24h."""
    from app.normalizer import normalize_issue
    raw = _raw(count=10000, stats={"24h": [[1, 2]]})
    n = normalize_issue(raw, "shahrzad-backend")
    # Baseline is empty so _is_spike falls through to the "no baseline"
    # branch which checks count_24h ≥ 20, NOT lifetime count.
    s = score_issue(n, baseline={}, recent_releases=[], rules=RULES)
    assert s["is_spike"] is False, (
        f"festering issue (count=10000, count_24h=2) must not be flagged as spike; "
        f"got is_spike={s['is_spike']}"
    )


def test_spike_fires_when_count_24h_above_baseline_multiplier() -> None:
    """count_24h > 3× baseline_avg ⇒ spike."""
    from app.normalizer import normalize_issue
    raw = _raw(count=500, stats={"24h": [[1, 50]]})
    n = normalize_issue(raw, "shahrzad-backend")
    baseline = {n["stack_signature"]: {"avg_daily": 10.0, "daily": {}}}
    s = score_issue(n, baseline=baseline, recent_releases=[], rules=RULES)
    assert s["is_spike"] is True


def test_festering_bonus_caps_at_10_points() -> None:
    """count > 1000 (lifetime) but count_24h tiny → small bonus, can't reach 50."""
    from app.normalizer import normalize_issue
    # warning level (5), no other flags → base score 5 + festering bonus 10 → 15
    raw = _raw(level="warning", count=10000, userCount=0,
               stats={"24h": [[1, 1]]},
               firstSeen="2026-04-01T00:00:00Z")  # not new
    n = normalize_issue(raw, "shahrzad-backend")
    s = score_issue(n, baseline={}, recent_releases=[], rules=RULES)
    # warning weight = 5, festering bonus = 10 → 15
    assert s["severity_score"] < 50, (
        f"festering bonus alone must not push severity to NEEDS_ATTENTION; got {s['severity_score']}"
    )
    assert s["severity_score"] >= 5  # at least the level weight


def test_festering_bonus_not_awarded_below_threshold() -> None:
    """Lifetime count < 1000 → no festering bonus."""
    from app.normalizer import normalize_issue
    raw = _raw(level="warning", count=500, stats={"24h": [[1, 1]]})
    n = normalize_issue(raw, "shahrzad-backend")
    s = score_issue(n, baseline={}, recent_releases=[], rules=RULES)
    # Just the level=warning weight (5), no festering, no other flags except possibly is_new
    assert s["severity_score"] <= 25  # warning(5) + maybe is_new(15) + cushion
