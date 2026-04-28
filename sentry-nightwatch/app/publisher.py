"""Publisher — POSTs the daily digest to the bot's IPC `/inject` endpoint.

Phase 2B: direct delivery (no LLM, no Claude session). Builds a Telegram-HTML
digest from the snapshot artefacts written by `app.builder`, signs it with
HMAC-SHA256, and hands it to the bot which forwards it to chat_ids.

A future "Investigate" button will be wired to a Claude handoff in Phase 3;
for now the third button is a deliberate placeholder so users see the slot.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import html
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog

log = structlog.get_logger(__name__)

# Telegram single-message ceiling. We aim ≤ 3950 to leave headroom for Telegram's
# own overhead and for the inline keyboard JSON.
TELEGRAM_HARD_LIMIT = 4000
DIGEST_BUDGET = 3950

# Watchdog digest is shorter — no top issues / decisions to render.
WATCHDOG_BUDGET = 1500

# HTTP retry schedule on transient/5xx failures.
RETRY_DELAYS_S = (1, 4, 16)

Mode = Literal["nightly", "manual", "watchdog"]


def _build_session_label(date_str: str, mode: str) -> str:
    """Build the IPC session_label for the bot's _NW_SEEN dedup.

    All modes now include a unix timestamp suffix. The bot's dedup remains
    a useful guard against accidental duplicate POSTs within a single
    second (e.g., a retry burst), but it no longer blocks legitimate
    same-day runs from the systemd timer, /nightwatch_run, /nightwatch_last,
    or watchdog.

    The previous design carved out "nightly" as stable on the assumption
    that "we never want two nightly runs in a day". In practice:
      - The bot can stay up for days, so cache stickiness blocked manual
        /nightwatch_run (which also routes through mode=nightly).
      - systemd Persistent=true + RandomizedDelaySec already prevents
        accidental same-second double-fires.
      - DST shifts could legitimately produce two nightly runs in a day;
        we want both delivered.
    """
    return f"nightwatch-{date_str}-{mode}-{int(time.time())}"


@dataclass
class PublishResult:
    ok: bool
    status_code: int = 0
    error: str | None = None
    delivered: int = 0
    duplicates: int = 0
    failed: int = 0
    chat_ids: list[int] = field(default_factory=list)
    snapshot_dir: str | None = None
    session_label: str | None = None


# ──────────────────────────────────────────────────────────────────────────
# Verdict thresholds (deterministic — no LLM)
#
# Phase-2B-fix2: rewritten as multi-signal logic to fix the false-negative
# observed on 2026-04-26 (19 spikes + a 806-event runaway returned ALL CLEAR
# under the old single-signal max_score test).
#
# Tune values here without touching logic. Conditions (a..f) inside each
# severity bucket are OR-ed; the first severity bucket whose ANY condition
# matches wins (CRITICAL > NEEDS ATTENTION > ALL CLEAR).
# ──────────────────────────────────────────────────────────────────────────

VERDICT_THRESHOLDS: dict[str, dict[str, int]] = {
    "critical": {
        # a) any issue with level == "fatal"   (presence-based, no integer)
        # b) any issue with count >= N           (volume bomb / runaway loop)
        "count_volume_bomb": 500,
        # c) any issue with severity_score >= N
        "severity_score": 80,
        # d) total spike-issues across snapshot >= N
        "spike_count": 5,
        # e) cluster of new + spike + cross-project (presence-based)
        # f) any decision flagged is_critical=True (presence-based)
    },
    "needs_attention": {
        # a) any error-level issue with count >= N
        "error_count": 50,
        # b) any issue with severity_score >= N
        "severity_score": 50,
        # c) >= N spike-issues
        "spike_count": 1,
        # d) >= N regressions
        "regression_count": 1,
        # e) any decision present (presence-based)
        # f) >= N new issues in 24h
        "new_count": 5,
    },
}


def _critical_reasons(
    scored: list[dict],
    decisions: list[dict],
    *,
    clusters: list[dict] | None = None,
) -> list[str]:
    """Return reasons matching CRITICAL conditions, in spec order (a..f)."""
    reasons: list[str] = []
    crit = VERDICT_THRESHOLDS["critical"]
    clusters = clusters or []

    # a) fatal level
    fatal_count = sum(1 for s in scored if (s.get("level") == "fatal"))
    if fatal_count:
        reasons.append(f"{fatal_count} fatal-level issue(s) present")

    # b) volume bomb
    bomb_threshold = crit["count_volume_bomb"]
    bomb_top = max(
        ((int(s.get("count") or 0), s.get("title") or "?") for s in scored),
        default=(0, ""),
    )
    if bomb_top[0] >= bomb_threshold:
        title_short = bomb_top[1][:60]
        reasons.append(
            f"Top issue '{title_short}' has {bomb_top[0]} events (≥ {bomb_threshold})"
        )

    # c) severity score
    sev_threshold = crit["severity_score"]
    max_score = max((int(s.get("severity_score") or 0) for s in scored), default=0)
    if max_score >= sev_threshold:
        reasons.append(f"Max severity_score = {max_score} (≥ {sev_threshold})")

    # d) spike fan-out
    spike_threshold = crit["spike_count"]
    spike_count = sum(1 for s in scored if s.get("is_spike"))
    if spike_count >= spike_threshold:
        reasons.append(
            f"{spike_count} issues spiked vs baseline (≥ {spike_threshold} triggers CRITICAL)"
        )

    # e) new + spike + cross-project cluster co-occurrence
    has_new = any(s.get("is_new") for s in scored)
    has_spike = any(s.get("is_spike") for s in scored)
    has_cluster = bool(clusters)
    if has_new and has_spike and has_cluster:
        reasons.append("New + spike + cross-project cluster co-occur in this window")

    # f) decisions explicitly marked critical
    crit_decisions = sum(1 for d in decisions if bool(d.get("is_critical")))
    if crit_decisions:
        reasons.append(f"{crit_decisions} decision(s) flagged is_critical=true")

    return reasons


def _attention_reasons(
    scored: list[dict],
    decisions: list[dict],
) -> list[str]:
    """Return reasons matching NEEDS ATTENTION conditions, in spec order (a..f)."""
    reasons: list[str] = []
    th = VERDICT_THRESHOLDS["needs_attention"]

    # a) error-level + count
    err_thr = th["error_count"]
    err_hits = [
        s for s in scored
        if s.get("level") == "error" and int(s.get("count") or 0) >= err_thr
    ]
    if err_hits:
        reasons.append(
            f"{len(err_hits)} error-level issue(s) with count ≥ {err_thr}"
        )

    # b) severity score
    sev_thr = th["severity_score"]
    max_score = max((int(s.get("severity_score") or 0) for s in scored), default=0)
    if max_score >= sev_thr:
        reasons.append(f"Max severity_score = {max_score} (≥ {sev_thr})")

    # c) any spikes
    spike_thr = th["spike_count"]
    spike_count = sum(1 for s in scored if s.get("is_spike"))
    if spike_count >= spike_thr:
        reasons.append(f"{spike_count} spike-issue(s) (≥ {spike_thr})")

    # d) regressions
    reg_thr = th["regression_count"]
    reg_count = sum(1 for s in scored if s.get("is_regression"))
    if reg_count >= reg_thr:
        reasons.append(f"{reg_count} regression(s) (≥ {reg_thr})")

    # e) decisions
    if decisions:
        reasons.append(f"{len(decisions)} decision item(s) surfaced")

    # f) new in 24h
    new_thr = th["new_count"]
    new_count = sum(1 for s in scored if s.get("is_new"))
    if new_count >= new_thr:
        reasons.append(f"{new_count} new issue(s) in 24h (≥ {new_thr})")

    return reasons


def compute_verdict(
    scored: list[dict],
    decisions: list[dict],
    *,
    clusters: list[dict] | None = None,
) -> tuple[str, str]:
    """Return (icon, label) per the deterministic verdict table.

    🚨 CRITICAL          — any of the conditions in VERDICT_THRESHOLDS["critical"]
    ⚠️ NEEDS ATTENTION  — any of the conditions in VERDICT_THRESHOLDS["needs_attention"]
    ✅ ALL CLEAR        — none of the above
    """
    if _critical_reasons(scored, decisions, clusters=clusters):
        return ("🚨", "CRITICAL")
    if _attention_reasons(scored, decisions):
        return ("⚠️", "NEEDS ATTENTION")
    return ("✅", "ALL CLEAR")


def explain_verdict(
    scored: list[dict],
    decisions: list[dict],
    *,
    clusters: list[dict] | None = None,
) -> list[str]:
    """Return human-readable reasons for the current verdict.

    Returns the reasons that *fired* — i.e. for a CRITICAL verdict it returns
    the CRITICAL conditions that matched; for NEEDS ATTENTION, the NEEDS
    ATTENTION conditions; for ALL CLEAR, an empty list.
    """
    crit = _critical_reasons(scored, decisions, clusters=clusters)
    if crit:
        return crit
    att = _attention_reasons(scored, decisions)
    if att:
        return att
    return []


# ──────────────────────────────────────────────────────────────────────────
# HTML digest renderer
# ──────────────────────────────────────────────────────────────────────────


def _esc(s: Any) -> str:
    """Telegram-HTML escape (only &, <, >)."""
    return html.escape(str(s) if s is not None else "", quote=False)


def _level_counts(by_level: dict[str, int]) -> str:
    """Format e.g. '3 fatal, 12 error, 32 warn' from the by_level dict."""
    parts = []
    for level, label in (("fatal", "fatal"), ("error", "error"), ("warning", "warn"), ("info", "info")):
        n = int(by_level.get(level, 0) or 0)
        if n:
            parts.append(f"{n} {label}")
    return ", ".join(parts) or "0"


def _project_list(by_project: dict[str, int]) -> str:
    return ", ".join(sorted(by_project.keys())) or "—"


def render_digest_html(
    summary: dict,
    top_issues: list[dict],
    decisions: list[dict],
    *,
    tz_label: str = "Asia/Tehran",
    window_label: str = "last 24h",
    clusters: list[dict] | None = None,
) -> str:
    """Return a single Telegram-HTML message body. Capped at DIGEST_BUDGET chars."""
    date = _esc(summary.get("date", "?"))
    proj_str = _esc(_project_list(summary.get("by_project") or {}))

    by_level = summary.get("by_level") or {}
    counts_str = _level_counts(by_level)
    issue_count = int(summary.get("issue_count") or 0)
    cluster_count = int(summary.get("cluster_count") or 0)
    new_count = sum(1 for s in top_issues if s.get("is_new"))
    regr_count = sum(1 for s in top_issues if s.get("is_regression"))
    spike_count = sum(1 for s in top_issues if s.get("is_spike"))

    icon, verdict_label = compute_verdict(top_issues, decisions, clusters=clusters)
    reasons = explain_verdict(top_issues, decisions, clusters=clusters)

    lines: list[str] = []
    lines.append(f"🌙 <b>NightWatch</b> · {date} ({_esc(tz_label)})")
    lines.append(f"⏱ Window: {_esc(window_label)} · Sentry projects: {proj_str}")
    lines.append("")
    lines.append("📊 <b>Snapshot</b>")
    lines.append(f"  • Total issues: <b>{issue_count}</b> ({_esc(counts_str)})")
    lines.append(
        f"  • New today: <b>{new_count}</b> · Regressions: <b>{regr_count}</b> · Spikes: <b>{spike_count}</b>"
    )
    lines.append(f"  • Cross-project clusters: <b>{cluster_count}</b>")
    lines.append(f"  • Verdict: {icon} <b>{verdict_label}</b>")
    if reasons:
        lines.append("  • Reasons:")
        # Cap whole reasons block at ≤ 500 chars; truncate with "…and N more".
        rendered: list[str] = []
        used = 0
        budget = 500
        for idx, r in enumerate(reasons):
            line = f"    – {_esc(r)}"
            extra = len(line) + 1  # +1 for the newline
            if used + extra > budget and idx > 0:
                remaining = len(reasons) - idx
                rendered.append(f"    – …and {remaining} more")
                break
            rendered.append(line)
            used += extra
        lines.extend(rendered)
    lines.append("")

    top3 = sorted(top_issues, key=lambda s: int(s.get("severity_score") or 0), reverse=True)[:3]
    if top3:
        lines.append("🔝 <b>Top 3</b>")
        for i, s in enumerate(top3, 1):
            title = _esc(s.get("title") or "(no title)")
            slug = _esc(s.get("project_slug") or "?")
            count = int(s.get("count") or 0)
            users = int(s.get("user_count") or 0)
            flags = []
            if s.get("is_new"):
                flags.append("new")
            if s.get("is_regression"):
                flags.append("regression")
            if s.get("is_spike"):
                flags.append("spike")
            if s.get("is_release_correlated"):
                flags.append("release")
            extra = []
            extra.append(f"{count}×")
            if users:
                extra.append(f"{users} users")
            if flags:
                extra.append(", ".join(flags))
            lines.append(f"  {i}. <b>{title}</b> · {slug} · {' · '.join(extra)}")
        lines.append("")

    if decisions:
        lines.append("🧭 <b>Decisions needed</b>")
        for d in decisions[:5]:
            lines.append(f"  • {_esc(d.get('summary') or '(no summary)')}")
        if len(decisions) > 5:
            lines.append(f"  …and {len(decisions) - 5} more (see report)")
        lines.append("")

    lines.append("📁 Full report → <i>see button below</i>")

    body = "\n".join(lines)
    if len(body) > DIGEST_BUDGET:
        # Truncate from the bottom (decisions / top3) progressively.
        truncated = _truncate_to_budget(lines, DIGEST_BUDGET)
        body = truncated + "\n\n<i>(digest truncated — see Full Report)</i>"
        # Defensive double-check.
        if len(body) > TELEGRAM_HARD_LIMIT:
            body = body[: TELEGRAM_HARD_LIMIT - 4] + "…"
    return body


def _truncate_to_budget(lines: list[str], budget: int) -> str:
    """Drop trailing lines until the joined length fits in `budget`."""
    keep = list(lines)
    while keep and len("\n".join(keep)) > budget - 50:
        keep.pop()
    return "\n".join(keep)


# ──────────────────────────────────────────────────────────────────────────
# Watchdog digest — Phase 2B-fix
#
# Sent when the pipeline degrades to `sentry_unreachable`. Different layout
# from the healthy digest: no top issues, no decisions, suppressed verdict,
# and an explanatory body so silence after /nightwatch_run never happens
# again.
# ──────────────────────────────────────────────────────────────────────────


def render_watchdog_html(
    date_str: str,
    reason: str,
    snapshot_dir: Path,
    *,
    tz_label: str = "Asia/Tehran",
    started_at: datetime | None = None,
    org_slug: str | None = None,
) -> str:
    """Telegram-HTML watchdog digest. Capped at WATCHDOG_BUDGET chars."""
    started = (started_at or datetime.now(timezone.utc)).strftime("%H:%M UTC")
    org = org_slug or os.environ.get("SENTRY_ORG_SLUG", "(unset)")
    lines = [
        f"⚠️ <b>NightWatch · Watchdog</b> · {_esc(date_str)} ({_esc(tz_label)})",
        "",
        "The pipeline ran but could not collect data.",
        "",
        f"📛 Reason: <i>{_esc(reason)}</i>",
        f"🕒 Run started: {_esc(started)}",
        f"📂 Stub snapshot: <code>{_esc(snapshot_dir.as_posix())}</code>",
        "",
        "Suggested checks:",
        "  • SENTRY_AUTH_TOKEN valid and not expired",
        "  • Sentry API reachable from this host",
        f"  • Org slug matches: <code>{_esc(org)}</code>",
        "",
        "<i>No new errors observed (data is incomplete — verdict suppressed).</i>",
    ]
    body = "\n".join(lines)
    if len(body) > WATCHDOG_BUDGET:
        body = body[: WATCHDOG_BUDGET - 4] + "…"
    return body


# ──────────────────────────────────────────────────────────────────────────
# Publisher
# ──────────────────────────────────────────────────────────────────────────


class Publisher:
    """Owns the IPC handshake. One instance per `nightwatch run` invocation."""

    def __init__(
        self,
        *,
        bot_url: str | None = None,
        hmac_secret: str | None = None,
        chat_ids: list[int] | None = None,
        report_base_url: str | None = None,
        sentry_org_url: str | None = None,
        webroot_dir: str | None = None,
        report_token: str | None = None,
    ) -> None:
        self.bot_url = (bot_url or os.environ.get("BOT_IPC_URL", "http://127.0.0.1:9091")).rstrip("/")
        self.hmac_secret = (hmac_secret or os.environ.get("BOT_IPC_HMAC_SECRET", "")).strip()
        if chat_ids is None:
            chat_ids_env = os.environ.get("NIGHTWATCH_TELEGRAM_CHAT_IDS", "").strip()
            chat_ids = [int(x.strip()) for x in chat_ids_env.split(",") if x.strip()]
        self.chat_ids = chat_ids
        self.report_base_url = (
            report_base_url or os.environ.get("NIGHTWATCH_REPORT_BASE_URL", "https://devops.shahrzad.ai/reports")
        ).rstrip("/")
        self.sentry_org_url = (
            sentry_org_url or os.environ.get("NIGHTWATCH_SENTRY_ORG_URL", "https://shahrzad-ai.sentry.io")
        ).rstrip("/")
        # ── Bug 1 fix (2026-04-28) ────────────────────────────────────────
        # NIGHTWATCH_WEBROOT_DIR is the token-gated dir Caddy serves at
        # NIGHTWATCH_REPORT_BASE_URL/<token>/. We mirror each fresh
        # snapshot here AFTER the IPC POST returns 202 so the
        # "📊 Full Report" button URL actually resolves.
        # NIGHTWATCH_REPORT_TOKEN is duplicated info (already encoded in
        # the path of WEBROOT_DIR) but separated as a knob so the URL
        # base and token can diverge later (e.g., multi-tenant).
        # If either is empty the bridge is a no-op + warning, and the URL
        # falls back to the un-tokenised <BASE>/nightwatch-<date>/ form.
        self.webroot_dir = (
            webroot_dir if webroot_dir is not None
            else os.environ.get("NIGHTWATCH_WEBROOT_DIR", "")
        ).strip()
        self.report_token = (
            report_token if report_token is not None
            else os.environ.get("NIGHTWATCH_REPORT_TOKEN", "")
        ).strip()

        if not self.hmac_secret:
            raise RuntimeError("BOT_IPC_HMAC_SECRET is not set — refusing to publish")
        if not self.chat_ids:
            raise RuntimeError("NIGHTWATCH_TELEGRAM_CHAT_IDS is empty — refusing to publish (fail-loud)")

    # ── building the payload ──────────────────────────────────────────────

    def _report_url(self, date_str: str) -> str:
        """Construct the public report URL for `date_str`.

        With NIGHTWATCH_REPORT_TOKEN set, returns
            <BASE>/<TOKEN>/nightwatch-<date>/
        Without it, falls back to <BASE>/nightwatch-<date>/ (the pre-fix
        layout — kept for back-compat with environments that haven't
        migrated yet).
        """
        if self.report_token:
            return (
                f"{self.report_base_url}/{self.report_token}"
                f"/nightwatch-{date_str}/"
            )
        return f"{self.report_base_url}/nightwatch-{date_str}/"

    def build_buttons(self, date_str: str) -> list[dict[str, str]]:
        return [
            {"text": "📊 Full Report", "url": self._report_url(date_str)},
            {"text": "🔍 In Sentry", "url": f"{self.sentry_org_url}/issues/?statsPeriod=24h"},
            {"text": "💬 Investigate", "url": "https://t.me/"},  # Phase 3 placeholder
        ]

    def build_watchdog_buttons(self, date_str: str) -> list[dict[str, str]]:
        return [
            {"text": "📊 Stub Report", "url": self._report_url(date_str)},
            {"text": "🔍 Sentry Status", "url": "https://status.sentry.io/"},
            {"text": "🛠 Re-run", "url": "https://t.me/"},  # Phase 3: callback_data="nw:rerun"
        ]

    def build_request_body(
        self,
        snapshot_dir: Path,
        mode: Mode,
    ) -> dict[str, Any]:
        summary = json.loads((snapshot_dir / "summary.json").read_text(encoding="utf-8"))
        try:
            top_issues = json.loads((snapshot_dir / "top_issues.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            top_issues = []
        try:
            decisions = json.loads((snapshot_dir / "decisions.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            decisions = []
        try:
            clusters = json.loads((snapshot_dir / "clusters.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            clusters = []

        date_str = str(summary.get("date") or snapshot_dir.name)
        message_html = render_digest_html(summary, top_issues, decisions, clusters=clusters)
        buttons = self.build_buttons(date_str)
        body = {
            "session_label": _build_session_label(date_str, mode),
            "project": "shahrzad-ops",
            "message_html": message_html,
            "buttons": buttons,
            "chat_ids": list(self.chat_ids),
            "report_url": self._report_url(date_str),
        }
        return body

    # ── HTTP transport ────────────────────────────────────────────────────

    def _sign(self, raw: bytes) -> str:
        return hmac.new(self.hmac_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    def _post_with_retry(self, body: dict[str, Any]) -> PublishResult:
        url = f"{self.bot_url}/inject"
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        sig = self._sign(raw)
        headers = {"Content-Type": "application/json", "X-NightWatch-Signature": sig}
        last_err = "no attempt"
        for attempt, delay in enumerate(RETRY_DELAYS_S, start=1):
            try:
                resp = httpx.post(url, content=raw, headers=headers, timeout=10.0)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning("publisher.network_error", attempt=attempt, error=last_err)
                if attempt < len(RETRY_DELAYS_S):
                    time.sleep(delay)
                continue
            if resp.status_code == 202:
                payload: dict[str, Any] = {}
                with contextlib.suppress(json.JSONDecodeError):
                    payload = resp.json()
                log.info(
                    "publisher.delivered",
                    session_label=body["session_label"],
                    chat_ids=body["chat_ids"],
                    delivered=payload.get("delivered", 0),
                    duplicates=payload.get("duplicates", 0),
                    failed=payload.get("failed", 0),
                )
                return PublishResult(
                    ok=True,
                    status_code=202,
                    delivered=int(payload.get("delivered", 0) or 0),
                    duplicates=int(payload.get("duplicates", 0) or 0),
                    failed=int(payload.get("failed", 0) or 0),
                    chat_ids=list(body["chat_ids"]),
                    session_label=body["session_label"],
                )
            # Permanent client error — do NOT retry.
            if 400 <= resp.status_code < 500:
                last_err = f"{resp.status_code}: {resp.text[:200]}"
                log.error("publisher.permanent_error", status=resp.status_code, body=resp.text[:200])
                return PublishResult(ok=False, status_code=resp.status_code, error=last_err)
            # 5xx — retry.
            last_err = f"{resp.status_code}: {resp.text[:200]}"
            log.warning("publisher.transient_error", attempt=attempt, status=resp.status_code, body=resp.text[:200])
            if attempt < len(RETRY_DELAYS_S):
                time.sleep(delay)
        return PublishResult(ok=False, error=f"max retries exhausted: {last_err}")

    # ── public API ────────────────────────────────────────────────────────

    def publish(self, snapshot_dir: Path, mode: Mode = "nightly") -> PublishResult:
        if not snapshot_dir.is_dir():
            return PublishResult(ok=False, error=f"snapshot dir does not exist: {snapshot_dir}")
        try:
            body = self.build_request_body(snapshot_dir, mode)
        except FileNotFoundError as exc:
            return PublishResult(ok=False, error=f"snapshot artefact missing: {exc}")
        result = self._post_with_retry(body)
        result.snapshot_dir = str(snapshot_dir.resolve())
        if result.ok:
            # Bug 1 fix (2026-04-28): bridge the snapshot into the
            # token-gated webroot AFTER the IPC POST returns 202 and
            # BEFORE we update last-digest.txt. Order matters because
            # /nightwatch_last reads last-digest.txt; if bridging blew up
            # we still want last-digest pointing at this run.
            #
            # The bridge intentionally never raises — failure to mirror
            # files into the webroot must NOT prevent the digest from
            # reaching Telegram (the user can SSH and read snapshots/
            # directly).
            self._bridge_to_webroot(snapshot_dir, snapshot_dir.name)
            self._record_last_digest(snapshot_dir)
        return result

    def build_watchdog_body(self, snapshot_dir: Path, reason: str) -> dict[str, Any]:
        """Build the IPC body for a watchdog (degraded-pipeline) digest."""
        date_str = snapshot_dir.name
        message_html = render_watchdog_html(date_str, reason, snapshot_dir)
        return {
            "session_label": _build_session_label(date_str, "watchdog"),
            "project": "shahrzad-ops",
            "message_html": message_html,
            "buttons": self.build_watchdog_buttons(date_str),
            "chat_ids": list(self.chat_ids),
            "report_url": f"{self.report_base_url}/nightwatch-{date_str}/",
        }

    def publish_watchdog(self, snapshot_dir: Path, reason: str) -> PublishResult:
        """Send the watchdog digest. Caller is expected to ignore failures
        and exit with the original sentry_unreachable code (= 2)."""
        if not snapshot_dir.is_dir():
            return PublishResult(ok=False, error=f"snapshot dir does not exist: {snapshot_dir}")
        body = self.build_watchdog_body(snapshot_dir, reason)
        result = self._post_with_retry(body)
        result.snapshot_dir = str(snapshot_dir.resolve())
        # Deliberately do NOT update last-digest.txt for watchdog runs:
        # /nightwatch_last should re-send the previous *real* digest, not the
        # degraded watchdog notice.
        return result

    # ── webroot bridge (Bug 1 fix, 2026-04-28) ────────────────────────────

    def _bridge_to_webroot(self, snapshot_dir: Path, date_str: str) -> Path | None:
        """Atomically mirror snapshot_dir into the token-gated webroot.

        Layout:
            ${NIGHTWATCH_WEBROOT_DIR}/nightwatch-<date>/

        Strategy:
            1. shutil.copytree to a sibling .tmp.<pid>.<ts> staging dir.
            2. Render index.html into the staging dir.
            3. chmod -R world-readable (Caddy runs in a container as a
               different uid).
            4. If destination exists, rename it aside, then os.rename
               staging → destination (atomic on Linux). Best-effort
               cleanup of the displaced dir.

        Returns the target path on success, None on skip or failure.

        ALL exceptions are swallowed (logged at warning level) — the
        digest must reach Telegram even if the report URL won't work.
        """
        if not self.webroot_dir:
            log.warning(
                "publisher.bridge_skipped",
                reason="NIGHTWATCH_WEBROOT_DIR is empty",
            )
            return None
        webroot = Path(self.webroot_dir)
        if not webroot.is_dir():
            log.warning(
                "publisher.bridge_skipped",
                reason="webroot does not exist",
                path=str(webroot),
            )
            return None
        if not snapshot_dir.is_dir():
            log.warning(
                "publisher.bridge_skipped",
                reason="snapshot dir does not exist",
                path=str(snapshot_dir),
            )
            return None

        target = webroot / f"nightwatch-{date_str}"
        ts = int(time.time())
        staging = webroot / f"nightwatch-{date_str}.tmp.{os.getpid()}.{ts}"

        try:
            # Clear any stale staging dir from a previous failed attempt.
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(snapshot_dir, staging)

            # Generate index.html in the staging dir so it lands in the
            # atomic swap. _render_index_html lives in builder.py — lazy
            # import keeps the publisher module-load contract unchanged.
            try:
                from app.builder import _render_index_html
                (staging / "index.html").write_text(
                    _render_index_html(staging, date_str), encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001 — never propagate
                log.warning("publisher.bridge_index_failed", error=str(exc))

            # World-readable: dirs 0o755, files 0o644.
            for p in [staging, *staging.rglob("*")]:
                try:
                    if p.is_dir():
                        p.chmod(0o755)
                    else:
                        p.chmod(0o644)
                except OSError as exc:
                    log.warning(
                        "publisher.bridge_chmod_failed",
                        path=str(p),
                        error=str(exc),
                    )

            # Atomic swap. If target exists, displace it first so the
            # rename onto the empty slot is atomic, then drop the
            # displaced dir.
            displaced: Path | None = None
            if target.exists():
                displaced = webroot / f"nightwatch-{date_str}.old.{os.getpid()}.{ts}"
                os.rename(target, displaced)
            os.rename(staging, target)
            if displaced is not None:
                shutil.rmtree(displaced, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 — never propagate to publish
            log.warning(
                "publisher.bridge_failed",
                reason=str(exc),
                snapshot_dir=str(snapshot_dir),
                target=str(target),
            )
            # Clean up staging if it survived the failure.
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            return None

        try:
            total_bytes = sum(
                p.stat().st_size for p in target.rglob("*") if p.is_file()
            )
        except OSError:
            total_bytes = -1
        log.info(
            "publisher.bridged",
            path=str(target),
            bytes=total_bytes,
        )
        return target

    def _record_last_digest(self, snapshot_dir: Path) -> None:
        """Store the snapshot path for `/nightwatch_last` and `republish` lookup."""
        try:
            target = snapshot_dir.parent / "last-digest.txt"
            target.write_text(str(snapshot_dir.resolve()) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — never propagate
            log.warning("publisher.last_digest_write_failed", error=str(exc))
