"""Blocking finding 2 — the FINAL chunk of a long reply gets the footer.

Exercises the real send_long() flow (not only resource_footer.with_footer):
short, near-limit, multi-chunk HTML, multi-chunk plain, emoji, and HTML
parse-fallback. In every case exactly one footer lands on the final delivered
text, none on earlier chunks, every delivered chunk stays within Telegram's
UTF-16 limit, reply markup rides only the final chunk, and the central bot
layer adds no duplicate (send_long suppresses it).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import resource_footer as rf
from telegram.constants import ParseMode
from telegram.error import BadRequest

_LIMIT = 4096


@pytest.fixture(autouse=True)
def _fresh_footer(monkeypatch):
    rf._reset_cache_for_test()
    monkeypatch.setattr(rf, "_compute", lambda: "🖥 RAM 1G/2G · Swap 0B/0B · Disk 3G/4G")
    # no real 0.3s inter-chunk sleeps
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(bot.asyncio, "sleep", _no_sleep)
    yield
    rf._reset_cache_for_test()


class FakeMessage:
    """reply_text that routes through the central footer layer (like production:
    Message.reply_text -> bot.send_message -> FooterBot._footerize) so we also
    prove there is no duplicate footer from that layer."""

    def __init__(self, fail_html_once=False):
        self.delivered = []          # (text, reply_markup, parse_mode)
        self._fail_html_once = fail_html_once

    async def reply_text(self, text, parse_mode=None, reply_markup=None,
                         disable_web_page_preview=None):
        if self._fail_html_once and parse_mode == ParseMode.HTML:
            self._fail_html_once = False
            raise BadRequest("Can't parse entities: unexpected end tag")
        # central layer (FooterBot) runs on every real send:
        text = bot._footerize(text, parse_mode)
        self.delivered.append((text, reply_markup, parse_mode))
        return "sent"


def _run(msg, text, **kw):
    asyncio.run(bot.send_long(msg, text, **kw))


def _footer_counts(msg):
    return [t.count(rf.FOOTER_MARK) for (t, _rm, _pm) in msg.delivered]


def _assert_within_limit(msg):
    for (t, _rm, _pm) in msg.delivered:
        assert rf.tg_len(t) <= _LIMIT, f"chunk exceeds limit: {rf.tg_len(t)}"


def test_short_html_one_footer():
    m = FakeMessage()
    _run(m, "hello world")
    assert len(m.delivered) == 1
    assert _footer_counts(m) == [1]
    _assert_within_limit(m)


def test_near_limit_html_splits_and_final_footer():
    # ~4096 units of HTML-safe text forces a split; footer must still land once.
    m = FakeMessage()
    _run(m, "x" * 4096)
    counts = _footer_counts(m)
    assert len(m.delivered) >= 2
    assert sum(counts) == 1
    assert counts[-1] == 1
    assert all(c == 0 for c in counts[:-1])
    _assert_within_limit(m)


def test_multi_chunk_html_one_footer_on_final():
    m = FakeMessage()
    body = "\n".join(f"line {i} " + "y" * 80 for i in range(200))  # >> 4096
    _run(m, body)
    counts = _footer_counts(m)
    assert len(m.delivered) >= 3
    assert sum(counts) == 1 and counts[-1] == 1
    _assert_within_limit(m)


def test_multi_chunk_plain_one_footer_on_final():
    m = FakeMessage()
    body = ("word " * 3000)
    _run(m, body, pm=None)
    counts = _footer_counts(m)
    assert len(m.delivered) >= 2
    assert sum(counts) == 1 and counts[-1] == 1
    # plain footer has no <code> wrapper
    assert "<code>" not in m.delivered[-1][0]
    _assert_within_limit(m)


def test_emoji_multi_chunk_no_surrogate_split_and_footer():
    m = FakeMessage()
    payload = "😀" * 5000                    # 10000 UTF-16 units (non-BMP, != footer mark)
    _run(m, payload, pm=None)
    counts = _footer_counts(m)
    assert sum(counts) == 1 and counts[-1] == 1
    _assert_within_limit(m)
    # no surrogate was split: every delivered chunk round-trips through utf-16
    for (t, _r, _p) in m.delivered:
        t.encode("utf-16-le").decode("utf-16-le")
    # lossless reconstruction of the payload (strip the one appended footer block)
    joined = "".join(t for (t, _r, _p) in m.delivered)
    assert payload in joined.replace("\n\n" + rf.footer_text(), "")


def test_reply_markup_only_on_final_chunk():
    m = FakeMessage()
    marker = object()
    body = "\n".join("z" * 200 for _ in range(60))
    _run(m, body, rm=marker)
    markups = [rm for (_t, rm, _pm) in m.delivered]
    assert markups[-1] is marker
    assert all(rm is None for rm in markups[:-1])


def test_html_parse_fallback_keeps_footer_on_final_text():
    # The final chunk fails HTML parsing once; send_long retries it as plain.
    # The delivered plain text must still carry exactly one footer (mark survives
    # tag stripping because the footer text itself is not a tag).
    m = FakeMessage(fail_html_once=True)
    _run(m, "<b>hello</b> world")
    assert len(m.delivered) == 1
    assert m.delivered[-1][2] is None                 # delivered as plain
    assert m.delivered[-1][0].count(rf.FOOTER_MARK) == 1
    _assert_within_limit(m)


def test_no_duplicate_from_central_layer():
    # send_long footers the final chunk itself AND suppresses the central layer.
    # If suppression regressed, _footerize in FakeMessage.reply_text would add a
    # second footer -> count 2. Guard against that.
    m = FakeMessage()
    _run(m, "single short message")
    assert _footer_counts(m) == [1]
