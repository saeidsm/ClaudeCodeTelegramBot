"""Logging filters used by bot.py.

Kept in a separate module so tests can import the filter without paying
bot.py's top-level cost (env reads, FS checks, big import block).
"""

from __future__ import annotations

import logging
import re

# Compiled once. Matches the bot-token segment of any Telegram Bot API URL:
# /bot<digits-and-letters>:<more>/  →  /bot<redacted>/
_TOKEN_URL_RE = re.compile(r"/bot[A-Za-z0-9_:-]+/")


class TokenRedactFilter(logging.Filter):
    """Replace /bot<TOKEN>/ → /bot<redacted>/ in any LogRecord's message.

    python-telegram-bot (and httpx underneath it) log full request URLs
    at INFO/DEBUG, which include the bot token. Without this filter,
    every nightly run leaks the bot token into telegram-bot.log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 — never block log emission
            return True
        if "/bot" in rendered:
            redacted = _TOKEN_URL_RE.sub("/bot<redacted>/", rendered)
            if redacted != rendered:
                # Pin the rendered text and clear args so downstream
                # formatters don't try to re-format with the original
                # placeholders (which the redacted text no longer carries).
                record.msg = redacted
                record.args = None
        return True


def install_token_redact_filter(logger: logging.Logger | None = None) -> None:
    """Attach a TokenRedactFilter to every handler on `logger` (default: root).

    Records from named loggers ('telegram', 'httpx', 'http.client') propagate
    up to root and pass through root's handlers — but NOT through parent
    loggers' filters. Attaching at the handler level catches every record
    on its way to a sink, regardless of which logger originated it.

    Idempotent: re-runs detect the existing filter and skip.
    """
    target = logger if logger is not None else logging.getLogger()
    redactor = TokenRedactFilter()
    for h in target.handlers:
        if not any(isinstance(f, TokenRedactFilter) for f in h.filters):
            h.addFilter(redactor)
