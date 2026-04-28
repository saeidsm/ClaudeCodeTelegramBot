"""Tests for compute_verdict / explain_verdict — Phase-2B-fix2.

Cover every CRITICAL trigger (a..f) and every NEEDS ATTENTION trigger (a..f),
plus the genuinely-clean case, plus the 2026-04-26 false-negative regression.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.publisher import (
    VERDICT_THRESHOLDS,
    compute_verdict,
    explain_verdict,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ──────────────────────────────────────────────────────────────────────────


def _scored(**overrides) -> dict:
    base = {
        "issue_id": "1",
        "project_slug": "shahrzad-backend",
        "title": "ExampleError: x",
        "level": "warning",
        "count": 1,
        "user_count": 0,
        "is_new": False,
        "is_regression": False,
        "is_spike": False,
        "is_release_correlated": False,
        "is_user_impacting": False,
        "in_cluster": False,
        "severity_score": 0,
        "reasons": [],
    }
    base.update(overrides)
    return base


# ──────────────────────────────────────────────────────────────────────────
# CRITICAL — every condition (a..f)
# ──────────────────────────────────────────────────────────────────────────


def test_critical_a_fatal_level() -> None:
    icon, label = compute_verdict([_scored(level="fatal", count=1, severity_score=20)], decisions=[])
    assert (icon, label) == ("🚨", "CRITICAL")
    reasons = explain_verdict([_scored(level="fatal", count=1, severity_score=20)], decisions=[])
    assert any("fatal-level" in r for r in reasons)


def test_critical_b_volume_bomb() -> None:
    n = VERDICT_THRESHOLDS["critical"]["count_volume_bomb"]
    icon, label = compute_verdict(
        [_scored(level="error", count=n, severity_score=10)], decisions=[]
    )
    assert (icon, label) == ("🚨", "CRITICAL")
    reasons = explain_verdict(
        [_scored(level="error", count=n, severity_score=10, title="X")], decisions=[]
    )
    assert any(str(n) in r and "events" in r for r in reasons)


def test_critical_c_severity_score() -> None:
    n = VERDICT_THRESHOLDS["critical"]["severity_score"]
    icon, label = compute_verdict(
        [_scored(level="error", severity_score=n, count=1)], decisions=[]
    )
    assert (icon, label) == ("🚨", "CRITICAL")


def test_critical_d_spike_fanout() -> None:
    n = VERDICT_THRESHOLDS["critical"]["spike_count"]
    items = [_scored(level="error", is_spike=True, severity_score=20) for _ in range(n)]
    icon, label = compute_verdict(items, decisions=[])
    assert (icon, label) == ("🚨", "CRITICAL")
    reasons = explain_verdict(items, decisions=[])
    assert any("spike" in r.lower() for r in reasons)


def test_critical_e_new_spike_cluster_cooccur() -> None:
    items = [
        _scored(issue_id="a", is_new=True, severity_score=10),
        _scored(issue_id="b", is_spike=True, severity_score=10),
    ]
    clusters = [{"members": ["a", "b"], "confidence": 0.7}]
    icon, label = compute_verdict(items, decisions=[], clusters=clusters)
    assert (icon, label) == ("🚨", "CRITICAL")
    reasons = explain_verdict(items, decisions=[], clusters=clusters)
    assert any("co-occur" in r for r in reasons)


def test_critical_f_decision_is_critical() -> None:
    decisions = [{"kind": "x", "summary": "y", "is_critical": True}]
    icon, label = compute_verdict([_scored(severity_score=10)], decisions=decisions)
    assert (icon, label) == ("🚨", "CRITICAL")
    reasons = explain_verdict([_scored(severity_score=10)], decisions=decisions)
    assert any("is_critical" in r for r in reasons)


# ──────────────────────────────────────────────────────────────────────────
# NEEDS ATTENTION — every condition (a..f)
# ──────────────────────────────────────────────────────────────────────────


def test_attention_a_error_count() -> None:
    n = VERDICT_THRESHOLDS["needs_attention"]["error_count"]
    icon, label = compute_verdict(
        [_scored(level="error", count=n, severity_score=10)], decisions=[]
    )
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


def test_attention_b_severity_score() -> None:
    n = VERDICT_THRESHOLDS["needs_attention"]["severity_score"]
    icon, label = compute_verdict(
        [_scored(level="error", severity_score=n, count=1)], decisions=[]
    )
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


def test_attention_c_one_spike() -> None:
    icon, label = compute_verdict(
        [_scored(level="error", is_spike=True, severity_score=10, count=5)],
        decisions=[],
    )
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


def test_attention_d_one_regression() -> None:
    icon, label = compute_verdict(
        [_scored(level="error", is_regression=True, severity_score=10, count=5)],
        decisions=[],
    )
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


def test_attention_e_decision_present() -> None:
    icon, label = compute_verdict(
        [_scored(severity_score=10)], decisions=[{"kind": "k", "summary": "s"}]
    )
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


def test_attention_f_new_count() -> None:
    n = VERDICT_THRESHOLDS["needs_attention"]["new_count"]
    items = [_scored(is_new=True, severity_score=20) for _ in range(n)]
    icon, label = compute_verdict(items, decisions=[])
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


# ──────────────────────────────────────────────────────────────────────────
# ALL CLEAR — genuinely clean snapshot
# ──────────────────────────────────────────────────────────────────────────


def test_all_clear_clean_snapshot() -> None:
    items = [_scored(level="warning", severity_score=20, count=3)]
    icon, label = compute_verdict(items, decisions=[])
    assert (icon, label) == ("✅", "ALL CLEAR")
    assert explain_verdict(items, decisions=[]) == []


# ──────────────────────────────────────────────────────────────────────────
# Real snapshot: 2026-04-26 must now produce CRITICAL (regression test)
# ──────────────────────────────────────────────────────────────────────────


def test_2026_04_26_real_snapshot_is_critical() -> None:
    """The bug that motivated this PR: 19 spikes + 806-event runaway returned
    ALL CLEAR. After the fix, the same snapshot must produce CRITICAL with
    at least 2 reasons."""
    snap_dir = Path("/opt/sentry-nightwatch/snapshots/2026-04-26")
    if not snap_dir.exists():
        # Test only runs on the dev host that has the snapshot.
        import pytest
        pytest.skip("real snapshot fixture not present on this host")

    top_issues = json.loads((snap_dir / "top_issues.json").read_text(encoding="utf-8"))
    decisions = json.loads((snap_dir / "decisions.json").read_text(encoding="utf-8"))
    clusters = json.loads((snap_dir / "clusters.json").read_text(encoding="utf-8"))

    icon, label = compute_verdict(top_issues, decisions, clusters=clusters)
    assert (icon, label) == ("🚨", "CRITICAL")

    reasons = explain_verdict(top_issues, decisions, clusters=clusters)
    assert len(reasons) >= 2, f"expected ≥ 2 reasons, got {reasons}"

    # Specifically these two should fire:
    joined = " | ".join(reasons)
    assert "spiked" in joined, f"expected spike-fanout reason, got {joined}"
    assert "events" in joined, f"expected volume-bomb reason, got {joined}"
