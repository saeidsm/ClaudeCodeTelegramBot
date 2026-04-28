"""Normalize Sentry issues into our internal schema and detect cross-project clusters.

Pure functions, no I/O. The output is a stable shape downstream modules can rely on.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

import structlog

log = structlog.get_logger(__name__)


class NormalizedIssue(TypedDict):
    issue_id: str
    short_id: str | None
    project_slug: str
    title: str
    level: str
    count: int
    user_count: int
    first_seen: str  # ISO 8601
    last_seen: str
    status: str
    platform: str | None
    culprit: str | None
    release: str | None
    environment: str | None
    top_tags: dict[str, str]
    stack_signature: str
    permalink: str | None
    is_regression: bool
    raw_url_tag: str | None


@dataclass
class Cluster:
    """A group of issues that look related across projects."""

    members: list[str] = field(default_factory=list)  # issue_ids
    confidence: float = 0.0
    reason: str = ""
    shared_release: str | None = None
    shared_route: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": self.members,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "shared_release": self.shared_release,
            "shared_route": self.shared_route,
        }


# ──────────────────────────────────────────────────────────────────────────


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _tag_value(raw_issue: dict, key: str) -> str | None:
    for tag in raw_issue.get("tags", []) or []:
        if isinstance(tag, dict) and tag.get("key") == key:
            return tag.get("value")
    return None


def _stack_signature(raw_issue: dict) -> str:
    """sha256 of (title + culprit + top frames) — stable per fingerprint."""
    parts = [raw_issue.get("title", ""), raw_issue.get("culprit", "")]
    # Sentry list endpoint doesn't usually include frames; fall back to type+value.
    md = raw_issue.get("metadata", {}) or {}
    parts.extend([md.get("type", ""), md.get("value", "")])
    blob = "||".join(p or "" for p in parts)
    return hashlib.sha256(blob.encode("utf-8", errors="ignore")).hexdigest()[:16]


def extract_event_tags(event: dict | None) -> dict[str, str]:
    """Pull tags from a Sentry event payload into a flat key→value dict.

    Falls back to top-level `release`/`environment` fields if not present in
    the tags array. Returns {} if event is None or empty.
    """
    if not event:
        return {}
    out: dict[str, str] = {}
    for tag in event.get("tags", []) or []:
        if isinstance(tag, dict):
            k = tag.get("key")
            v = tag.get("value")
            if isinstance(k, str) and v is not None:
                out[k] = str(v)
    for k in ("release", "environment"):
        if k not in out:
            v = event.get(k)
            if isinstance(v, str) and v:
                out[k] = v
    return out


def normalize_issue(
    raw_issue: dict, project_slug: str, *, event: dict | None = None
) -> NormalizedIssue:
    """Convert a raw Sentry issue (and optional matching event) to NormalizedIssue.

    `release` and `environment` are sourced from the event tags when an event
    is provided; the issue payload is fallback only. The Sentry list endpoint
    omits tags by default, so without an event these fields are usually None.
    """
    proj = raw_issue.get("project") or {}
    slug = project_slug or proj.get("slug") or "unknown"
    top_tags = {t.get("key"): t.get("value") for t in raw_issue.get("tags", []) or [] if isinstance(t, dict)}

    issue_release = top_tags.get("release") or _tag_value(raw_issue, "release")
    issue_env = top_tags.get("environment") or _tag_value(raw_issue, "environment")

    final_release = issue_release
    final_env = issue_env
    if event is not None:
        ev_tags = extract_event_tags(event)
        ev_release = ev_tags.get("release")
        ev_env = ev_tags.get("environment")
        if ev_release and issue_release and ev_release != issue_release:
            log.warning(
                "normalizer.release_conflict",
                issue_id=str(raw_issue.get("id", "")),
                issue_release=issue_release,
                event_release=ev_release,
                resolution="event_wins",
            )
        final_release = ev_release or issue_release
        if ev_env and issue_env and ev_env != issue_env:
            log.warning(
                "normalizer.environment_conflict",
                issue_id=str(raw_issue.get("id", "")),
                issue_environment=issue_env,
                event_environment=ev_env,
                resolution="event_wins",
            )
        final_env = ev_env or issue_env

    return NormalizedIssue(
        issue_id=str(raw_issue.get("id", "")),
        short_id=raw_issue.get("shortId"),
        project_slug=slug,
        title=raw_issue.get("title", "") or "",
        level=raw_issue.get("level", "error") or "error",
        count=_to_int(raw_issue.get("count")),
        user_count=_to_int(raw_issue.get("userCount")),
        first_seen=raw_issue.get("firstSeen", "") or "",
        last_seen=raw_issue.get("lastSeen", "") or "",
        status=raw_issue.get("status", "unresolved") or "unresolved",
        platform=raw_issue.get("platform"),
        culprit=raw_issue.get("culprit"),
        release=final_release,
        environment=final_env,
        top_tags=top_tags,
        stack_signature=_stack_signature(raw_issue),
        permalink=raw_issue.get("permalink"),
        is_regression=bool(raw_issue.get("isRegression")),
        raw_url_tag=top_tags.get("url"),
    )


# ──────────────────────────────────────────────────────────────────────────
# Cross-project clustering
# ──────────────────────────────────────────────────────────────────────────

_ROUTE_RE = re.compile(r"/api/[\w/\-_:.]+|/[a-z]+/\[[a-z]+\]")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _route_key(issue: NormalizedIssue) -> str | None:
    candidates = []
    if issue.get("raw_url_tag"):
        candidates.append(issue["raw_url_tag"])
    if issue.get("title"):
        candidates.append(issue["title"])
    if issue.get("culprit"):
        candidates.append(issue["culprit"])
    for text in candidates:
        m = _ROUTE_RE.search(text or "")
        if m:
            # Normalize: strip trailing ids, lowercase
            return m.group(0).lower().rstrip("/")
    return None


def cluster_cross_project(
    issues: list[NormalizedIssue], window_minutes: int = 5
) -> list[Cluster]:
    """Group backend + frontend issues that share release, route, or near-time co-occurrence."""
    backends = [i for i in issues if i["project_slug"].endswith("backend")]
    frontends = [i for i in issues if i["project_slug"].endswith("frontend")]
    clusters: list[Cluster] = []
    seen_pairs: set[tuple[str, str]] = set()

    for b in backends:
        b_first = _parse_iso(b["first_seen"])
        b_route = _route_key(b)
        for f in frontends:
            pair = (b["issue_id"], f["issue_id"])
            if pair in seen_pairs:
                continue
            f_first = _parse_iso(f["first_seen"])
            f_route = _route_key(f)
            confidence = 0.0
            reasons: list[str] = []

            if b_first and f_first and abs((b_first - f_first).total_seconds()) <= window_minutes * 60:
                confidence += 0.4
                reasons.append("time_overlap")

            if b["release"] and f["release"]:
                # Both have releases (different version namespaces are expected) — partial signal.
                confidence += 0.1

            if b_route and f_route and (b_route in f_route or f_route in b_route):
                confidence += 0.5
                reasons.append("shared_route")

            if confidence >= 0.5:
                seen_pairs.add(pair)
                clusters.append(
                    Cluster(
                        members=[b["issue_id"], f["issue_id"]],
                        confidence=min(confidence, 1.0),
                        reason="+".join(reasons) or "weak",
                        shared_release=None,
                        shared_route=b_route or f_route,
                    )
                )
    return clusters


def derive_recent_window(now: datetime, hours: int = 24) -> tuple[datetime, datetime]:
    """Inclusive [start, end) window, UTC."""
    return now - timedelta(hours=hours), now
