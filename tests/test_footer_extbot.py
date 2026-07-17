"""Blocking finding 1 — footer wiring on the REAL frozen ExtBot.

The FakeBot tests in test_footer_coverage.py exercise the mutable-double path.
These tests use the actually-installed python-telegram-bot ExtBot / Application
construction path (valid-shaped fake token, no network) to prove:

  * the real ExtBot rejects the old instance monkey-patch (the startup crash);
  * FooterBot (ExtBot subclass) + ApplicationBuilder.bot() constructs cleanly;
  * send_message / edit_message_text stay callable and append exactly one
    footer, idempotently, honouring the suppression contextvar;
  * the supported PTB major range is present.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import telegram
from telegram.ext import Application, ExtBot
from telegram.request import HTTPXRequest
from telegram.constants import ParseMode

import bot
import resource_footer as rf

# Syntactically valid but non-real token; no network call is ever made.
FAKE_TOKEN = "123456:AAHfake-token-for-offline-construction-only_xxxx"


@pytest.fixture(autouse=True)
def _fresh_footer(monkeypatch):
    rf._reset_cache_for_test()
    monkeypatch.setattr(rf, "_compute", lambda: "🖥 RAM 1G/2G · Swap 0B/0B · Disk 3G/4G")
    yield
    rf._reset_cache_for_test()


def test_ptb_major_is_supported():
    # Behaviour here depends on ExtBot being frozen/slotted (>=20). requirements
    # pins >=21.0; bound the upper edge we have validated against.
    major = int(telegram.__version__.split(".")[0])
    assert 21 <= major <= 22, f"untested python-telegram-bot major {telegram.__version__}"


def test_real_extbot_rejects_instance_patch():
    # This is the crash the finding is about: the old install_send_footer path
    # assigns bot.send_message, which the frozen ExtBot refuses.
    real = ExtBot(FAKE_TOKEN)
    with pytest.raises(AttributeError):
        bot.install_send_footer(real)


def _build_app():
    """Mirror main()'s construction path, offline."""
    footer_bot = bot.FooterBot(
        FAKE_TOKEN,
        request=HTTPXRequest(read_timeout=60, write_timeout=60,
                             connect_timeout=30, pool_timeout=30),
        get_updates_request=HTTPXRequest(connection_pool_size=1),
    )
    return Application.builder().bot(footer_bot).build()


def test_application_construction_and_wiring_do_not_raise():
    app = _build_app()
    assert isinstance(app.bot, bot.FooterBot)
    assert isinstance(app.bot, ExtBot)            # still a real ExtBot
    assert callable(app.bot.send_message)
    assert callable(app.bot.edit_message_text)


def test_footerbot_appends_one_footer_idempotently(monkeypatch):
    app = _build_app()
    sent, edited = [], []

    async def rec_send(self, chat_id, text=None, *a, **k):
        sent.append(text)
        return "m"

    async def rec_edit(self, text=None, *a, **k):
        edited.append(text)
        return "m"

    # super().send_message inside FooterBot resolves to ExtBot.send_message.
    monkeypatch.setattr(ExtBot, "send_message", rec_send)
    monkeypatch.setattr(ExtBot, "edit_message_text", rec_edit)

    async def run():
        await app.bot.send_message(1, "hello", parse_mode=ParseMode.HTML)
        await app.bot.edit_message_text("edited", parse_mode=ParseMode.HTML)
        # already-footered text is not doubled
        already = f"hi\n\n<code>{rf.FOOTER_MARK} x</code>"
        await app.bot.send_message(1, already, parse_mode=ParseMode.HTML)

    asyncio.run(run())
    assert sent[0].count(rf.FOOTER_MARK) == 1
    assert rf.FOOTER_MARK in edited[0]
    assert sent[1].count(rf.FOOTER_MARK) == 1        # idempotent


def test_footerbot_honours_suppression(monkeypatch):
    app = _build_app()
    sent = []

    async def rec_send(self, chat_id, text=None, *a, **k):
        sent.append(text)
        return "m"

    monkeypatch.setattr(ExtBot, "send_message", rec_send)

    async def run():
        tok = bot._FOOTER_SUPPRESS.set(True)
        try:
            await app.bot.send_message(1, "quiet", parse_mode=ParseMode.HTML)
        finally:
            bot._FOOTER_SUPPRESS.reset(tok)

    asyncio.run(run())
    assert rf.FOOTER_MARK not in sent[0]
