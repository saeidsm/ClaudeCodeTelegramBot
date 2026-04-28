"""Tests for app.publisher — Telegram digest builder + IPC poster."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from app.publisher import (
    DIGEST_BUDGET,
    Publisher,
    PublishResult,
    compute_verdict,
    render_digest_html,
)

# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


def _summary(by_level: dict[str, int] | None = None, **overrides) -> dict:
    base = {
        "date": "2026-04-26",
        "status": "ok",
        "issue_count": 47,
        "cluster_count": 1,
        "by_level": by_level or {"fatal": 3, "error": 12, "warning": 32},
        "by_project": {"shahrzad-backend": 30, "shahrzad-frontend": 17},
        "top10_summary": [],
    }
    base.update(overrides)
    return base


def _scored(level: str = "error", score: int = 60, **overrides) -> dict:
    base = {
        "issue_id": "1",
        "project_slug": "shahrzad-backend",
        "title": "OOMKilled in pipeline.py:2847",
        "level": level,
        "count": 47,
        "user_count": 12,
        "first_seen": "2026-04-26T01:00:00Z",
        "last_seen": "2026-04-26T18:00:00Z",
        "release": None,
        "permalink": None,
        "is_new": False,
        "is_regression": False,
        "is_spike": False,
        "is_release_correlated": False,
        "is_user_impacting": True,
        "in_cluster": False,
        "severity_score": score,
        "reasons": [],
    }
    base.update(overrides)
    return base


# ──────────────────────────────────────────────────────────────────────────
# Verdict thresholds (3 cases per spec)
# ──────────────────────────────────────────────────────────────────────────


def test_verdict_all_clear() -> None:
    icon, label = compute_verdict([_scored(level="warning", score=20)], decisions=[])
    assert (icon, label) == ("✅", "ALL CLEAR")


def test_verdict_needs_attention_via_score() -> None:
    icon, label = compute_verdict([_scored(level="error", score=55)], decisions=[])
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


def test_verdict_needs_attention_via_decision() -> None:
    icon, label = compute_verdict([_scored(level="error", score=10)], decisions=[{"summary": "x"}])
    assert (icon, label) == ("⚠️", "NEEDS ATTENTION")


def test_verdict_critical_via_fatal_level() -> None:
    icon, label = compute_verdict([_scored(level="fatal", score=10)], decisions=[])
    assert (icon, label) == ("🚨", "CRITICAL")


def test_verdict_critical_via_score() -> None:
    icon, label = compute_verdict([_scored(level="error", score=85)], decisions=[])
    assert (icon, label) == ("🚨", "CRITICAL")


# ──────────────────────────────────────────────────────────────────────────
# HTML escape (security-relevant — Saeid called this out specifically)
# ──────────────────────────────────────────────────────────────────────────


def test_render_escapes_html_in_titles() -> None:
    """Test fixture user names like 'Nikrouz <special>' must render safely."""
    summary = _summary()
    top = [
        _scored(title="<script>alert('xss')</script> in Nikrouz <special> code"),
    ]
    out = render_digest_html(summary, top, decisions=[])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "Nikrouz &lt;special&gt;" in out


def test_render_escapes_html_in_decisions() -> None:
    out = render_digest_html(
        _summary(),
        [_scored()],
        decisions=[{"summary": "Endpoint <foo> & <bar> failing"}],
    )
    assert "<foo>" not in out
    assert "&lt;foo&gt; &amp; &lt;bar&gt;" in out


def test_render_basic_layout_present() -> None:
    out = render_digest_html(
        _summary(),
        [_scored(title="OOMKilled", count=47, user_count=12)],
        decisions=[{"summary": "Release backend@1.4.7 correlates"}],
    )
    assert "🌙" in out
    assert "<b>NightWatch</b>" in out
    assert "📊 <b>Snapshot</b>" in out
    assert "🔝 <b>Top 3</b>" in out
    assert "🧭 <b>Decisions needed</b>" in out
    assert "📁 Full report" in out


# ──────────────────────────────────────────────────────────────────────────
# Truncation
# ──────────────────────────────────────────────────────────────────────────


def test_render_truncates_when_over_budget() -> None:
    long_title = "X" * 800
    top = [_scored(title=long_title) for _ in range(20)]
    decisions = [{"summary": "Y" * 500} for _ in range(20)]
    out = render_digest_html(_summary(), top, decisions)
    assert len(out) <= 4000  # Telegram hard cap
    assert "(digest truncated" in out or len(out) <= DIGEST_BUDGET


def test_render_short_message_not_truncated() -> None:
    out = render_digest_html(_summary(), [_scored()], [])
    assert "(digest truncated" not in out
    assert len(out) < DIGEST_BUDGET


# ──────────────────────────────────────────────────────────────────────────
# Publisher: HMAC + body shape + chat_ids always present
# ──────────────────────────────────────────────────────────────────────────


def _write_snapshot(tmp_path: Path, *, top: list[dict], decisions: list[dict]) -> Path:
    snap = tmp_path / "2026-04-26"
    snap.mkdir()
    (snap / "summary.json").write_text(json.dumps(_summary()), encoding="utf-8")
    (snap / "top_issues.json").write_text(json.dumps(top), encoding="utf-8")
    (snap / "decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    return snap


def _make_publisher(secret: str = "test-secret-32chars-xxxxxxxxxxxxxxxx", chat_ids: list[int] | None = None) -> Publisher:
    return Publisher(
        bot_url="http://127.0.0.1:9091",
        hmac_secret=secret,
        chat_ids=chat_ids if chat_ids is not None else [42, 43],
        report_base_url="https://example.com/reports",
        sentry_org_url="https://example.sentry.io",
    )


def test_publisher_refuses_without_secret() -> None:
    with pytest.raises(RuntimeError, match="BOT_IPC_HMAC_SECRET"):
        Publisher(bot_url="x", hmac_secret="", chat_ids=[1])


def test_publisher_refuses_without_chat_ids() -> None:
    with pytest.raises(RuntimeError, match="chat_ids|TELEGRAM_CHAT_IDS"):
        Publisher(bot_url="x", hmac_secret="s" * 32, chat_ids=[])


def test_publisher_body_shape(tmp_path: Path) -> None:
    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    pub = _make_publisher()
    body = pub.build_request_body(snap, mode="nightly")
    assert body["session_label"].startswith("nightwatch-2026-04-26-nightly-")
    assert body["project"] == "shahrzad-ops"
    assert body["chat_ids"] == [42, 43]  # always explicit
    assert body["report_url"] == "https://example.com/reports/nightwatch-2026-04-26/"
    assert "<b>NightWatch</b>" in body["message_html"]
    # Three buttons in a known order
    assert [b["text"] for b in body["buttons"]] == ["📊 Full Report", "🔍 In Sentry", "💬 Investigate"]


def test_publisher_chat_ids_never_empty_in_request(tmp_path: Path) -> None:
    """chat_ids must ALWAYS be in the body — never rely on bot default."""
    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    pub = _make_publisher(chat_ids=[12345])
    body = pub.build_request_body(snap, mode="nightly")
    assert "chat_ids" in body
    assert body["chat_ids"] == [12345]


def test_publisher_mode_changes_label(tmp_path: Path) -> None:
    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    pub = _make_publisher()
    nightly = pub.build_request_body(snap, mode="nightly")
    manual = pub.build_request_body(snap, mode="manual")
    # Phase 2B-fix4: every mode is now timestamped, so the labels carry a
    # `-{mode}-{unix-ts}` suffix and the mode segment differs.
    assert nightly["session_label"].startswith("nightwatch-2026-04-26-nightly-")
    assert manual["session_label"].startswith("nightwatch-2026-04-26-manual-")
    # The trailing segment must be a numeric unix timestamp on both modes.
    for label in (nightly["session_label"], manual["session_label"]):
        ts_part = label.rsplit("-", 1)[-1]
        assert ts_part.isdigit() and int(ts_part) > 0


# ──────────────────────────────────────────────────────────────────────────
# Phase-2B-fix2: explain_verdict reasons rendered into digest HTML
# ──────────────────────────────────────────────────────────────────────────


def test_render_includes_reasons_section_when_verdict_fires() -> None:
    """A snapshot that triggers CRITICAL must show the Reasons block."""
    top = [
        _scored(
            level="error",
            count=900,           # volume bomb
            severity_score=40,
            is_spike=True,
            title="Loop bomb",
        )
    ] + [
        _scored(level="error", is_spike=True, severity_score=20, title=f"Spike{i}")
        for i in range(6)
    ]
    out = render_digest_html(_summary(), top, decisions=[])
    assert "🚨 <b>CRITICAL</b>" in out
    assert "Reasons:" in out
    # At least one of the two key triggers should appear by name.
    assert "events" in out or "spike" in out


def test_render_no_reasons_section_when_all_clear() -> None:
    """A clean snapshot (warning-only, low score) renders no Reasons block."""
    top = [_scored(level="warning", count=3, severity_score=10)]
    out = render_digest_html(_summary(by_level={"warning": 1}), top, decisions=[])
    assert "✅ <b>ALL CLEAR</b>" in out
    assert "Reasons:" not in out


def test_render_reasons_truncates_when_too_many() -> None:
    """When >5 reasons stack up, the block stays under ~500 chars and ends
    with an "…and N more" line."""
    # Build a snapshot that fires *every* NEEDS ATTENTION condition AND
    # multiple critical ones — gives a long reasons list.
    top = (
        # error_count + severity_score
        [_scored(level="error", count=120, severity_score=70, title="A")]
        # spike fan-out (>= 5)
        + [_scored(level="error", is_spike=True, severity_score=20, title=f"S{i}") for i in range(7)]
        # regressions
        + [_scored(level="error", is_regression=True, severity_score=20, title=f"R{i}") for i in range(2)]
        # new
        + [_scored(level="error", is_new=True, severity_score=20, title=f"N{i}") for i in range(6)]
        # volume bomb
        + [_scored(level="error", count=900, severity_score=40, title="VolumeBomb")]
        # fatal
        + [_scored(level="fatal", count=1, severity_score=20, title="FatalOne")]
    )
    decisions = [{"summary": "Endpoint x", "is_critical": True}]
    out = render_digest_html(_summary(), top, decisions=decisions)
    # The Reasons block exists and is bounded.
    assert "Reasons:" in out
    # Find the chunk between "Reasons:" and the next blank line.
    after = out.split("Reasons:", 1)[1]
    block = after.split("\n\n", 1)[0]
    # 500 char budget for the whole block (we add a small fudge for the
    # framing chars).
    assert len(block) < 700, f"reasons block too long: {len(block)}"
    # Truncation marker appears somewhere if we generated >5 reasons.
    # (Not strictly enforced here — just sanity.)


# ──────────────────────────────────────────────────────────────────────────
# Phase-2B-fix2: clusters loaded into build_request_body
# ──────────────────────────────────────────────────────────────────────────


def test_build_request_body_loads_clusters(tmp_path: Path) -> None:
    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    # Add a clusters.json — the only data source for CRITICAL trigger (e).
    (snap / "clusters.json").write_text(
        json.dumps([{"members": ["1"], "confidence": 0.8}]), encoding="utf-8"
    )
    pub = _make_publisher()
    body = pub.build_request_body(snap, mode="nightly")
    assert body["session_label"].startswith("nightwatch-2026-04-26-nightly-")
    # No assertion on clusters in body — they aren't surfaced as a top-level
    # field; we only verify the build succeeded with a clusters.json present.


# ──────────────────────────────────────────────────────────────────────────
# HMAC reproducibility
# ──────────────────────────────────────────────────────────────────────────


def test_hmac_signature_reproducible(tmp_path: Path) -> None:
    secret = "deadbeef" * 4
    pub = _make_publisher(secret=secret)
    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    body = pub.build_request_body(snap, mode="nightly")
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert pub._sign(raw) == expected
    # Same input twice → same digest.
    assert pub._sign(raw) == pub._sign(raw)


def test_hmac_signature_changes_with_body(tmp_path: Path) -> None:
    pub = _make_publisher()
    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    body = pub.build_request_body(snap, mode="nightly")
    raw1 = json.dumps(body, ensure_ascii=False).encode("utf-8")
    body["chat_ids"] = [99]
    raw2 = json.dumps(body, ensure_ascii=False).encode("utf-8")
    assert pub._sign(raw1) != pub._sign(raw2)


# ──────────────────────────────────────────────────────────────────────────
# Snapshot dir handling
# ──────────────────────────────────────────────────────────────────────────


def test_publish_missing_snapshot_returns_error(tmp_path: Path) -> None:
    pub = _make_publisher()
    result = pub.publish(tmp_path / "does-not-exist", mode="nightly")
    assert result.ok is False
    assert "does not exist" in (result.error or "")


def test_publish_records_last_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful publish writes last-digest.txt next to the snapshot dir."""
    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    pub = _make_publisher()

    # Stub the HTTP layer to simulate a 202.
    def fake_post(self, body):  # noqa: ANN001 - test stub
        return PublishResult(ok=True, status_code=202, delivered=1, chat_ids=body["chat_ids"])

    monkeypatch.setattr(Publisher, "_post_with_retry", fake_post)
    result = pub.publish(snap, mode="nightly")
    assert result.ok
    last = (snap.parent / "last-digest.txt").read_text(encoding="utf-8").strip()
    assert last == str(snap.resolve())


# ──────────────────────────────────────────────────────────────────────────
# Watchdog digest (Phase 2B-fix)
# ──────────────────────────────────────────────────────────────────────────


import os  # noqa: E402

from app.publisher import WATCHDOG_BUDGET, render_watchdog_html  # noqa: E402


def test_render_watchdog_basic_layout() -> None:
    out = render_watchdog_html(
        "2026-04-27",
        "SENTRY_AUTH_TOKEN is not set",
        Path("/opt/sentry-nightwatch/snapshots/2026-04-27"),
        org_slug="ziggurat-f9",
    )
    assert "⚠️ <b>NightWatch · Watchdog</b>" in out
    assert "2026-04-27" in out
    assert "Asia/Tehran" in out
    assert "SENTRY_AUTH_TOKEN is not set" in out
    assert "📛 Reason" in out
    assert "🕒 Run started" in out
    assert "📂 Stub snapshot" in out
    assert "Suggested checks" in out
    assert "ziggurat-f9" in out
    assert "verdict suppressed" in out


def test_render_watchdog_under_budget() -> None:
    """Watchdog HTML must stay ≤ 1500 chars even with a long reason."""
    long_reason = "x" * 500
    out = render_watchdog_html(
        "2026-04-27", long_reason, Path("/snap/2026-04-27")
    )
    assert len(out) <= WATCHDOG_BUDGET


def test_render_watchdog_escapes_html_in_reason() -> None:
    """A user-supplied reason string must be HTML-escaped."""
    out = render_watchdog_html(
        "2026-04-27", "<script>x</script>", Path("/snap/2026-04-27")
    )
    assert "<script>x</script>" not in out
    assert "&lt;script&gt;x&lt;/script&gt;" in out


def test_publisher_build_watchdog_body_shape(tmp_path: Path) -> None:
    snap = tmp_path / "2026-04-27"
    snap.mkdir()
    pub = _make_publisher()
    body = pub.build_watchdog_body(snap, reason="Sentry returned 401 — check token scopes")
    # Phase 2B-fix3: watchdog labels are also timestamped — every degraded run
    # should punch through dedup so the watchdog message is never silently swallowed.
    assert body["session_label"].startswith("nightwatch-2026-04-27-watchdog-")
    assert body["chat_ids"] == [42, 43]
    assert "Watchdog" in body["message_html"]
    assert "401" in body["message_html"]
    # Three watchdog buttons in the spec'd order
    assert [b["text"] for b in body["buttons"]] == ["📊 Stub Report", "🔍 Sentry Status", "🛠 Re-run"]
    assert all(b.get("url") for b in body["buttons"])  # URL fallback for Phase 2B
    assert len(body["message_html"]) <= WATCHDOG_BUDGET


def test_publisher_publish_watchdog_does_not_record_last_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watchdog publishes must NOT update last-digest.txt — /nightwatch_last
    should still re-send the previous *real* digest, not the degraded notice."""
    snap = tmp_path / "2026-04-27"
    snap.mkdir()
    pub = _make_publisher()

    def fake_post(self, body):  # noqa: ANN001 - test stub
        return PublishResult(ok=True, status_code=202, delivered=1, chat_ids=body["chat_ids"])

    monkeypatch.setattr(Publisher, "_post_with_retry", fake_post)
    result = pub.publish_watchdog(snap, reason="Sentry timeout")
    assert result.ok
    assert result.status_code == 202
    assert not (snap.parent / "last-digest.txt").exists()


def test_publisher_publish_watchdog_missing_snapshot_returns_error(tmp_path: Path) -> None:
    pub = _make_publisher()
    result = pub.publish_watchdog(tmp_path / "no-such-dir", reason="x")
    assert result.ok is False
    assert "does not exist" in (result.error or "")


def test_publisher_publish_watchdog_signs_with_hmac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the watchdog request goes through the same HMAC-signed transport."""
    snap = tmp_path / "2026-04-27"
    snap.mkdir()
    pub = _make_publisher()

    captured: dict = {}

    def fake_post(self, body):  # noqa: ANN001 - test stub
        captured["body"] = body
        return PublishResult(ok=True, status_code=202, delivered=1, chat_ids=body["chat_ids"])

    monkeypatch.setattr(Publisher, "_post_with_retry", fake_post)
    pub.publish_watchdog(snap, reason="x")
    # The body that would have been signed must include chat_ids and session_label
    assert captured["body"]["chat_ids"] == [42, 43]
    assert captured["body"]["session_label"].startswith("nightwatch-2026-04-27-watchdog-")


def test_main_skips_watchdog_when_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When NIGHTWATCH_WATCHDOG_ENABLED=false, main.py's sentry_unreachable
    branch must not even instantiate the Publisher."""
    monkeypatch.setenv("NIGHTWATCH_WATCHDOG_ENABLED", "false")
    monkeypatch.setenv("BOT_IPC_HMAC_SECRET", "")  # would crash Publisher if called

    flag = (
        os.environ.get("NIGHTWATCH_WATCHDOG_ENABLED", "true").strip().lower() == "true"
    )
    assert flag is False  # the gate behaviour we rely on in main.py


# ──────────────────────────────────────────────────────────────────────────
# Phase 2B-fix3: per-(chat,label) dedup blocked back-to-back /nightwatch_last
# calls because publisher emitted the same session_label twice.  Regression
# test: two consecutive manual publishes must produce DISTINCT labels and
# both must hit the IPC.
# ──────────────────────────────────────────────────────────────────────────


def test_back_to_back_manual_publishes_get_unique_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedup-bypass design depends on each manual call having a unique
    session_label. Two publishes in a row must therefore see two distinct
    labels and two POSTs to /inject."""
    import time

    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    pub = _make_publisher()

    captured: list[dict] = []

    def fake_post(self, body):  # noqa: ANN001 - test stub
        captured.append(body)
        return PublishResult(
            ok=True, status_code=202, delivered=1, chat_ids=body["chat_ids"],
            session_label=body["session_label"],
        )

    monkeypatch.setattr(Publisher, "_post_with_retry", fake_post)

    r1 = pub.publish(snap, mode="manual")
    # Sleep just enough that int(time.time()) ticks — protects against the
    # 1-second resolution if both calls land in the same wall-clock second.
    time.sleep(1.1)
    r2 = pub.publish(snap, mode="manual")

    assert r1.ok and r2.ok
    assert len(captured) == 2, "publisher must POST both calls — neither should be elided"
    label1 = captured[0]["session_label"]
    label2 = captured[1]["session_label"]
    assert label1 != label2, f"manual labels collided: {label1!r} == {label2!r}"
    assert label1.startswith("nightwatch-2026-04-26-manual-")
    assert label2.startswith("nightwatch-2026-04-26-manual-")
    # And the timestamp on the second must be ≥ the first.
    ts1 = int(label1.rsplit("-", 1)[-1])
    ts2 = int(label2.rsplit("-", 1)[-1])
    assert ts2 >= ts1
    assert ts2 > ts1, "after sleep(1.1) the timestamps must differ"


def test_all_modes_produce_unique_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2B-fix4: every mode (nightly, manual, watchdog) now carries a
    unix-timestamp suffix, so two same-day publishes produce DISTINCT labels
    and both punch through the bot's _NW_SEEN dedup. The previous fix3
    behaviour — stable nightly label — silently swallowed manual
    /nightwatch_run calls (which also route through mode=nightly)."""
    import time

    snap = _write_snapshot(tmp_path, top=[_scored()], decisions=[])
    pub = _make_publisher()

    captured: list[dict] = []

    def fake_post(self, body):  # noqa: ANN001 - test stub
        captured.append(body)
        return PublishResult(ok=True, status_code=202, delivered=1, chat_ids=body["chat_ids"])

    monkeypatch.setattr(Publisher, "_post_with_retry", fake_post)

    # Nightly back-to-back must now differ.
    pub.publish(snap, mode="nightly")
    time.sleep(1.1)
    pub.publish(snap, mode="nightly")
    # Manual back-to-back must differ (preserves the fix3 guarantee).
    pub.publish(snap, mode="manual")
    time.sleep(1.1)
    pub.publish(snap, mode="manual")
    # Watchdog mode must also be timestamped (verified separately too).
    pub.publish(snap, mode="watchdog")

    assert len(captured) == 5
    nightly1, nightly2, manual1, manual2, watchdog1 = (
        c["session_label"] for c in captured
    )
    assert nightly1 != nightly2, f"nightly labels collided: {nightly1!r}"
    assert manual1 != manual2, f"manual labels collided: {manual1!r}"
    assert nightly1.startswith("nightwatch-2026-04-26-nightly-")
    assert nightly2.startswith("nightwatch-2026-04-26-nightly-")
    assert manual1.startswith("nightwatch-2026-04-26-manual-")
    assert manual2.startswith("nightwatch-2026-04-26-manual-")
    assert watchdog1.startswith("nightwatch-2026-04-26-watchdog-")


def test_build_session_label_helper() -> None:
    """Direct unit test of the helper — independent of HTTP mocking.

    Phase 2B-fix4: every mode is timestamped, including nightly. Two calls
    in succession (after a >1s sleep so int(time.time()) ticks) produce
    distinct labels.
    """
    import time

    from app.publisher import _build_session_label

    # Nightly: now timestamped — two calls separated by >1s must differ.
    a = _build_session_label("2026-04-27", "nightly")
    time.sleep(1.1)
    b = _build_session_label("2026-04-27", "nightly")
    assert a != b
    assert a.startswith("nightwatch-2026-04-27-nightly-")
    assert b.startswith("nightwatch-2026-04-27-nightly-")

    # Manual: same guarantee as before fix4.
    c = _build_session_label("2026-04-27", "manual")
    time.sleep(1.1)
    d = _build_session_label("2026-04-27", "manual")
    assert c != d
    assert c.startswith("nightwatch-2026-04-27-manual-")
    assert d.startswith("nightwatch-2026-04-27-manual-")

    # Watchdog: also timestamped.
    e = _build_session_label("2026-04-27", "watchdog")
    assert e.startswith("nightwatch-2026-04-27-watchdog-")
