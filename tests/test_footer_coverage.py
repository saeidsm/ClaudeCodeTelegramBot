"""Central footer coverage + UTF-16 boundary correctness (Correction 5)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import resource_footer as rf
from telegram.constants import ParseMode


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id=None, text=None, *a, **k):
        self.sent.append(text)
        return "m"

    async def edit_message_text(self, text=None, *a, **k):
        self.edited.append(text)
        return "m"


@pytest.fixture(autouse=True)
def _fresh_footer(monkeypatch):
    rf._reset_cache_for_test()
    monkeypatch.setattr(rf, "_compute", lambda: "🖥 RAM 1G/2G · Swap 0B/0B · Disk 3G/4G")
    yield
    rf._reset_cache_for_test()


def _fresh_bot():
    b = FakeBot()
    bot.install_send_footer(b)
    return b


def test_send_message_gets_one_footer():
    b = _fresh_bot()
    asyncio.run(b.send_message(1, "hello", parse_mode=ParseMode.HTML))
    assert b.sent[-1].count(rf.FOOTER_MARK) == 1


def test_edit_message_text_gets_footer():
    b = _fresh_bot()
    asyncio.run(b.edit_message_text("edited", parse_mode=ParseMode.HTML))
    assert rf.FOOTER_MARK in b.edited[-1]


def test_plain_message_gets_plain_footer():
    b = _fresh_bot()
    asyncio.run(b.send_message(1, "plain", parse_mode=None))
    assert rf.FOOTER_MARK in b.sent[-1]
    assert "<code>" not in b.sent[-1]          # plain, not HTML


def test_markdown_message_is_left_alone():
    b = _fresh_bot()
    asyncio.run(b.send_message(1, "**md**", parse_mode="MarkdownV2"))
    assert rf.FOOTER_MARK not in b.sent[-1]


def test_empty_text_untouched():
    b = _fresh_bot()
    asyncio.run(b.send_message(1, None, parse_mode=ParseMode.HTML))
    assert b.sent[-1] is None


def test_idempotent_when_footer_already_present():
    b = _fresh_bot()
    already = f"hi\n\n<code>{rf.FOOTER_MARK} x</code>"
    asyncio.run(b.send_message(1, already, parse_mode=ParseMode.HTML))
    assert b.sent[-1].count(rf.FOOTER_MARK) == 1   # not doubled


def test_suppression_contextvar():
    b = _fresh_bot()
    tok = bot._FOOTER_SUPPRESS.set(True)
    try:
        asyncio.run(b.send_message(1, "quiet", parse_mode=ParseMode.HTML))
    finally:
        bot._FOOTER_SUPPRESS.reset(tok)
    assert rf.FOOTER_MARK not in b.sent[-1]


# ── UTF-16 boundary correctness ────────────────────────────────────────────
def test_tg_len_counts_utf16_units():
    assert rf.tg_len("abc") == 3
    assert rf.tg_len("🖥") == 2          # non-BMP emoji = 2 UTF-16 units
    assert rf.tg_len("a🖥b") == 4


def test_utf16_prefix_len_never_splits_surrogate():
    s = "🖥" * 10                          # each is 2 UTF-16 units
    i = bot._utf16_prefix_len(s, 5)        # budget 5 units -> 2 whole emoji (4 units)
    assert i == 2
    assert rf.tg_len(s[:i]) <= 5


def test_plain_split_respects_utf16_limit():
    s = "🖥" * 5000                         # 10000 UTF-16 units
    chunks = bot._split_plain_utf16(s, 4000)
    assert all(rf.tg_len(c) <= 4000 for c in chunks)
    assert "".join(chunks) == s            # lossless


def test_html_split_respects_utf16_limit():
    s = ("x" * 100 + "🖥" * 2000)           # mixed BMP + emoji
    chunks = bot._split_html_chunks(s, 4000)
    assert all(rf.tg_len(c) <= 4000 for c in chunks)


def test_with_footer_length_aware_utf16():
    # a message already near the UTF-16 limit must NOT get a footer appended
    big = "🖥" * 2047                        # ~4094 UTF-16 units
    out = rf.with_footer(big)
    assert out == big                        # no footer (would overflow)
