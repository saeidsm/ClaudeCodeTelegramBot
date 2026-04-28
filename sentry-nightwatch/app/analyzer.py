"""Rule-based scoring and decision detection.

Pure functions, no I/O. Weights/thresholds are read from rules.yml via config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from app.config import RulesConfig
from app.normalizer import Cluster, NormalizedIssue, _parse_iso


class ScoredIssue(TypedDict):
    issue_id: str
    project_slug: str
    title: str
    level: str
    count: int                # lifetime
    count_24h: int | None     # 2026-04-28 verdict-delta: events in last 24h
    user_count: int
    first_seen: str
    last_seen: str
    release: str | None
    permalink: str | None
    is_new: bool
    is_regression: bool
    is_spike: bool
    is_release_correlated: bool
    is_user_impacting: bool
    in_cluster: bool
    severity_score: int
    reasons: list[str]


@dataclass
class Decision:
    kind: str
    summary: str
    issue_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    # Phase-2B-fix2: surfaced into compute_verdict CRITICAL trigger (f).
    # Existing detect_decision_required() leaves this False; future detectors
    # set it True for decisions that should escalate the verdict.
    is_critical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "issue_ids": self.issue_ids,
            "confidence": round(self.confidence, 2),
            "is_critical": bool(self.is_critical),
        }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_new(issue: NormalizedIssue, now: datetime) -> bool:
    fs = _parse_iso(issue["first_seen"])
    return bool(fs and now - fs <= timedelta(hours=24))


def _is_release_correlated(
    issue: NormalizedIssue, recent_releases: list[dict], window_minutes: int = 30
) -> bool:
    fs = _parse_iso(issue["first_seen"])
    if not fs:
        return False
    for rel in recent_releases:
        rel_dt = _parse_iso(rel.get("dateCreated", "") or rel.get("date_created", ""))
        if rel_dt and 0 <= (fs - rel_dt).total_seconds() <= window_minutes * 60:
            return True
    return False


def _is_spike(issue: NormalizedIssue, baseline: dict[str, Any], multiplier: float) -> bool:
    """2026-04-28 verdict-delta: spike measured against count_24h, not lifetime.

    Old logic flagged a 10000-event lifetime issue as a spike even when it
    had zero new events in the last day. We now compare 24h-window events
    to the rolling baseline; festering issues with low 24h activity stay
    quiet.
    """
    sig = issue.get("stack_signature", "")
    base = baseline.get(sig, {}).get("avg_daily", 0.0)
    c24 = issue.get("count_24h")
    # When stats fetch failed/skipped, count_24h is None → no spike signal.
    if c24 is None:
        return False
    if base <= 0:
        # No baseline → only treat as spike if 24h count is unusually high
        # AND the signature is genuinely new to us.
        return c24 >= 20 and _is_new_signature(issue, baseline)
    return c24 > base * multiplier


def _is_new_signature(issue: NormalizedIssue, baseline: dict[str, Any]) -> bool:
    return issue.get("stack_signature", "") not in baseline


def _level_weight(level: str, weights: dict[str, int]) -> int:
    return {
        "fatal": weights["level_fatal"],
        "error": weights["level_error"],
        "warning": weights["level_warning"],
    }.get(level, 0)


def score_issue(
    issue: NormalizedIssue,
    *,
    baseline: dict[str, Any],
    recent_releases: list[dict],
    rules: RulesConfig,
    cluster_member_ids: set[str] | None = None,
    now: datetime | None = None,
) -> ScoredIssue:
    now = now or _now_utc()
    cluster_member_ids = cluster_member_ids or set()
    weights = rules.scoring.weights.model_dump()
    thresholds = rules.scoring.thresholds

    is_new = _is_new(issue, now)
    is_regression = bool(issue.get("is_regression"))
    is_spike = _is_spike(issue, baseline, thresholds.spike_baseline_multiplier)
    is_release_correlated = _is_release_correlated(issue, recent_releases)
    is_user_impacting = issue["user_count"] >= thresholds.user_impacting_min_users
    in_cluster = issue["issue_id"] in cluster_member_ids

    score = 0
    reasons: list[str] = []
    score += _level_weight(issue["level"], weights)
    if issue["level"] in ("fatal", "error", "warning"):
        reasons.append(f"level:{issue['level']}")
    if is_new:
        score += weights["is_new"]
        reasons.append("new")
    if is_regression:
        score += weights["is_regression"]
        reasons.append("regression")
    if is_spike:
        score += weights["is_spike"]
        reasons.append("spike")
    if is_release_correlated:
        score += weights["is_release_correlated"]
        reasons.append("release_correlated")
    if is_user_impacting:
        score += weights["is_user_impacting"]
        reasons.append(f"user_impact:{issue['user_count']}")
    if in_cluster:
        score += weights["cross_project_member"]
        reasons.append("cross_project")

    # 2026-04-28 verdict-delta: festering bonus.
    # Award a small fixed bonus (capped via weights["cumulative_festering_bonus_max"])
    # to issues with a high lifetime count, so they don't disappear from the
    # severity ranking entirely. Cap is set deliberately low (10) so this can
    # NEVER alone push severity ≥ 50 (the NEEDS_ATTENTION threshold).
    festering_min = getattr(thresholds, "festering_bonus_min_lifetime", 1000)
    festering_max = weights.get("cumulative_festering_bonus_max", 10)
    if issue["count"] >= festering_min:
        score += festering_max
        reasons.append(f"festering:{issue['count']}_lifetime")

    score = max(0, min(score, 100))

    return ScoredIssue(
        issue_id=issue["issue_id"],
        project_slug=issue["project_slug"],
        title=issue["title"],
        level=issue["level"],
        count=issue["count"],
        count_24h=issue.get("count_24h"),
        user_count=issue["user_count"],
        first_seen=issue["first_seen"],
        last_seen=issue["last_seen"],
        release=issue.get("release"),
        permalink=issue.get("permalink"),
        is_new=is_new,
        is_regression=is_regression,
        is_spike=is_spike,
        is_release_correlated=is_release_correlated,
        is_user_impacting=is_user_impacting,
        in_cluster=in_cluster,
        severity_score=score,
        reasons=reasons,
    )


def score_all(
    issues: list[NormalizedIssue],
    *,
    baseline: dict[str, Any],
    recent_releases: list[dict],
    rules: RulesConfig,
    clusters: list[Cluster] | None = None,
    now: datetime | None = None,
) -> list[ScoredIssue]:
    cluster_ids: set[str] = set()
    for c in clusters or []:
        cluster_ids.update(c.members)
    return [
        score_issue(
            i,
            baseline=baseline,
            recent_releases=recent_releases,
            rules=rules,
            cluster_member_ids=cluster_ids,
            now=now,
        )
        for i in issues
    ]


def detect_decision_required(
    scored: list[ScoredIssue], clusters: list[Cluster]
) -> list[Decision]:
    """Surface action-worthy patterns: rollback hints, hotspot endpoints."""
    decisions: list[Decision] = []

    # 1. Release-correlated burst: if 3+ new errors share a release tag, suggest rollback review.
    by_release: dict[str, list[ScoredIssue]] = {}
    for s in scored:
        if s["is_new"] and s["is_release_correlated"] and s.get("release"):
            by_release.setdefault(s["release"], []).append(s)
    for release, items in by_release.items():
        if len(items) >= 3:
            decisions.append(
                Decision(
                    kind="release_rollback_review",
                    summary=f"Release {release} correlates with {len(items)} new errors — consider rollback review.",
                    issue_ids=[i["issue_id"] for i in items],
                    confidence=min(1.0, 0.5 + 0.1 * len(items)),
                )
            )

    # 2. Hotspot endpoint: any single cluster carrying >= 2 high-severity issues.
    for c in clusters:
        sev_in_cluster = [s for s in scored if s["issue_id"] in c.members and s["severity_score"] >= 50]
        if len(sev_in_cluster) >= 2:
            decisions.append(
                Decision(
                    kind="hotspot_endpoint",
                    summary=(
                        f"Endpoint {c.shared_route or 'unknown'} has cross-project failures "
                        f"({len(sev_in_cluster)} high-severity issues) — investigate."
                    ),
                    issue_ids=c.members,
                    confidence=c.confidence,
                )
            )

    return decisions
