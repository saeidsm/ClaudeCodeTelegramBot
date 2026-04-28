"""Snapshot writer: builds the on-disk artifact for a given date.

ALL output passes through redactor.redact() before write — no exceptions.
"""

from __future__ import annotations

import csv
import html
import json
import os
import shutil
import zipfile
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.analyzer import Decision, ScoredIssue
from app.normalizer import Cluster, NormalizedIssue
from app.redactor import redact

log = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Webroot bridging lives in app.publisher (Bug 1 fix, 2026-04-28). The
# previous Phase-2B-fix2 bridge was here in build_snapshot, but the
# digest delivery flow needs to bridge on republish too — so the bridge
# now runs after the IPC POST returns 202, regardless of whether
# build_snapshot fired in this process. _render_index_html stays here
# because it's a rendering utility (it produces an artifact) and the
# publisher imports it lazily.
# ──────────────────────────────────────────────────────────────────────────


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_json(path: Path, obj: Any) -> None:
    _atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ──────────────────────────────────────────────────────────────────────────


def _summary(
    scored: list[ScoredIssue],
    clusters: list[Cluster],
    decisions: list[Decision],
    date_str: str,
    status: str,
) -> dict[str, Any]:
    by_level = Counter(s["level"] for s in scored)
    by_project = Counter(s["project_slug"] for s in scored)
    top10 = sorted(scored, key=lambda s: s["severity_score"], reverse=True)[:10]
    return {
        "date": date_str,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issue_count": len(scored),
        "cluster_count": len(clusters),
        "decision_count": len(decisions),
        "by_level": dict(by_level),
        "by_project": dict(by_project),
        "top10_summary": [
            {
                "issue_id": s["issue_id"],
                "project": s["project_slug"],
                "title": s["title"],
                "severity_score": s["severity_score"],
                "user_count": s["user_count"],
            }
            for s in top10
        ],
    }


def _write_issues_csv(path: Path, issues: list[NormalizedIssue]) -> None:
    fields = [
        "issue_id",
        "short_id",
        "project_slug",
        "title",
        "level",
        "count",
        "user_count",
        "first_seen",
        "last_seen",
        "status",
        "platform",
        "culprit",
        "release",
        "environment",
        "stack_signature",
        "permalink",
        "is_regression",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for i in issues:
            w.writerow({k: i.get(k, "") for k in fields})


def _markdown_digest(
    summary: dict, scored: list[ScoredIssue], clusters: list[Cluster], decisions: list[Decision]
) -> str:
    lines: list[str] = []
    lines.append(f"# NightWatch Daily — {summary['date']}\n")
    lines.append(f"**Status:** {summary['status']}\n")
    lines.append(f"**Generated:** {summary['generated_at']}\n")
    lines.append("\n## Counts\n")
    lines.append(f"- Total issues: **{summary['issue_count']}**")
    lines.append(f"- Clusters: **{summary['cluster_count']}**")
    lines.append(f"- Decisions: **{summary['decision_count']}**")
    if summary.get("by_level"):
        lines.append(f"- By level: {summary['by_level']}")
    if summary.get("by_project"):
        lines.append(f"- By project: {summary['by_project']}")
    lines.append("\n## Top 10 issues\n")
    for s in sorted(scored, key=lambda s: s["severity_score"], reverse=True)[:10]:
        flags = []
        if s["is_new"]:
            flags.append("NEW")
        if s["is_regression"]:
            flags.append("REGRESSION")
        if s["is_spike"]:
            flags.append("SPIKE")
        if s["is_release_correlated"]:
            flags.append("RELEASE")
        if s["is_user_impacting"]:
            flags.append(f"USERS:{s['user_count']}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"- **{s['severity_score']}** [{s['project_slug']}] {s['title']}{flag_str}"
        )

    if clusters:
        lines.append("\n## Cross-project clusters\n")
        for c in clusters:
            lines.append(
                f"- members={c.members} confidence={c.confidence:.2f} reason={c.reason} "
                f"route={c.shared_route or '-'}"
            )

    if decisions:
        lines.append("\n## Decision items\n")
        for d in decisions:
            lines.append(f"- **{d.kind}** ({d.confidence:.2f}): {d.summary}")

    lines.append("\n---\n_Phase 1 rule-based digest. No LLM in the loop yet._\n")
    return "\n".join(lines)


def _prompt_placeholder(date_str: str) -> str:
    return (
        f"# Phase 3 prompt placeholder for {date_str}\n\n"
        "This file will be populated in Phase 3 with a Claude-ready operational brief prompt.\n"
        "Phase 1 leaves it empty so downstream code can rely on the path existing.\n"
    )


def _zip_dir(src: Path, dest_zip: Path) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file() and p.name != dest_zip.name:
                zf.write(p, p.relative_to(src))


# ──────────────────────────────────────────────────────────────────────────


def _humanize_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _render_index_html(snap_dir: Path, date_str: str) -> str:
    """Build a self-contained HTML index for a bridged snapshot dir.

    Mobile-friendly, no JS, no external CSS. Shows verdict + top 5 at top,
    renders analysis.md inline (markdown lib if available, else <pre>),
    and lists every artifact with size + link.
    """
    summary: dict = {}
    top_issues: list[dict] = []
    decisions: list[dict] = []
    clusters: list[dict] = []
    try:
        summary = json.loads((snap_dir / "summary.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        top_issues = json.loads((snap_dir / "top_issues.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        decisions = json.loads((snap_dir / "decisions.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        clusters = json.loads((snap_dir / "clusters.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Lazy import — keep publisher contract unchanged when the bridge runs.
    from app.publisher import compute_verdict, explain_verdict

    icon, label = compute_verdict(top_issues, decisions, clusters=clusters)
    reasons = explain_verdict(top_issues, decisions, clusters=clusters)

    # analysis.md inline render (markdown lib if installed, else <pre>).
    analysis_md = ""
    try:
        analysis_md = (snap_dir / "analysis.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    analysis_html = ""
    if analysis_md:
        try:
            import markdown  # type: ignore[import-not-found]
            analysis_html = markdown.markdown(
                analysis_md, extensions=["fenced_code", "tables"]
            )
        except ImportError:
            analysis_html = f"<pre>{html.escape(analysis_md)}</pre>"

    # File listing — every file in snap_dir, recursive.
    file_rows: list[tuple[str, int]] = []
    for p in sorted(snap_dir.rglob("*")):
        if p.is_file() and p.name != "index.html":
            rel = p.relative_to(snap_dir).as_posix()
            file_rows.append((rel, p.stat().st_size))

    # Top 5 — quick scan above the fold.
    top5 = sorted(
        top_issues, key=lambda s: int(s.get("severity_score") or 0), reverse=True
    )[:5]

    # Build HTML.
    e = html.escape
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html lang='en'>")
    parts.append("<head>")
    parts.append("<meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>NightWatch — {e(date_str)}</title>")
    parts.append(
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
        "margin:0;padding:1rem;max-width:900px;margin:0 auto;color:#1f2937;"
        "background:#f9fafb;line-height:1.5}"
        "h1,h2,h3{color:#111827}"
        "h1{font-size:1.5rem;margin:.5rem 0}"
        ".verdict{display:inline-block;padding:.4rem .8rem;border-radius:.5rem;"
        "font-weight:600;font-size:1.1rem;margin:.5rem 0}"
        ".v-CRITICAL{background:#fee2e2;color:#991b1b}"
        ".v-NEEDS{background:#fef3c7;color:#92400e}"
        ".v-CLEAR{background:#d1fae5;color:#065f46}"
        ".reasons{background:#fff;border:1px solid #e5e7eb;border-radius:.5rem;"
        "padding:.75rem 1rem;margin:.5rem 0}"
        ".reasons li{margin:.25rem 0}"
        ".dl{display:inline-block;background:#0ea5e9;color:#fff;padding:.5rem 1rem;"
        "border-radius:.5rem;text-decoration:none;font-weight:600;margin:.5rem 0}"
        ".dl:hover{background:#0284c7}"
        "table{width:100%;border-collapse:collapse;background:#fff;"
        "border:1px solid #e5e7eb;border-radius:.5rem;overflow:hidden}"
        "th,td{text-align:left;padding:.5rem;border-bottom:1px solid #e5e7eb}"
        "th{background:#f3f4f6;font-weight:600;font-size:.875rem}"
        "tr:last-child td{border-bottom:0}"
        "td.sz{text-align:right;color:#6b7280;font-variant-numeric:tabular-nums}"
        "a{color:#0369a1;text-decoration:none}"
        "a:hover{text-decoration:underline}"
        ".analysis{background:#fff;border:1px solid #e5e7eb;border-radius:.5rem;"
        "padding:1rem;margin:1rem 0}"
        ".analysis pre{background:#f3f4f6;padding:.75rem;border-radius:.4rem;"
        "overflow-x:auto;font-size:.85rem}"
        "code{background:#f3f4f6;padding:.1rem .3rem;border-radius:.25rem;"
        "font-size:.9rem}"
        ".muted{color:#6b7280;font-size:.875rem}"
        "</style>"
    )
    parts.append("</head><body>")
    parts.append(f"<h1>🌙 NightWatch — {e(date_str)}</h1>")

    # Verdict block.
    css_class = (
        "v-CRITICAL" if label == "CRITICAL"
        else "v-NEEDS" if label == "NEEDS ATTENTION"
        else "v-CLEAR"
    )
    parts.append(
        f"<div class='verdict {css_class}'>{e(icon)} {e(label)}</div>"
    )

    if reasons:
        parts.append("<div class='reasons'><strong>Reasons:</strong><ul>")
        for r in reasons:
            parts.append(f"<li>{e(r)}</li>")
        parts.append("</ul></div>")

    # Quick stats.
    issue_count = int(summary.get("issue_count") or 0)
    cluster_count = int(summary.get("cluster_count") or 0)
    decision_count = int(summary.get("decision_count") or 0)
    by_level = summary.get("by_level") or {}
    by_proj = summary.get("by_project") or {}
    parts.append(
        f"<p class='muted'>Issues: <strong>{issue_count}</strong> · "
        f"Clusters: <strong>{cluster_count}</strong> · "
        f"Decisions: <strong>{decision_count}</strong></p>"
    )
    if by_level:
        lvl_parts = " · ".join(
            f"{k}: {v}" for k, v in sorted(by_level.items()) if v
        )
        parts.append(f"<p class='muted'>By level: {e(lvl_parts)}</p>")
    if by_proj:
        proj_parts = " · ".join(
            f"{k}: {v}" for k, v in sorted(by_proj.items()) if v
        )
        parts.append(f"<p class='muted'>By project: {e(proj_parts)}</p>")

    # Top 5.
    if top5:
        parts.append("<h2>🔝 Top 5 issues</h2>")
        parts.append("<table>")
        parts.append(
            "<tr><th>#</th><th>Title</th><th>Project</th>"
            "<th>Score</th><th>Count</th></tr>"
        )
        for i, s in enumerate(top5, 1):
            title = e(str(s.get("title") or "(no title)"))
            proj = e(str(s.get("project_slug") or "?"))
            score = int(s.get("severity_score") or 0)
            count = int(s.get("count") or 0)
            parts.append(
                f"<tr><td>{i}</td><td>{title}</td><td>{proj}</td>"
                f"<td>{score}</td><td>{count}</td></tr>"
            )
        parts.append("</table>")

    # Download ZIP.
    if (snap_dir / "report.zip").exists():
        parts.append("<a class='dl' href='report.zip'>⬇ Download ZIP</a>")

    # Analysis (inline markdown).
    if analysis_html:
        parts.append("<h2>📋 Analysis</h2>")
        parts.append(f"<div class='analysis'>{analysis_html}</div>")

    # File listing.
    if file_rows:
        parts.append("<h2>📁 Files</h2>")
        parts.append("<table>")
        parts.append("<tr><th>Name</th><th>Size</th></tr>")
        for rel, sz in file_rows:
            parts.append(
                f"<tr><td><a href='{e(rel)}'>{e(rel)}</a></td>"
                f"<td class='sz'>{e(_humanize_bytes(sz))}</td></tr>"
            )
        parts.append("</table>")

    parts.append(
        "<p class='muted' style='margin-top:2rem;text-align:center'>"
        "Generated by NightWatch · "
        f"<code>{e(date_str)}</code></p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def build_snapshot(
    date_str: str,
    *,
    snapshots_dir: Path,
    issues: list[NormalizedIssue],
    scored: list[ScoredIssue],
    clusters: list[Cluster],
    decisions: list[Decision],
    raw_evidence: dict[str, dict] | None = None,
    status: str = "ok",
) -> Path:
    """Write the snapshot directory for a given date. Always passes data through redact()."""
    raw_evidence = raw_evidence or {}
    snap_dir = _ensure_dir(snapshots_dir / date_str)
    evidence_dir = _ensure_dir(snap_dir / "evidence")

    # Idempotency: clear stale files (but keep the dir).
    for child in snap_dir.iterdir():
        if child.is_file():
            child.unlink()
    for child in evidence_dir.iterdir():
        if child.is_file():
            child.unlink()

    # Sanitize ALL outbound data through the redactor.
    safe_issues = redact(issues)
    safe_scored = redact(scored)
    safe_clusters = redact([c.to_dict() for c in clusters])
    safe_decisions = redact([d.to_dict() for d in decisions])

    summary = _summary(safe_scored, clusters, decisions, date_str, status)
    safe_summary = redact(summary)

    _write_json(snap_dir / "summary.json", safe_summary)
    _write_issues_csv(snap_dir / "issues.csv", safe_issues)

    top20 = sorted(safe_scored, key=lambda s: s["severity_score"], reverse=True)[:20]
    _write_json(snap_dir / "top_issues.json", top20)
    _write_json(snap_dir / "clusters.json", safe_clusters)
    _write_json(snap_dir / "decisions.json", safe_decisions)

    md = _markdown_digest(safe_summary, safe_scored, clusters, decisions)
    _atomic_write_text(snap_dir / "analysis.md", md)
    _atomic_write_text(snap_dir / "prompt.md", _prompt_placeholder(date_str))

    # Evidence: full events for top 10 issues, redacted in 'evidence' mode.
    top10_ids = [s["issue_id"] for s in sorted(safe_scored, key=lambda s: s["severity_score"], reverse=True)[:10]]
    for iid in top10_ids:
        ev = raw_evidence.get(iid)
        if not ev:
            continue
        safe_ev = redact(ev, mode="evidence")
        _write_json(evidence_dir / f"{iid}.json", safe_ev)

    # Bundle.
    zip_path = snap_dir / "report.zip"
    if zip_path.exists():
        zip_path.unlink()
    _zip_dir(snap_dir, zip_path)

    log.info(
        "builder.snapshot_written",
        date=date_str,
        path=str(snap_dir),
        issues=len(safe_issues),
        clusters=len(clusters),
        decisions=len(decisions),
        status=status,
    )
    # Note: webroot bridging happens in app.publisher after the IPC POST
    # returns 202 — see Publisher._bridge_to_webroot. We deliberately do
    # NOT bridge here so republish (which doesn't rebuild) still re-mirrors.
    return snap_dir


def update_baseline(snapshots_dir: Path, issues: list[NormalizedIssue], date_str: str) -> Path:
    """Roll forward a 7-day fingerprint baseline. Append-only with date pruning."""
    base_path = snapshots_dir / "baseline.json"
    baseline = (
        json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    )

    today: dict[str, int] = {}
    for i in issues:
        sig = i.get("stack_signature", "")
        today[sig] = today.get(sig, 0) + i.get("count", 0)

    for sig, count in today.items():
        entry = baseline.setdefault(sig, {"daily": {}, "avg_daily": 0.0})
        entry["daily"][date_str] = count

    # Prune to last 7 dates per signature, recompute avg.
    for entry in baseline.values():
        dates = sorted(entry["daily"].keys())[-7:]
        entry["daily"] = {d: entry["daily"][d] for d in dates}
        entry["avg_daily"] = (
            sum(entry["daily"].values()) / len(entry["daily"]) if entry["daily"] else 0.0
        )

    _write_json(base_path, baseline)
    return base_path


def write_stub_snapshot(date_str: str, snapshots_dir: Path, reason: str) -> Path:
    """Used when Sentry is unreachable — keeps downstream pipeline well-formed."""
    snap_dir = _ensure_dir(snapshots_dir / date_str)
    stub = {
        "date": date_str,
        "status": "sentry_unreachable",
        "reason": reason,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issue_count": 0,
        "cluster_count": 0,
        "decision_count": 0,
        "by_level": {},
        "by_project": {},
        "top10_summary": [],
    }
    _write_json(snap_dir / "summary.json", stub)
    _atomic_write_text(snap_dir / "issues.csv", "issue_id\n")
    _write_json(snap_dir / "top_issues.json", [])
    _write_json(snap_dir / "clusters.json", [])
    _write_json(snap_dir / "decisions.json", [])
    _atomic_write_text(
        snap_dir / "analysis.md",
        f"# NightWatch Daily — {date_str}\n\n**Status:** sentry_unreachable\n\nReason: {reason}\n",
    )
    _atomic_write_text(snap_dir / "prompt.md", _prompt_placeholder(date_str))
    _ensure_dir(snap_dir / "evidence")
    zip_path = snap_dir / "report.zip"
    if zip_path.exists():
        zip_path.unlink()
    _zip_dir(snap_dir, zip_path)
    return snap_dir


def prune_old_snapshots(snapshots_dir: Path, keep_days: int) -> int:
    """Delete snapshot directories older than keep_days days. Returns count deleted."""
    if not snapshots_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc).date()
    deleted = 0
    for child in snapshots_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            d = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        age_days = (cutoff - d).days
        if age_days > keep_days:
            shutil.rmtree(child)
            deleted += 1
    return deleted


# Suppress unused-import lint (asdict is reserved for future use).
_ = asdict
