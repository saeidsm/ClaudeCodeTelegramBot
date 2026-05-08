"""Tests for log_filters.py — bot token redaction in logging."""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

# Make log_filters importable when running pytest from the repo root or tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from log_filters import (  # noqa: E402
    TokenRedactFilter,
    install_token_redact_filter,
)


def test_filter_redacts_typical_python_telegram_bot_url() -> None:
    """python-telegram-bot logs URLs like:
        POST https://api.telegram.org/bot8636153276:AAEPFm.../sendMessage
    The filter must rewrite that segment without breaking the rest of the line.
    """
    record = logging.LogRecord(
        name="telegram",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=(
            "POST https://api.telegram.org/bot8636153276:AAEPFmLNVQ_lVw-J1mwWpUVcWwMB-j7yaM0/sendMessage"
            " HTTP/1.1 200"
        ),
        args=None,
        exc_info=None,
    )
    TokenRedactFilter().filter(record)
    rendered = record.getMessage()
    assert "AAEPFmLNVQ_lVw-J1mwWpUVcWwMB-j7yaM0" not in rendered
    assert "8636153276" not in rendered
    assert "/bot<redacted>/" in rendered
    # Surrounding context preserved
    assert "POST https://api.telegram.org" in rendered
    assert "/sendMessage HTTP/1.1 200" in rendered


def test_filter_persists_redaction_through_full_log_pipeline() -> None:
    """End-to-end: install on a logger's handler, log a tokenized URL via
    a CHILD logger, and verify the persisted log line is redacted.

    Models the production setup: bot.py installs on root's handlers; the
    library logs via 'telegram'/'httpx' and propagates up.
    """
    root = logging.getLogger()

    # Capture into an in-memory stream
    buf = io.StringIO()
    capture_handler = logging.StreamHandler(buf)
    capture_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(capture_handler)
    original_level = root.level
    root.setLevel(logging.INFO)

    try:
        install_token_redact_filter()

        # Log via a CHILD logger (the library pattern)
        child = logging.getLogger("httpx")
        child.info(
            "HTTP Request: POST https://api.telegram.org/bot1234:secret-token-xyz/getMe"
        )

        out = buf.getvalue()
        assert "secret-token-xyz" not in out
        assert "1234:secret-token-xyz" not in out
        assert "/bot<redacted>/" in out
        # Format-string args also work (regression: filter must handle args)
        buf.truncate(0)
        buf.seek(0)
        child.info("API: %s", "https://api.telegram.org/botABC:DEF/sendMessage")
        out = buf.getvalue()
        assert "ABC:DEF" not in out
        assert "/bot<redacted>/" in out
    finally:
        # Cleanup: remove our capture handler + redactor + restore level.
        root.removeHandler(capture_handler)
        for h in list(root.handlers):
            for f in list(h.filters):
                if isinstance(f, TokenRedactFilter):
                    h.removeFilter(f)
        root.setLevel(original_level)


def test_install_is_idempotent() -> None:
    """Calling install_token_redact_filter twice must not duplicate filters."""
    root = logging.getLogger()
    capture = logging.StreamHandler(io.StringIO())
    root.addHandler(capture)
    try:
        install_token_redact_filter()
        install_token_redact_filter()
        install_token_redact_filter()
        n_redactors_on_capture = sum(
            1 for f in capture.filters if isinstance(f, TokenRedactFilter)
        )
        assert n_redactors_on_capture == 1
    finally:
        root.removeHandler(capture)
        for h in list(root.handlers):
            for f in list(h.filters):
                if isinstance(f, TokenRedactFilter):
                    h.removeFilter(f)
