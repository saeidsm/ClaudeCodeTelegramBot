"""Integration-shaped test for the publisher.

Marked `live` and skipped in default CI to mirror tests/test_schema_live.py
discipline. Uses an httpx MockTransport (not real network) but exercises the
full Publisher.publish path end to end: build body → sign → POST → parse 202.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.publisher import Publisher

pytestmark = pytest.mark.live


def _summary_dict() -> dict:
    return {
        "date": "2026-04-26",
        "status": "ok",
        "issue_count": 5,
        "cluster_count": 0,
        "by_level": {"error": 5},
        "by_project": {"shahrzad-backend": 5},
        "top10_summary": [],
    }


def _make_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "2026-04-26"
    snap.mkdir()
    (snap / "summary.json").write_text(json.dumps(_summary_dict()), encoding="utf-8")
    (snap / "top_issues.json").write_text(
        json.dumps(
            [
                {
                    "issue_id": "1",
                    "project_slug": "shahrzad-backend",
                    "title": "TestError",
                    "level": "error",
                    "count": 5,
                    "user_count": 1,
                    "first_seen": "2026-04-26T01:00:00Z",
                    "last_seen": "2026-04-26T18:00:00Z",
                    "release": None,
                    "permalink": None,
                    "is_new": True,
                    "is_regression": False,
                    "is_spike": False,
                    "is_release_correlated": False,
                    "is_user_impacting": False,
                    "in_cluster": False,
                    "severity_score": 30,
                    "reasons": ["new"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (snap / "decisions.json").write_text("[]", encoding="utf-8")
    return snap


def test_publish_full_request_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the full HTTP shape sent by Publisher.publish.

    Uses httpx.MockTransport to capture the actual request without touching
    the network. Asserts the URL, headers (including the HMAC signature),
    and body shape match what the bot's `_nw_inject` expects.
    """
    snap = _make_snapshot(tmp_path)
    pub = Publisher(
        bot_url="http://127.0.0.1:9091",
        hmac_secret="integration-test-secret-32-bytes-aaaaaaaa",
        chat_ids=[42],
        report_base_url="https://example.com/reports",
        sentry_org_url="https://example.sentry.io",
    )

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(202, json={"ok": True, "delivered": 1, "duplicates": 0, "failed": 0})

    transport = httpx.MockTransport(handler)
    real_post = httpx.post

    def patched_post(url, *args, **kwargs):  # noqa: ANN001 - test stub
        with httpx.Client(transport=transport) as client:
            return client.post(url, *args, **kwargs)

    monkeypatch.setattr(httpx, "post", patched_post)

    result = pub.publish(snap, mode="nightly")

    assert result.ok
    assert result.status_code == 202
    assert result.delivered == 1
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:9091/inject"
    assert captured["headers"]["content-type"] == "application/json"
    assert "x-nightwatch-signature" in captured["headers"]
    assert len(captured["headers"]["x-nightwatch-signature"]) == 64  # sha256 hex

    body = captured["body"]
    assert body["session_label"] == "nightwatch-2026-04-26-nightly"
    assert body["chat_ids"] == [42]
    assert "<b>NightWatch</b>" in body["message_html"]
    assert len(body["buttons"]) == 3
    assert body["report_url"] == "https://example.com/reports/nightwatch-2026-04-26/"

    # Restore.
    monkeypatch.setattr(httpx, "post", real_post)
