"""Conversation FSM for /video — collect brand, template, aspect, files, text.

States are integers (ConversationHandler convention). State data lives on
context.user_data["video_state"]; a snapshot is mirrored to JobStore for
crash recovery.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from . import assets
from .jobs import JobStore

log = logging.getLogger(__name__)

(
    S_BRAND, S_TEMPLATE, S_ASPECT, S_LOGO, S_PRODUCTS,
    S_HEADLINE, S_SUBHEAD, S_PRICE, S_CTA, S_DURATION,
    S_MUSIC, S_NARRATION_CHOICE, S_NARRATION_TEXT, S_CONFIRM,
) = range(14)


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def _assets_root() -> Path:
    return Path(os.environ.get("REMOTION_ASSETS_DIR", "/tmp/remotion-assets"))


def _jobs_root() -> Path:
    p = Path("/tmp/video-jobs")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    JobStore(_jobs_root()).save(chat_id, dict(ctx.user_data.get("video_state", {})))


def _wipe(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    ctx.user_data.pop("video_state", None)
    JobStore(_jobs_root()).delete(chat_id)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    brands = assets.list_brands(_assets_root())
    if not brands:
        await update.message.reply_text("هیچ برندی در /tmp/remotion-assets پیدا نشد.")
        return ConversationHandler.END
    ctx.user_data["video_state"] = {}
    rows = [[(b, f"brand:{b}")] for b in brands]
    await update.message.reply_text("🎬 برند را انتخاب کن:", reply_markup=_kb(rows))
    return S_BRAND


async def on_brand(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    brand = q.data.split(":", 1)[1]
    ctx.user_data["video_state"]["brand"] = brand
    _save(ctx, q.message.chat_id)
    await q.edit_message_text(
        f"برند: <b>{brand}</b>\nتمپلیت؟",
        parse_mode="HTML",
        reply_markup=_kb([
            [("🎯 Product Promo", "tpl:ProductPromo")],
            [("🏗 Service / Project Promo", "tpl:ServicePromo")],
            [("✨ Custom (Claude Code)", "tpl:Custom")],
        ]),
    )
    return S_TEMPLATE


async def on_template(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    tpl = q.data.split(":", 1)[1]
    if tpl == "Custom":
        await q.edit_message_text(
            "حالت Custom: متن آزاد بفرست (در پاسخ همین پیام). من اون رو به "
            "session پروژه video در Claude Code می‌فرستم."
        )
        _wipe(ctx, q.message.chat_id)
        return ConversationHandler.END
    ctx.user_data["video_state"]["template"] = tpl
    _save(ctx, q.message.chat_id)
    await q.edit_message_text(
        "نسبت تصویر؟",
        reply_markup=_kb([
            [("📱 9:16 عمودی", "asp:9:16")],
            [("🖥 16:9 افقی",  "asp:16:9")],
        ]),
    )
    return S_ASPECT


async def on_aspect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    aspect = q.data.split(":", 1)[1]
    ctx.user_data["video_state"]["aspect"] = aspect
    _save(ctx, q.message.chat_id)
    brand = ctx.user_data["video_state"]["brand"]
    logos = assets.list_assets(_assets_root(), brand, "logo")
    if not logos:
        await q.edit_message_text(f"لوگویی در {brand}/logo/ پیدا نشد. /cancel یا فایل اضافه کن.")
        return ConversationHandler.END
    rows = [[(f, f"logo:{f}")] for f in logos]
    await q.edit_message_text("لوگو را انتخاب کن:", reply_markup=_kb(rows))
    return S_LOGO


async def on_logo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    filename = q.data.split(":", 1)[1]
    ctx.user_data["video_state"]["logo_file"] = filename
    _save(ctx, q.message.chat_id)
    brand = ctx.user_data["video_state"]["brand"]
    products = assets.list_assets(_assets_root(), brand, "products")
    rows = [[(f, f"prod:{f}")] for f in products]
    rows.append([("✅ انتخاب کافیه", "prod_done")])
    ctx.user_data["video_state"]["product_files"] = []
    await q.edit_message_text(
        "محصول(ها) — می‌تونی چند تا پشت سر هم انتخاب کنی، بعد «انتخاب کافیه».",
        reply_markup=_kb(rows),
    )
    return S_PRODUCTS


async def on_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    chosen = ctx.user_data["video_state"].setdefault("product_files", [])
    if q.data == "prod_done":
        if not chosen:
            await q.answer("حداقل یک محصول انتخاب کن.", show_alert=True)
            return S_PRODUCTS
        await q.edit_message_text("تیتر اصلی را تایپ کن (≤۸۰ کاراکتر):")
        return S_HEADLINE
    filename = q.data.split(":", 1)[1]
    if filename not in chosen and len(chosen) < 5:
        chosen.append(filename)
    _save(ctx, q.message.chat_id)
    await q.answer(f"اضافه شد ({len(chosen)}/۵)")
    return S_PRODUCTS


async def on_headline(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()[:80]
    if not text:
        await update.message.reply_text("خالی بود — دوباره بفرست.")
        return S_HEADLINE
    ctx.user_data["video_state"]["headline"] = text
    _save(ctx, update.message.chat_id)
    await update.message.reply_text("زیرعنوان (اختیاری) یا /skip:")
    return S_SUBHEAD


async def on_subhead(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text and text != "/skip":
        ctx.user_data["video_state"]["subheadline"] = text[:100]
    _save(ctx, update.message.chat_id)
    await update.message.reply_text("قیمت یا آمار کلیدی (مثلاً «۳۰٪ صرفه‌جویی»)، یا /skip:")
    return S_PRICE


async def on_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text and text != "/skip":
        ctx.user_data["video_state"]["price_or_stat"] = text[:60]
    _save(ctx, update.message.chat_id)
    await update.message.reply_text("متن CTA (پیش‌فرض «اطلاعات بیشتر») یا /skip:")
    return S_CTA


async def on_cta(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    ctx.user_data["video_state"]["cta"] = text if text and text != "/skip" else "اطلاعات بیشتر"
    _save(ctx, update.message.chat_id)
    await update.message.reply_text(
        "مدت زمان؟",
        reply_markup=_kb([[("۱۵ ثانیه", "dur:15"), ("۲۰ ثانیه", "dur:20"), ("۳۰ ثانیه", "dur:30")]]),
    )
    return S_DURATION


async def on_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ctx.user_data["video_state"]["duration"] = int(q.data.split(":", 1)[1])
    _save(ctx, q.message.chat_id)
    tracks = assets.list_music(_assets_root())
    rows = [[(t, f"mus:{t}")] for t in tracks]
    rows.append([("🎵 خودکار بر اساس برند", "mus:__auto__"), ("🔇 بدون موزیک", "mus:__none__")])
    await q.edit_message_text("موزیک پس‌زمینه؟", reply_markup=_kb(rows))
    return S_MUSIC


_AUTO_MUSIC = {
    "AlumGlass":  "corporate-uplifting.mp3",
    "NanoShield": "cinematic-tech.mp3",
    "Shahrzad":   "calm-storytelling.mp3",
}


async def on_music(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":", 1)[1]
    if choice == "__none__":
        ctx.user_data["video_state"].pop("music_file", None)
    elif choice == "__auto__":
        ctx.user_data["video_state"]["music_file"] = _AUTO_MUSIC.get(
            ctx.user_data["video_state"]["brand"], "corporate-uplifting.mp3"
        )
    else:
        ctx.user_data["video_state"]["music_file"] = choice
    _save(ctx, q.message.chat_id)
    await q.edit_message_text(
        "روایت صوتی؟",
        reply_markup=_kb([
            [("🎙 متن narration می‌نویسم", "narr:yes")],
            [("🤐 بدون narration",        "narr:no")],
        ]),
    )
    return S_NARRATION_CHOICE


async def on_narration_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data.endswith(":no"):
        return await _confirm(q, ctx)
    await q.edit_message_text("متن روایت (فارسی، تا ۵۰۰ کاراکتر):")
    return S_NARRATION_TEXT


async def on_narration_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()[:500]
    if not text:
        await update.message.reply_text("خالی بود — دوباره بفرست یا /cancel.")
        return S_NARRATION_TEXT
    ctx.user_data["video_state"]["narration_text"] = text
    _save(ctx, update.message.chat_id)
    return await _confirm(update.message, ctx, via_message=True)


async def _confirm(target: Any, ctx: ContextTypes.DEFAULT_TYPE, *, via_message: bool = False) -> int:
    s = ctx.user_data["video_state"]
    summary = (
        f"<b>خلاصه</b>\n"
        f"برند: {s.get('brand')}\n"
        f"تمپلیت: {s.get('template')}  •  {s.get('aspect')}  •  {s.get('duration')}s\n"
        f"تیتر: {s.get('headline')}\n"
        f"زیرعنوان: {s.get('subheadline','—')}\n"
        f"قیمت/آمار: {s.get('price_or_stat','—')}\n"
        f"CTA: {s.get('cta')}\n"
        f"موزیک: {s.get('music_file','—')}\n"
        f"روایت: {('بله' if s.get('narration_text') else 'خیر')}\n"
    )
    kb = _kb([[("✅ Render", "go:render"), ("❌ Cancel", "go:cancel")]])
    if via_message:
        await target.reply_text(summary, parse_mode="HTML", reply_markup=kb)
    else:
        await target.edit_message_text(summary, parse_mode="HTML", reply_markup=kb)
    return S_CONFIRM


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    _wipe(ctx, chat_id)
    await update.effective_message.reply_text("لغو شد.")
    return ConversationHandler.END
