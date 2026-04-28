"""CLI entrypoint for NightWatch.

Usage:
  python -m app.main run [--dry-run] [--date YYYY-MM-DD] [--verbose]
  python -m app.main republish [--date YYYY-MM-DD] [--verbose]
  python -m app.main validate-redactor
  python -m app.main prune [--days N]

Exit codes:
  0 — success
  1 — generic error
  2 — Sentry unreachable (a stub snapshot is still written)
  3 — redactor self-test failed OR publisher (digest delivery) failed
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click
import structlog
from dateutil import tz

from app import collector, redactor
from app.analyzer import detect_decision_required, score_all
from app.builder import (
    build_snapshot,
    prune_old_snapshots,
    update_baseline,
    write_stub_snapshot,
)
from app.config import load_config
from app.normalizer import cluster_cross_project, normalize_issue

# ──────────────────────────────────────────────────────────────────────────
# Phase 2B-fix: zero-dependency .env loader.
#
# Subprocess-spawned runs (bot's /nightwatch_run, future systemd timers, ad-hoc
# `python -m app.main` invocations) inherit only the parent's env. Without this
# loader the parent has to pre-source the .env file, which is easy to forget —
# and silently degrades the pipeline to `sentry_unreachable` on every tick.
# ──────────────────────────────────────────────────────────────────────────


def _load_env_file(path: Path) -> int:
    """Load KEY=VALUE pairs from `path` into os.environ.

    - Skips blank lines and `#` comments.
    - Splits on the first `=` only (values may contain `=`).
    - Strips matching surrounding single/double quotes.
    - Uses `setdefault` semantics: existing env wins, so systemd's
      `EnvironmentFile=` (or a manually-sourced shell) overrides this file.
    Returns the number of new keys actually applied.
    """
    if not path.exists():
        return 0
    applied = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v
            applied += 1
    return applied


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _resolve_date(date_str: str | None, tzname: str) -> str:
    if date_str:
        # validate format
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    local = tz.gettz(tzname) or timezone.utc
    yesterday = datetime.now(local) - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


def _load_baseline(snapshots_dir: Path) -> dict:
    p = snapshots_dir / "baseline.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _load_dry_run_data(fixtures_dir: Path) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, dict]]:
    """Returns (issues_by_project, releases_by_project, evidence_by_issue_id)."""
    from app.redactor import expand_test_placeholders

    def _load(name: str):
        return json.loads(
            expand_test_placeholders((fixtures_dir / name).read_text(encoding="utf-8"))
        )

    sample = _load("sentry_issues_sample.json")
    issues_by_project: dict[str, list[dict]] = {}
    for slug, lst in sample.items():
        if slug.startswith("_"):
            continue
        issues_by_project[slug] = lst
    releases = sample.get("_releases", {})

    evidence_by_id: dict[str, dict] = {}
    event_full_path = fixtures_dir / "sentry_event_full.json"
    if event_full_path.exists():
        ev = _load("sentry_event_full.json")
        evidence_by_id[str(ev.get("issueId", ""))] = ev
    return issues_by_project, releases, evidence_by_id


def _collect_live(
    cfg: Any,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, dict]]:
    """Hit Sentry for real; raises SentryUnreachable on failure."""
    org = cfg.env.sentry_org_slug or cfg.projects.organization
    issues_by_project: dict[str, list[dict]] = {}
    releases_by_project: dict[str, list[dict]] = {}
    for proj in cfg.projects.projects:
        issues_by_project[proj.slug] = collector.fetch_issues(proj.slug, org=org)
        releases_by_project[proj.slug] = collector.fetch_releases(org, proj.slug)

    evidence: dict[str, dict] = {}
    fetched = 0
    cap = cfg.rules.rate_limits.sentry_max_event_fetches_per_run
    # Identify top issues by raw count for evidence pull (post-scoring re-pick later if needed).
    flat = []
    for slug, lst in issues_by_project.items():
        for raw in lst:
            flat.append((slug, raw))
    flat.sort(key=lambda t: int(t[1].get("count") or 0), reverse=True)
    for slug, raw in flat:
        if fetched >= cap:
            break
        ev = collector.fetch_event_full(org, slug, str(raw.get("id", "")))
        if ev:
            evidence[str(raw.get("id", ""))] = ev
            fetched += 1
    return issues_by_project, releases_by_project, evidence


# ──────────────────────────────────────────────────────────────────────────


@click.group()
def cli() -> None:
    """NightWatch — offline Sentry analysis pipeline."""
    env_path = Path(
        os.environ.get("NIGHTWATCH_ENV_FILE", "/opt/sentry-nightwatch/.env")
    )
    n = _load_env_file(env_path)
    if n:
        # No structlog here — subcommands set up logging themselves.
        logging.getLogger("nightwatch.env").debug(
            "loaded %d keys from %s", n, env_path
        )


@cli.command("__echo_env", hidden=True)
@click.argument("key")
def __echo_env_cmd(key: str) -> None:
    """[internal/test only] Echo a single env var value, then exit.

    Used by tests/test_subprocess_env.py to verify the .env loader runs at
    cli() entry — invoked as `python -m app.main __echo_env <KEY>`.
    """
    click.echo(os.environ.get(key, ""))


@cli.command()
@click.option("--dry-run", is_flag=True, help="Use tests/fixtures instead of Sentry API.")
@click.option("--date", "date_str", default=None, help="Target date YYYY-MM-DD. Default: yesterday.")
@click.option("--verbose", is_flag=True, help="Verbose logging on stderr.")
def run(dry_run: bool, date_str: str | None, verbose: bool) -> None:
    _configure_logging(verbose)
    log = structlog.get_logger("main")
    cfg = load_config()
    target_date = _resolve_date(date_str, cfg.env.nightwatch_tz)
    log.info("nightwatch.start", date=target_date, dry_run=dry_run)

    try:
        if dry_run:
            issues_by_project, releases_by_project, evidence = _load_dry_run_data(cfg.fixtures_dir)
        else:
            issues_by_project, releases_by_project, evidence = _collect_live(cfg)
    except collector.SentryUnreachable as exc:
        log.error("nightwatch.sentry_unreachable", error=str(exc))
        path = write_stub_snapshot(target_date, cfg.snapshots_dir, reason=str(exc))
        # Phase 2B-fix: send a watchdog digest so silence-after-/nightwatch_run
        # never happens again. Watchdog failure must NOT mask the sentry_unreachable
        # exit code — log and continue to sys.exit(2).
        if not dry_run and os.environ.get(
            "NIGHTWATCH_WATCHDOG_ENABLED", "true"
        ).strip().lower() == "true":
            try:
                from app.publisher import Publisher

                publisher = Publisher()
                wd = publisher.publish_watchdog(path, reason=str(exc))
                if wd.ok:
                    log.info(
                        "publisher.watchdog_delivered",
                        session_label=wd.session_label,
                        delivered=wd.delivered,
                    )
                else:
                    log.warning("publisher.watchdog_failed", error=wd.error)
            except Exception as wd_exc:  # noqa: BLE001 — never propagate
                log.warning("publisher.watchdog_exception", error=str(wd_exc))
        else:
            log.info("publisher.watchdog_skipped",
                     reason="dry_run" if dry_run else "disabled_via_env")
        click.echo(str(path))
        sys.exit(2)

    # Normalize. When an event is available for the issue (top-N pre-fetched),
    # release/environment come from the event tags; otherwise they stay None.
    normalized = []
    for slug, lst in issues_by_project.items():
        for raw in lst:
            issue_id = str(raw.get("id", ""))
            normalized.append(normalize_issue(raw, slug, event=evidence.get(issue_id)))

    clusters = cluster_cross_project(
        normalized, window_minutes=cfg.rules.scoring.thresholds.cluster_time_window_minutes
    )

    # Aggregate releases across projects for correlation.
    all_releases: list[dict] = []
    for lst in releases_by_project.values():
        all_releases.extend(lst)

    baseline = _load_baseline(cfg.snapshots_dir)
    scored = score_all(
        normalized,
        baseline=baseline,
        recent_releases=all_releases,
        rules=cfg.rules,
        clusters=clusters,
    )
    decisions = detect_decision_required(scored, clusters)

    snap_dir = build_snapshot(
        target_date,
        snapshots_dir=cfg.snapshots_dir,
        issues=normalized,
        scored=scored,
        clusters=clusters,
        decisions=decisions,
        raw_evidence=evidence,
        status="ok",
    )
    update_baseline(cfg.snapshots_dir, normalized, target_date)
    log.info("nightwatch.done", path=str(snap_dir))
    click.echo(str(snap_dir))

    # Phase 2B — deliver the digest to the bot's IPC. Skipped on --dry-run.
    if not dry_run:
        from app.publisher import Publisher

        try:
            publisher = Publisher()
        except RuntimeError as exc:
            log.error("publisher.init_failed", error=str(exc))
            sys.exit(3)
        result = publisher.publish(snap_dir, mode="nightly")
        if not result.ok:
            log.error("publisher.failed", error=result.error, status=result.status_code)
            sys.exit(3)
        log.info(
            "publisher.ok",
            session_label=result.session_label,
            delivered=result.delivered,
            duplicates=result.duplicates,
            failed=result.failed,
        )


@cli.command()
@click.option("--date", "date_str", default=None, help="Snapshot date YYYY-MM-DD. Default: most recent.")
@click.option("--verbose", is_flag=True, help="Verbose logging on stderr.")
def republish(date_str: str | None, verbose: bool) -> None:
    """Re-send a previously-built digest to Telegram (mode=manual).

    Used by the bot's `/nightwatch_last` command and for manual recovery.
    The "manual" suffix in the session_label is what bypasses the bot's
    per-(chat, label) dedup so a re-publish actually re-delivers.
    """
    _configure_logging(verbose)
    log = structlog.get_logger("main")
    cfg = load_config()
    snapshots = cfg.snapshots_dir

    if date_str:
        # Validate format and resolve.
        datetime.strptime(date_str, "%Y-%m-%d")
        target = snapshots / date_str
    else:
        last_marker = snapshots / "last-digest.txt"
        if last_marker.exists():
            target = Path(last_marker.read_text(encoding="utf-8").strip())
        else:
            candidates = sorted(
                d for d in snapshots.iterdir()
                if d.is_dir() and len(d.name) == 10 and d.name[:4].isdigit()
            )
            if not candidates:
                click.echo("no snapshots found", err=True)
                sys.exit(1)
            target = candidates[-1]

    if not target.is_dir():
        click.echo(f"snapshot dir does not exist: {target}", err=True)
        sys.exit(1)

    # Note: republish doesn't rebuild the snapshot, but Publisher.publish()
    # bridges into NIGHTWATCH_WEBROOT_DIR after the IPC POST returns 202
    # (Bug 1 fix, 2026-04-28) — so manual recovery for missed bridges
    # works by simply re-running republish.
    from app.publisher import Publisher

    try:
        publisher = Publisher()
    except RuntimeError as exc:
        click.echo(f"publisher init failed: {exc}", err=True)
        sys.exit(3)
    result = publisher.publish(target, mode="manual")
    if not result.ok:
        log.error("publisher.failed", error=result.error, status=result.status_code)
        sys.exit(3)
    click.echo(f"republished {target.name} ({result.delivered} delivered, {result.duplicates} dup, {result.failed} failed)")


@cli.command("validate-redactor")
def validate_redactor_cmd() -> None:
    """Run the redactor self-test against the PII corpus."""
    rc = redactor._self_test()
    sys.exit(0 if rc == 0 else 3)


@cli.command()
@click.option("--days", default=None, type=int, help="Keep last N days. Default: env or 30.")
def prune(days: int | None) -> None:
    cfg = load_config()
    keep = days if days is not None else cfg.env.nightwatch_retention_days
    deleted = prune_old_snapshots(cfg.snapshots_dir, keep_days=keep)
    click.echo(f"deleted {deleted} snapshot directories older than {keep} days")


if __name__ == "__main__":
    cli()
