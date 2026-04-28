"""Live schema validation test.

Marked `@pytest.mark.live` and skipped by default. Run explicitly with:

    venv/bin/pytest tests/test_schema_live.py -m live -s

This test makes REAL HTTP calls to Sentry (≤6 calls per run), so it is gated
on the SENTRY_AUTH_TOKEN env var being set. It is NOT part of CI by default.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _skip_when_no_token() -> None:
    if not os.environ.get("SENTRY_AUTH_TOKEN"):
        pytest.skip("SENTRY_AUTH_TOKEN not set — skipping live schema check")
    if not os.environ.get("SENTRY_ORG_SLUG"):
        pytest.skip("SENTRY_ORG_SLUG not set — skipping live schema check")


def test_schema_check_runs_and_is_green(tmp_path: Path) -> None:
    """Run the live schema check and assert verdict is GREEN.

    Phase 1.5 patches (canonical event URL + release/env from event tags)
    should bring the live verdict from RED → GREEN. A non-GREEN verdict
    here means the pipeline has drifted again.
    """
    from app.schema_check import run_check

    report = tmp_path / "schema-report.md"
    sample = tmp_path / "live-sample.json"
    rc = run_check(report, sample)
    assert rc == 0, f"schema_check exited with rc={rc}"
    assert report.exists(), "report was not written"
    assert sample.exists(), "redacted sample was not written"

    text = report.read_text(encoding="utf-8")
    verdict_line = next(
        (
            ln
            for ln in text.splitlines()
            if ln.startswith("**RED**") or ln.startswith("**YELLOW**") or ln.startswith("**GREEN**")
        ),
        "",
    )
    assert verdict_line.startswith("**GREEN**"), (
        f"expected GREEN verdict; got {verdict_line!r}.\nReport at: {report}"
    )
