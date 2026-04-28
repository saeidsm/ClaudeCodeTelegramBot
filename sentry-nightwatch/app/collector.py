"""Sentry REST API client.

Pure HTTP layer: no business logic, no redaction (caller MUST redact before disk).
Token is read from SENTRY_AUTH_TOKEN env var; never logged.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 30.0
MAX_REQ_PER_MIN = 30
MAX_RETRIES = 5

_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="(\w+)"')


class SentryUnreachable(RuntimeError):
    """Raised when Sentry is unreachable after retries."""


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("SENTRY_AUTH_TOKEN", "")
    if not token:
        raise SentryUnreachable("SENTRY_AUTH_TOKEN is not set")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _base_url() -> str:
    return os.environ.get("SENTRY_BASE_URL", "https://sentry.io").rstrip("/")


def _parse_link_header(link: str) -> dict[str, dict[str, str]]:
    """Parse Sentry's RFC 5988 Link header into {rel: {url, results, cursor}}."""
    out: dict[str, dict[str, str]] = {}
    for m in _LINK_RE.finditer(link or ""):
        url, rel = m.group(1), m.group(2)
        out[rel] = {"url": url}
    return out


class _RateBucket:
    """Trivial token bucket: at most MAX_REQ_PER_MIN within any rolling 60s window."""

    def __init__(self, max_per_min: int = MAX_REQ_PER_MIN) -> None:
        self.max = max_per_min
        self._stamps: list[float] = []

    def acquire(self) -> None:
        now = time.monotonic()
        self._stamps = [t for t in self._stamps if now - t < 60.0]
        if len(self._stamps) >= self.max:
            sleep_for = 60.0 - (now - self._stamps[0]) + 0.01
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            self._stamps = [t for t in self._stamps if now - t < 60.0]
        self._stamps.append(now)


_BUCKET = _RateBucket()


def _request(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    """GET with rate-limit + exponential backoff on 429/5xx. Never logs the token."""
    backoff = 1.0
    for attempt in range(MAX_RETRIES):
        _BUCKET.acquire()
        try:
            resp = client.get(url, params=params, headers=_auth_headers(), timeout=DEFAULT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            log.warning("collector.http_error", attempt=attempt, error=type(exc).__name__)
            if attempt + 1 == MAX_RETRIES:
                raise SentryUnreachable(f"request failed: {type(exc).__name__}") from exc
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            log.warning("collector.rate_limited", retry_after=retry_after, attempt=attempt)
            time.sleep(min(retry_after, 30.0))
            continue

        if 500 <= resp.status_code < 600:
            log.warning("collector.server_error", status=resp.status_code, attempt=attempt)
            if attempt + 1 == MAX_RETRIES:
                raise SentryUnreachable(f"server {resp.status_code}")
            time.sleep(backoff)
            backoff *= 2
            continue

        return resp

    raise SentryUnreachable("max retries exceeded")


def fetch_issues(
    project_slug: str,
    *,
    org: str | None = None,
    hours_back: int = 24,
    limit: int = 100,
    statsPeriod: str | None = None,
) -> list[dict[str, Any]]:
    """List issues for a project, following pagination cursors."""
    org = org or os.environ.get("SENTRY_ORG_SLUG", "")
    if not org:
        raise SentryUnreachable("SENTRY_ORG_SLUG is not set")

    period = statsPeriod or f"{hours_back}h"
    url = f"{_base_url()}/api/0/projects/{org}/{project_slug}/issues/"
    params: dict[str, Any] = {"statsPeriod": period, "limit": limit}

    issues: list[dict[str, Any]] = []
    seen_pages = 0
    with httpx.Client() as client:
        while True:
            resp = _request(client, url, params=params)
            if resp.status_code == 404:
                log.warning("collector.project_404", slug=project_slug)
                break
            resp.raise_for_status()
            page = resp.json()
            issues.extend(page)
            seen_pages += 1
            link = _parse_link_header(resp.headers.get("Link", ""))
            nxt = link.get("next")
            if not nxt or "results=true" in resp.headers.get("Link", "").lower() is False:
                break
            # Sentry signals "no more results" via results=false; we only follow if results=true.
            if "results=\"true\"" not in resp.headers.get("Link", ""):
                break
            url = nxt["url"]
            params = None
            if seen_pages >= 20:
                log.warning("collector.pagination_capped", pages=seen_pages, slug=project_slug)
                break
    log.info("collector.fetched_issues", slug=project_slug, count=len(issues), pages=seen_pages)
    return issues


def fetch_event_full(
    org: str, project_slug: str, issue_id: str, event_id: str = "latest"
) -> dict[str, Any] | None:
    """Fetch a single event with full payload. Returns None on 404.

    `project_slug` is currently unused (org-scoped path); kept for API stability
    so existing callers compile unchanged. Phase 1.5 schema validation found
    the project-scoped path 404s while the org-scoped one returns 200.
    """
    url = (
        f"{_base_url()}/api/0/organizations/{org}/issues/{issue_id}/events/{event_id}/"
    )
    with httpx.Client() as client:
        resp = _request(client, url, params={"full": "true"})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


def fetch_releases(org: str, project_slug: str, days_back: int = 7) -> list[dict[str, Any]]:
    """List recent releases for deploy correlation."""
    url = f"{_base_url()}/api/0/projects/{org}/{project_slug}/releases/"
    with httpx.Client() as client:
        resp = _request(client, url, params={"per_page": 25})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json()
