"""Tests for the Publisher webroot bridge (Bug 1 fix, 2026-04-28).

Phase 2B shipped a digest publisher that POSTed to the bot but never
mirrored the snapshot directory into the token-gated webroot. The
"📊 Full Report" button URL therefore 404'd. These tests pin down the
new behaviour:

  • Publisher.publish() bridges snapshot_dir → NIGHTWATCH_WEBROOT_DIR/
    nightwatch-{date}/ AFTER the IPC POST returns 202 and BEFORE
    last-digest.txt is updated.
  • The copy is atomic via copytree-to-tmp + rename.
  • If NIGHTWATCH_WEBROOT_DIR is empty, the bridge is a no-op (warning
    only) and the digest is still delivered (back-compat).
  • If the bridge fails (disk full, permission), the digest still
    succeeds — the user can SSH and read the file.
  • Button URLs use NIGHTWATCH_REPORT_TOKEN when set:
        {BASE}/{TOKEN}/nightwatch-{date}/
    Falling back to {BASE}/nightwatch-{date}/ when unset.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.publisher import Publisher


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


def _write_snapshot(snap_dir: Path, date_str: str = "2026-04-27") -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "evidence").mkdir(exist_ok=True)
    (snap_dir / "summary.json").write_text(
        json.dumps({
            "date": date_str,
            "status": "ok",
            "issue_count": 3,
            "cluster_count": 0,
            "decision_count": 0,
            "by_level": {"error": 3},
            "by_project": {"shahrzad-backend": 3},
            "top10_summary": [],
        }),
        encoding="utf-8",
    )
    (snap_dir / "top_issues.json").write_text(
        json.dumps([{
            "issue_id": "1",
            "project_slug": "shahrzad-backend",
            "title": "BridgeTestError",
            "level": "error",
            "count": 5,
            "user_count": 1,
            "first_seen": f"{date_str}T01:00:00Z",
            "last_seen":  f"{date_str}T18:00:00Z",
            "release": None,
            "permalink": None,
            "is_new": False,
            "is_regression": False,
            "is_spike": False,
            "is_release_correlated": False,
            "is_user_impacting": False,
            "in_cluster": False,
            "severity_score": 30,
            "reasons": [],
        }]),
        encoding="utf-8",
    )
    (snap_dir / "decisions.json").write_text("[]", encoding="utf-8")
    (snap_dir / "clusters.json").write_text("[]", encoding="utf-8")
    (snap_dir / "analysis.md").write_text(
        f"# NightWatch — {date_str}\n", encoding="utf-8"
    )
    (snap_dir / "prompt.md").write_text("# placeholder\n", encoding="utf-8")
    (snap_dir / "issues.csv").write_text("issue_id\n1\n", encoding="utf-8")
    (snap_dir / "report.zip").write_bytes(b"PK\x03\x04fake-zip")
    (snap_dir / "evidence" / "1.json").write_text(
        json.dumps({"event": "fake"}), encoding="utf-8"
    )


def _make_publisher(**overrides) -> Publisher:
    kwargs = dict(
        bot_url="http://127.0.0.1:9091",
        hmac_secret="test-secret-32-bytes-aaaaaaaaaaaa",
        chat_ids=[42],
        report_base_url="https://example.com/reports",
        sentry_org_url="https://example.sentry.io",
    )
    kwargs.update(overrides)
    return Publisher(**kwargs)


def _patch_post_ok(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub httpx.post so it always returns 202. Returns a dict the test can
    inspect for the captured request body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            202, json={"ok": True, "delivered": 1, "duplicates": 0, "failed": 0}
        )

    transport = httpx.MockTransport(handler)

    def patched_post(url, *args, **kwargs):  # noqa: ANN001
        with httpx.Client(transport=transport) as client:
            return client.post(url, *args, **kwargs)

    monkeypatch.setattr(httpx, "post", patched_post)
    return captured


# ──────────────────────────────────────────────────────────────────────────
# Bridge happy path
# ──────────────────────────────────────────────────────────────────────────


def test_publish_bridges_snapshot_into_webroot_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = tmp_path / "snapshots" / "2026-04-27"
    _write_snapshot(snap, "2026-04-27")
    webroot = tmp_path / "webroot"
    webroot.mkdir()

    monkeypatch.setenv("NIGHTWATCH_WEBROOT_DIR", str(webroot))
    monkeypatch.delenv("NIGHTWATCH_REPORT_TOKEN", raising=False)
    _patch_post_ok(monkeypatch)

    pub = _make_publisher()
    result = pub.publish(snap, mode="manual")

    assert result.ok
    target = webroot / "nightwatch-2026-04-27"
    assert target.is_dir(), "bridged dir must exist after successful publish"
    for fname in (
        "summary.json", "top_issues.json", "clusters.json", "decisions.json",
        "analysis.md", "issues.csv", "report.zip",
    ):
        assert (target / fname).exists(), f"missing in webroot: {fname}"
    assert (target / "evidence" / "1.json").exists()
    # An index.html is generated for browseable access.
    index = target / "index.html"
    assert index.exists()
    assert "BridgeTestError" in index.read_text(encoding="utf-8")


def test_publish_bridge_sets_world_readable_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caddy runs in a container as a different uid — bridged dir must be
    world-readable so the report URL serves correctly."""
    snap = tmp_path / "snapshots" / "2026-04-27"
    _write_snapshot(snap, "2026-04-27")
    webroot = tmp_path / "webroot"
    webroot.mkdir()

    monkeypatch.setenv("NIGHTWATCH_WEBROOT_DIR", str(webroot))
    _patch_post_ok(monkeypatch)

    _make_publisher().publish(snap, mode="manual")

    target = webroot / "nightwatch-2026-04-27"
    # Directory: at least a+rx (mode bits & 0o005 == 0o005).
    assert (target.stat().st_mode & 0o005) == 0o005, "bridged dir not world-readable"
    # A representative file: at least a+r.
    f = target / "summary.json"
    assert (f.stat().st_mode & 0o004) == 0o004, "bridged file not world-readable"


def test_publish_bridge_overwrites_existing_target_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A republish (or manual re-bridge) must replace stale contents."""
    snap = tmp_path / "snapshots" / "2026-04-27"
    _write_snapshot(snap, "2026-04-27")
    webroot = tmp_path / "webroot"
    webroot.mkdir()

    # Plant a stale target with a sentinel file that should NOT survive.
    stale = webroot / "nightwatch-2026-04-27"
    stale.mkdir()
    (stale / "stale-marker.txt").write_text("OLD", encoding="utf-8")

    monkeypatch.setenv("NIGHTWATCH_WEBROOT_DIR", str(webroot))
    _patch_post_ok(monkeypatch)

    result = _make_publisher().publish(snap, mode="manual")
    assert result.ok
    assert not (stale / "stale-marker.txt").exists(), "stale file must be removed"
    assert (stale / "summary.json").exists(), "fresh content must be in place"


# ──────────────────────────────────────────────────────────────────────────
# Bridge skip / failure
# ──────────────────────────────────────────────────────────────────────────


def test_publish_skips_bridge_when_webroot_dir_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var → no-op + log warning; digest must still deliver."""
    snap = tmp_path / "snapshots" / "2026-04-27"
    _write_snapshot(snap, "2026-04-27")

    monkeypatch.delenv("NIGHTWATCH_WEBROOT_DIR", raising=False)
    _patch_post_ok(monkeypatch)

    result = _make_publisher().publish(snap, mode="manual")
    assert result.ok, "digest must deliver even without webroot configured"


def test_publish_bridge_failure_does_not_block_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk full / permission error during bridge → digest still delivers."""
    snap = tmp_path / "snapshots" / "2026-04-27"
    _write_snapshot(snap, "2026-04-27")
    webroot = tmp_path / "webroot"
    webroot.mkdir()

    monkeypatch.setenv("NIGHTWATCH_WEBROOT_DIR", str(webroot))
    _patch_post_ok(monkeypatch)

    # Force the bridge step to fail.
    with patch(
        "app.publisher.shutil.copytree",
        side_effect=PermissionError("fake permission denied"),
    ):
        result = _make_publisher().publish(snap, mode="manual")

    assert result.ok, "digest delivery must survive bridge failure"
    # Target should NOT exist — bridge failed before atomic swap.
    assert not (webroot / "nightwatch-2026-04-27").exists()


def test_publish_bridges_before_recording_last_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec ordering: AFTER 202, bridge runs BEFORE last-digest.txt update."""
    snap = tmp_path / "snapshots" / "2026-04-27"
    _write_snapshot(snap, "2026-04-27")
    webroot = tmp_path / "webroot"
    webroot.mkdir()

    monkeypatch.setenv("NIGHTWATCH_WEBROOT_DIR", str(webroot))
    _patch_post_ok(monkeypatch)

    call_order: list[str] = []

    pub = _make_publisher()

    real_bridge = pub._bridge_to_webroot

    def spy_bridge(*a, **kw):
        call_order.append("bridge")
        return real_bridge(*a, **kw)

    real_record = pub._record_last_digest

    def spy_record(*a, **kw):
        call_order.append("record_last_digest")
        return real_record(*a, **kw)

    monkeypatch.setattr(pub, "_bridge_to_webroot", spy_bridge)
    monkeypatch.setattr(pub, "_record_last_digest", spy_record)

    result = pub.publish(snap, mode="manual")
    assert result.ok
    assert call_order == ["bridge", "record_last_digest"], (
        f"expected bridge before last-digest, got {call_order!r}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Token-aware URL construction
# ──────────────────────────────────────────────────────────────────────────


def test_button_url_inserts_token_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIGHTWATCH_REPORT_TOKEN", "deadbeef" * 8)
    pub = _make_publisher(report_base_url="https://example.com/reports")
    buttons = pub.build_buttons("2026-04-27")
    full_report = next(b for b in buttons if "Full Report" in b["text"])
    assert full_report["url"] == (
        "https://example.com/reports/" + "deadbeef" * 8 + "/nightwatch-2026-04-27/"
    )


def test_button_url_falls_back_when_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIGHTWATCH_REPORT_TOKEN", raising=False)
    pub = _make_publisher(report_base_url="https://example.com/reports")
    buttons = pub.build_buttons("2026-04-27")
    full_report = next(b for b in buttons if "Full Report" in b["text"])
    assert full_report["url"] == "https://example.com/reports/nightwatch-2026-04-27/"


def test_report_url_field_uses_token_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IPC body's `report_url` should match the button URL convention."""
    snap = tmp_path / "snapshots" / "2026-04-27"
    _write_snapshot(snap, "2026-04-27")
    monkeypatch.setenv("NIGHTWATCH_REPORT_TOKEN", "abc" * 21 + "x")  # 64 chars
    pub = _make_publisher(report_base_url="https://example.com/reports")
    body = pub.build_request_body(snap, mode="manual")
    assert body["report_url"].startswith("https://example.com/reports/")
    assert "/" + ("abc" * 21 + "x") + "/nightwatch-2026-04-27/" in body["report_url"]
