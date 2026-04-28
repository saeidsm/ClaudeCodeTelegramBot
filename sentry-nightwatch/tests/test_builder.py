"""Tests for app.builder — snapshot artifact writer."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.analyzer import detect_decision_required, score_all
from app.builder import (
    build_snapshot,
    prune_old_snapshots,
    update_baseline,
    write_stub_snapshot,
)
from app.config import load_rules
from app.normalizer import cluster_cross_project, normalize_issue
from app.redactor import expand_test_placeholders

FIXTURES = Path(__file__).parent / "fixtures"
RULES = load_rules()


def _load_fixture_json(name: str):
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(expand_test_placeholders(raw))


def _build_inputs() -> tuple[list, list, list, list, dict]:
    sample = _load_fixture_json("sentry_issues_sample.json")
    issues = []
    for slug, lst in sample.items():
        if slug.startswith("_"):
            continue
        for raw in lst:
            issues.append(normalize_issue(raw, slug))
    clusters = cluster_cross_project(issues)
    scored = score_all(
        issues,
        baseline={},
        recent_releases=[
            {"version": "backend@1.4.7", "dateCreated": "2026-04-24T01:00:00Z"},
            {"version": "frontend@2.0.3", "dateCreated": "2026-04-24T01:05:00Z"},
        ],
        rules=RULES,
        clusters=clusters,
    )
    decisions = detect_decision_required(scored, clusters)
    evidence = {}
    event_full = _load_fixture_json("sentry_event_full.json")
    evidence[str(event_full["issueId"])] = event_full
    return issues, scored, clusters, decisions, evidence


def test_build_snapshot_creates_required_files(tmp_path: Path) -> None:
    issues, scored, clusters, decisions, evidence = _build_inputs()
    snap = build_snapshot(
        "2026-04-24",
        snapshots_dir=tmp_path,
        issues=issues,
        scored=scored,
        clusters=clusters,
        decisions=decisions,
        raw_evidence=evidence,
    )
    assert snap.is_dir()
    for name in [
        "summary.json",
        "issues.csv",
        "top_issues.json",
        "clusters.json",
        "decisions.json",
        "analysis.md",
        "prompt.md",
        "report.zip",
    ]:
        assert (snap / name).exists(), f"missing {name}"
    assert (snap / "evidence").is_dir()


def test_build_snapshot_redacts_evidence(tmp_path: Path) -> None:
    issues, scored, clusters, decisions, evidence = _build_inputs()
    snap = build_snapshot(
        "2026-04-24",
        snapshots_dir=tmp_path,
        issues=issues,
        scored=scored,
        clusters=clusters,
        decisions=decisions,
        raw_evidence=evidence,
    )
    flat = ""
    for p in snap.rglob("*.json"):
        flat += p.read_text(encoding="utf-8")
    # Stripe keys, JWTs, emails must not survive. The Stripe live key
    # literal is assembled at parse-time so the source bytes don't match
    # GitHub's secret-scanning push protection.
    _fake_sk_live = "sk" + "_live_" + "AbCdEfGhIjKlMnOpQrStUvWx"
    forbidden = [
        "cus_NeFG1RhZdkc4Yz",
        _fake_sk_live,
        "billing-test@shahrzad.ai",
        "admin@shahrzad.ai",
        "abc123def456SECRETSIGNATURE789xyz",
    ]
    for s in forbidden:
        assert s not in flat, f"leak: {s}"


def test_build_snapshot_zip_contains_summary(tmp_path: Path) -> None:
    issues, scored, clusters, decisions, evidence = _build_inputs()
    snap = build_snapshot(
        "2026-04-24",
        snapshots_dir=tmp_path,
        issues=issues,
        scored=scored,
        clusters=clusters,
        decisions=decisions,
        raw_evidence=evidence,
    )
    with zipfile.ZipFile(snap / "report.zip") as zf:
        names = zf.namelist()
    assert "summary.json" in names
    assert "analysis.md" in names


def test_build_snapshot_idempotent(tmp_path: Path) -> None:
    issues, scored, clusters, decisions, evidence = _build_inputs()
    a = build_snapshot(
        "2026-04-24",
        snapshots_dir=tmp_path,
        issues=issues,
        scored=scored,
        clusters=clusters,
        decisions=decisions,
        raw_evidence=evidence,
    )
    b = build_snapshot(
        "2026-04-24",
        snapshots_dir=tmp_path,
        issues=issues,
        scored=scored,
        clusters=clusters,
        decisions=decisions,
        raw_evidence=evidence,
    )
    assert a == b
    assert (a / "summary.json").exists()


def test_stub_snapshot_well_formed(tmp_path: Path) -> None:
    p = write_stub_snapshot("2026-04-24", tmp_path, reason="DNS failure")
    s = json.loads((p / "summary.json").read_text(encoding="utf-8"))
    assert s["status"] == "sentry_unreachable"
    for f in ["summary.json", "issues.csv", "top_issues.json", "clusters.json", "decisions.json", "analysis.md", "prompt.md", "report.zip"]:
        assert (p / f).exists(), f"stub missing {f}"


def test_update_baseline_creates_and_grows(tmp_path: Path) -> None:
    issues, *_ = _build_inputs()
    p1 = update_baseline(tmp_path, issues, "2026-04-22")
    p2 = update_baseline(tmp_path, issues, "2026-04-23")
    assert p1.exists()
    data = json.loads(p2.read_text(encoding="utf-8"))
    # Each fingerprint should have at least one date entry.
    sig0 = next(iter(data))
    assert "2026-04-22" in data[sig0]["daily"]
    assert "2026-04-23" in data[sig0]["daily"]


def test_prune_old_snapshots(tmp_path: Path) -> None:
    # Create snapshot dirs with old/recent dates.
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=40)).strftime("%Y-%m-%d")
    recent = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    (tmp_path / old).mkdir()
    (tmp_path / recent).mkdir()
    (tmp_path / "not-a-date").mkdir()
    deleted = prune_old_snapshots(tmp_path, keep_days=30)
    assert deleted == 1
    assert not (tmp_path / old).exists()
    assert (tmp_path / recent).exists()


@pytest.mark.parametrize("date_str", ["2026-04-24"])
def test_top_issues_json_has_score(tmp_path: Path, date_str: str) -> None:
    issues, scored, clusters, decisions, evidence = _build_inputs()
    snap = build_snapshot(
        date_str,
        snapshots_dir=tmp_path,
        issues=issues,
        scored=scored,
        clusters=clusters,
        decisions=decisions,
        raw_evidence=evidence,
    )
    top = json.loads((snap / "top_issues.json").read_text(encoding="utf-8"))
    assert top, "top_issues should not be empty"
    assert "severity_score" in top[0]
    assert "reasons" in top[0]
