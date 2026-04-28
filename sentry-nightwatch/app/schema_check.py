"""Phase 1.5 — live Sentry schema validation.

Strict HTTP budget: at most 6 calls per run. All responses pass through
`redactor.redact()` before any data hits disk or the report.

Usage:
  python -m app.schema_check [--report PATH] [--sample-out PATH]

Exit codes:
  0 — completed (report written; check verdict inside)
  1 — generic error
  2 — rate-limited (HTTP 429 from Sentry)
  3 — unauthorized (HTTP 401 — check token scopes)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import httpx

from app.config import load_config
from app.normalizer import normalize_issue
from app.redactor import redact

# Hard call budget — we're on the free plan and this script must stay frugal.
MAX_HTTP_CALLS = 6
TIMEOUT_S = 30.0


# ──────────────────────────────────────────────────────────────────────────
# Field expectations
# ──────────────────────────────────────────────────────────────────────────

# Fields normalize_issue() reads — all must be present (most can be empty).
REQUIRED_ISSUE_FIELDS = [
    "id",
    "title",
    "level",
    "count",
    "userCount",
    "firstSeen",
    "lastSeen",
    "status",
]
# Fields the normalizer reads but tolerates missing (None / default).
# Note: `tags` and `isRegression` are NOT listed here — the live list endpoint
# omits them by design, and the pipeline now sources release/environment from
# the per-event payload (Phase 1.5 patch). Their absence is expected.
OPTIONAL_ISSUE_FIELDS = [
    "shortId",
    "platform",
    "culprit",
    "permalink",
    "project",
    "metadata",
]

# Fields the builder writes evidence for; the event itself is opaque to the
# pipeline (passes through redactor only), so we just verify it's a dict.
REQUIRED_EVENT_FIELDS = ["eventID"]


# ──────────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────────


class RateLimited(RuntimeError):
    pass


class Unauthorized(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────
# HTTP helper with budget tracking
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class HttpLog:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, method: str, url: str, status: int, ms: int) -> None:
        self.calls.append(
            {"method": method, "url": _strip_token(url), "status": status, "ms": ms}
        )

    def count(self) -> int:
        return len(self.calls)


def _strip_token(url: str) -> str:
    # URL itself never contains the token, but be defensive.
    return url


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SENTRY_AUTH_TOKEN not set in environment")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _base_url() -> str:
    return os.environ.get("SENTRY_BASE_URL", "https://sentry.io").rstrip("/")


def _get(client: httpx.Client, url: str, params: dict | None, log: HttpLog) -> httpx.Response:
    if log.count() >= MAX_HTTP_CALLS:
        raise RuntimeError(f"HTTP budget exceeded ({MAX_HTTP_CALLS} calls) — refusing to call {url}")
    started = datetime.now(timezone.utc)
    resp = client.get(url, params=params, headers=_auth_headers(), timeout=TIMEOUT_S)
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    log.record("GET", str(resp.url), resp.status_code, elapsed_ms)
    if resp.status_code == 401:
        raise Unauthorized("Sentry returned 401 — check token scopes (need org:read, project:read, event:read)")
    if resp.status_code == 429:
        raise RateLimited("Sentry returned 429 — rate-limited; aborting before further damage")
    return resp


# ──────────────────────────────────────────────────────────────────────────
# Schema diff helpers
# ──────────────────────────────────────────────────────────────────────────


def _live_top_keys(obj: Any) -> set[str]:
    return set(obj.keys()) if isinstance(obj, dict) else set()


def _fixture_top_keys(path: Path) -> set[str]:
    """Take the union of top-level keys across all fixture issues."""
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for slug, lst in data.items():
        if slug.startswith("_"):
            continue
        for issue in lst:
            keys.update(issue.keys())
    return keys


def _check_issue_normalizable(raw_issue: dict, slug: str) -> dict[str, Any]:
    """Try normalize_issue and capture any missing required fields."""
    missing_required = [k for k in REQUIRED_ISSUE_FIELDS if k not in raw_issue]
    missing_optional = [k for k in OPTIONAL_ISSUE_FIELDS if k not in raw_issue]
    type_mismatches: list[str] = []
    for k in ["count", "userCount"]:
        v = raw_issue.get(k)
        # Sentry may serialize counts as strings ("412"); normalizer handles both.
        if v is not None and not isinstance(v, (int, str)):
            type_mismatches.append(f"{k}: expected int|str, got {type(v).__name__}")
    try:
        n = normalize_issue(raw_issue, slug)
        normalize_ok = True
        normalize_error = None
    except Exception as exc:  # noqa: BLE001 - intentional broad capture for diagnostic
        n = None
        normalize_ok = False
        normalize_error = f"{type(exc).__name__}: {exc}"
    return {
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "type_mismatches": type_mismatches,
        "normalize_ok": normalize_ok,
        "normalize_error": normalize_error,
        "normalized_sample": n if normalize_ok else None,
    }


def _check_event_basic(raw_event: dict | None) -> dict[str, Any]:
    if raw_event is None:
        return {"available": False, "missing": [], "note": "no event fetched"}
    missing = [k for k in REQUIRED_EVENT_FIELDS if k not in raw_event]
    has_exception = "exception" in raw_event and bool(raw_event["exception"])
    has_request = "request" in raw_event and bool(raw_event["request"])
    return {
        "available": True,
        "missing": missing,
        "has_exception": has_exception,
        "has_request": has_request,
        "top_keys": sorted(raw_event.keys()),
    }


# ──────────────────────────────────────────────────────────────────────────
# Verdict
# ──────────────────────────────────────────────────────────────────────────


def _verdict(
    issue_checks: list[dict],
    event_check: dict,
    no_issues_anywhere: bool,
    event_fetch_status: int | None,
) -> tuple[str, str]:
    # RED #1 — event fetch through the path collector.py uses returns non-200.
    if event_fetch_status is not None and event_fetch_status != 200 and not no_issues_anywhere:
        return (
            "RED",
            f"Event fetch (collector.py path) returned HTTP {event_fetch_status}; evidence/ would be empty in production.",
        )

    if no_issues_anywhere:
        return ("YELLOW", "No issues exist in either project; could not validate event schema.")

    for c in issue_checks:
        if c["missing_required"] or not c["normalize_ok"]:
            return ("RED", "Live issue is missing a required field or fails to normalize.")
    if event_check.get("missing"):
        return ("RED", "Live event is missing a required top-level field.")

    for c in issue_checks:
        if c["missing_optional"] or c["type_mismatches"]:
            return ("YELLOW", "Live response has minor optional/type differences; normalizer handles them.")
    # New top-level fields present in live but unknown to the fixture are
    # informational only — the normalizer ignores them. Surface in the report
    # but do NOT trigger YELLOW for benign additions.

    return ("GREEN", "Live schema matches fixture in every field the pipeline reads.")


# ──────────────────────────────────────────────────────────────────────────
# Report rendering
# ──────────────────────────────────────────────────────────────────────────


def _render_report(
    *,
    org: str,
    project_slugs: list[str],
    log: HttpLog,
    issue_checks: list[tuple[str, dict]],
    event_check: dict,
    new_top_fields: set[str],
    fixture_top_keys: set[str],
    live_top_keys: set[str],
    verdict: str,
    justification: str,
    redacted_sample_excerpt: str,
    event_fetch_status: int | None,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append("# Phase 1.5 — Schema Validation Report")
    lines.append("")
    lines.append(f"- **Date (UTC):** {now}")
    lines.append(f"- **Sentry org:** `{org}`")
    lines.append(f"- **Projects checked:** {', '.join('`' + s + '`' for s in project_slugs)}")
    lines.append("")
    lines.append("## API calls made")
    lines.append("")
    lines.append(f"Total: **{log.count()} / {MAX_HTTP_CALLS}** budget.")
    lines.append("")
    lines.append("| # | Method | URL | Status | ms |")
    lines.append("|---|--------|-----|-------:|---:|")
    for i, c in enumerate(log.calls, 1):
        lines.append(f"| {i} | {c['method']} | `{c['url']}` | {c['status']} | {c['ms']} |")
    lines.append("")

    # ── Schema diff ──────────────────────────────────────────────────────
    lines.append("## Schema diff")
    lines.append("")
    lines.append("### Fields in live response but NOT in fixture (top-level, issue list)")
    lines.append("")
    new_in_live = sorted(live_top_keys - fixture_top_keys)
    if new_in_live:
        lines.append("| field path | sample type | impact |")
        lines.append("|------------|-------------|--------|")
        for f in new_in_live:
            lines.append(f"| `{f}` | (live-only) | ignored — normalizer doesn't read it |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("### Fields expected by normalizer but MISSING in live response")
    lines.append("")
    rows: list[str] = []
    for slug, c in issue_checks:
        for field_name in c["missing_required"]:
            rows.append(f"| `{field_name}` | normalizer.normalize_issue ({slug}) | **HIGH — required** |")
        for field_name in c["missing_optional"]:
            rows.append(f"| `{field_name}` | normalizer.normalize_issue ({slug}) | LOW — optional, default applied |")
    if rows:
        lines.append("| field | which module reads it | severity |")
        lines.append("|-------|----------------------|----------|")
        lines.extend(rows)
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("### Type mismatches")
    lines.append("")
    type_rows: list[str] = []
    for slug, c in issue_checks:
        for tm in c["type_mismatches"]:
            type_rows.append(f"| `{slug}` | {tm} |")
    if type_rows:
        lines.append("| project | mismatch |")
        lines.append("|---------|----------|")
        lines.extend(type_rows)
    else:
        lines.append("_None._")
    lines.append("")

    # ── Event endpoint URL probe ────────────────────────────────────────
    lines.append("## Event endpoint probe")
    lines.append("")
    lines.append("| path (matches `collector.fetch_event_full`) | status |")
    lines.append("|---------------------------------------------|-------:|")
    lines.append(
        f"| `/api/0/organizations/{{org}}/issues/{{id}}/events/latest/` | "
        f"**{event_fetch_status if event_fetch_status is not None else 'n/a'}** |"
    )
    lines.append("")

    # ── Event schema check ──────────────────────────────────────────────
    lines.append("## Event schema check")
    lines.append("")
    if event_check.get("available"):
        lines.append(f"- Top-level keys: {', '.join('`' + k + '`' for k in event_check['top_keys'])}")
        lines.append(f"- Has `exception` field: **{event_check['has_exception']}**")
        lines.append(f"- Has `request` field: **{event_check['has_request']}**")
        if event_check.get("missing"):
            lines.append(f"- **Missing required:** {event_check['missing']}")
        else:
            lines.append("- All required event fields present.")
    else:
        lines.append(f"- Skipped: {event_check.get('note', 'no event')}")
    lines.append("")

    # ── Verdict ──────────────────────────────────────────────────────────
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**{verdict}** — {justification}")
    lines.append("")
    lines.append("Verdict legend:")
    lines.append("- **GREEN** — schema matches, proceed to Phase 2 unchanged.")
    lines.append("- **YELLOW** — minor differences; normalizer handles gracefully (no code change).")
    lines.append("- **RED** — live schema breaks the pipeline; code change required.")
    lines.append("")

    if verdict == "RED":
        lines.append("## RED verdict — investigate")
        lines.append("")
        lines.append(
            f"Event fetch returned HTTP {event_fetch_status}. The pipeline would "
            "ship empty `evidence/` directories until this is resolved. "
            "Inspect the URL, project visibility, and the token's `event:read` scope."
        )
        lines.append("")

    lines.append("## Sample of redacted live data (first 30 lines)")
    lines.append("")
    lines.append("```json")
    lines.append(redacted_sample_excerpt)
    lines.append("```")
    lines.append("")
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────────
# Main entrypoint
# ──────────────────────────────────────────────────────────────────────────


def run_check(report_path: Path, sample_path: Path) -> int:
    cfg = load_config()
    org = cfg.env.sentry_org_slug or cfg.projects.organization
    project_slugs = [p.slug for p in cfg.projects.projects]

    log = HttpLog()
    issue_checks: list[tuple[str, dict]] = []
    live_top_keys: set[str] = set()
    redacted_first_issue: dict | None = None
    redacted_first_event: dict | None = None
    no_issues_anywhere = True
    event_check: dict[str, Any] = {"available": False, "note": "no issues fetched"}
    event_fetch_status: int | None = None

    base = _base_url()

    try:
        with httpx.Client() as client:
            # Call 1: list org projects (sanity check that the org is reachable).
            url = f"{base}/api/0/organizations/{org}/projects/"
            resp = _get(client, url, None, log)
            resp.raise_for_status()

            # Calls 2-3: per-project issues (limit=5). Tolerate 4xx/5xx — log and skip.
            project_issues: dict[str, list[dict]] = {}
            for slug in project_slugs:
                url = f"{base}/api/0/projects/{org}/{slug}/issues/"
                resp = _get(client, url, {"limit": 5, "statsPeriod": "24h"}, log)
                if resp.status_code == 200:
                    lst = resp.json()
                    project_issues[slug] = lst
                    for raw_issue in lst:
                        live_top_keys.update(raw_issue.keys())
                else:
                    project_issues[slug] = []

            # Call 4: fetch event for the FIRST issue of the FIRST project that
            # has issues, using the SAME URL collector.py uses (canonical org-scoped
            # path post-Phase-1.5-patch). One probe, frugal.
            event_fetch_status: int | None = None
            event: dict | None = None
            for _slug, lst in project_issues.items():
                if not lst:
                    continue
                no_issues_anywhere = False
                issue_id = str(lst[0].get("id", ""))
                if not issue_id:
                    continue
                event_url = (
                    f"{base}/api/0/organizations/{org}/issues/{issue_id}/events/latest/"
                )
                if log.count() < MAX_HTTP_CALLS:
                    r = _get(client, event_url, {"full": "true"}, log)
                    event_fetch_status = r.status_code
                    if r.status_code == 200:
                        event = r.json()
                break

            if event is not None:
                event_check = _check_event_basic(event)
                redacted_first_event = redact(event, mode="snapshot")
            else:
                event_check = {
                    "available": False,
                    "note": f"event fetch returned HTTP {event_fetch_status}",
                }

            # Call 6: org-level releases (frugal: per_page=5, only if budget allows).
            if log.count() < MAX_HTTP_CALLS:
                rel_url = f"{base}/api/0/organizations/{org}/releases/"
                _get(client, rel_url, {"per_page": 5}, log)
                # We don't read the body — just confirming reachability + budget compliance.

            # Schema check on each issue we got.
            for slug, lst in project_issues.items():
                if not lst:
                    continue
                check = _check_issue_normalizable(lst[0], slug)
                issue_checks.append((slug, check))
                if redacted_first_issue is None:
                    redacted_first_issue = redact(lst[0], mode="snapshot")

    except RateLimited as exc:
        click.echo(f"ABORTED: {exc}", err=True)
        return 2
    except Unauthorized as exc:
        click.echo(f"ABORTED: {exc}", err=True)
        return 3

    # Build redacted sample fixture.
    sample_payload = {
        "_note": "Redacted live sample captured during Phase 1.5 schema validation.",
        "_captured_at": datetime.now(timezone.utc).isoformat(),
        "_org": org,
        "issue": redacted_first_issue,
        "event": redacted_first_event,
    }
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(json.dumps(sample_payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    # Verdict.
    fixture_top_keys = _fixture_top_keys(cfg.fixtures_dir / "sentry_issues_sample.json")
    new_top_fields = live_top_keys - fixture_top_keys
    verdict_label, justification = _verdict(
        [c for _, c in issue_checks],
        event_check,
        no_issues_anywhere,
        event_fetch_status,
    )

    # Excerpt for the report — first 30 lines of the redacted sample JSON.
    excerpt_obj = redacted_first_issue or redacted_first_event or {}
    excerpt_lines = json.dumps(excerpt_obj, indent=2, ensure_ascii=False, default=str).splitlines()
    excerpt = "\n".join(excerpt_lines[:30])

    report = _render_report(
        org=org,
        project_slugs=project_slugs,
        log=log,
        issue_checks=issue_checks,
        event_check=event_check,
        new_top_fields=new_top_fields,
        fixture_top_keys=fixture_top_keys,
        live_top_keys=live_top_keys,
        verdict=verdict_label,
        justification=justification,
        redacted_sample_excerpt=excerpt,
        event_fetch_status=event_fetch_status,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    click.echo(f"verdict={verdict_label} calls={log.count()}/{MAX_HTTP_CALLS}")
    click.echo(f"report={report_path}")
    click.echo(f"sample={sample_path}")
    return 0


@click.command()
@click.option(
    "--report",
    "report_path",
    default="docs/phase-1.5-schema-report.md",
    help="Where to write the validation report.",
)
@click.option(
    "--sample-out",
    "sample_path",
    default="tests/fixtures/sentry_live_redacted_sample.json",
    help="Where to write the redacted live sample fixture.",
)
def cli(report_path: str, sample_path: str) -> None:
    rc = run_check(Path(report_path), Path(sample_path))
    sys.exit(rc)


if __name__ == "__main__":
    cli()
