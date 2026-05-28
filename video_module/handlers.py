"""Glue: register the /video conversation, /assets, /upload_music, /renders.

Also owns the final confirm → render → deliver step (because that requires
all four sibling modules: assets, props, tts, renderer).
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from telegram import InputFile, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from . import assets, props, tts, wizard
from .renderer import RenderError, RenderJob, render

log = logging.getLogger(__name__)
_MAX_TG_BYTES = 50 * 1024 * 1024


def _project_dir() -> Path:
    return Path(os.environ.get("REMOTION_PROJECT_DIR", "/mnt/devopsstorage/repos/video"))


def _assets_root() -> Path:
    return Path(os.environ.get("REMOTION_ASSETS_DIR", "/tmp/remotion-assets"))


def _renders_dir() -> Path:
    p = _project_dir() / "renders"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower() or "music"


# ──────────────────────────── /video confirm step ──────────────────────────

async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "go:cancel":
        return await wizard.cancel(update, ctx)
    chat_id = q.message.chat_id
    state = ctx.user_data.get("video_state") or {}
    try:
        brand_json = assets.load_brand(_assets_root(), state["brand"])
    except FileNotFoundError as e:
        await q.edit_message_text(f"⚠️ {e}")
        return ConversationHandler.END

    await q.edit_message_text("🎬 در حال render… (~۲ دقیقه)")

    # 1. TTS if narration requested
    narration_path = None
    if text := state.get("narration_text"):
        api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not api_key:
            await ctx.bot.send_message(chat_id, "⚠️ ELEVENLABS_API_KEY تنظیم نشده — بدون narration می‌سازم.")
        else:
            try:
                narration_path = Path(f"/tmp/video-jobs/{chat_id}-narration.mp3")
                narration_path.parent.mkdir(parents=True, exist_ok=True)
                await tts.synthesize(
                    text=text,
                    voice_id=brand_json["tts"]["voiceId"],
                    model_id=brand_json["tts"]["modelId"],
                    api_key=api_key,
                    output_path=narration_path,
                )
                state["narration_file"] = narration_path.name
            except tts.TtsError as e:
                await ctx.bot.send_message(chat_id, f"⚠️ TTS خطا: {e}\nبدون narration ادامه می‌دم.")
                narration_path = None

    # 2. Build props + RenderJob
    props_dict = props.build(brand_json, state)
    composition_id = props.composition_id(state["template"], state["aspect"])
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    aspect_tag = "v" if state["aspect"] == "9:16" else "h"
    out_name = f"{stamp}-{state['brand']}-{state['template']}-{aspect_tag}.mp4"
    out_path = _renders_dir() / out_name
    job = RenderJob(
        composition_id=composition_id,
        props=props_dict,
        logo_src=_assets_root() / state["brand"] / "logo" / state["logo_file"],
        product_srcs=[_assets_root() / state["brand"] / "products" / f for f in state["product_files"]],
        music_src=(_assets_root() / "_shared" / "music" / state["music_file"]) if state.get("music_file") else None,
        narration_src=narration_path,
        output_path=out_path,
    )

    # 3. Render
    try:
        result = await render(_project_dir(), job)
    except RenderError as e:
        log.exception("render failed")
        await ctx.bot.send_message(chat_id, f"❌ Render failed:\n<pre>{e}</pre>", parse_mode="HTML")
        wizard._wipe(ctx, chat_id)
        return ConversationHandler.END

    # 4. Deliver
    size = result.output_path.stat().st_size
    caption = (
        f"✅ {state['brand']} • {state['template']} • {state['aspect']} • "
        f"{state['duration']}s ({size // (1024*1024)} MB, {result.duration_seconds:.0f}s render)"
    )
    if size <= _MAX_TG_BYTES:
        with result.output_path.open("rb") as fh:
            await ctx.bot.send_video(chat_id, InputFile(fh, filename=out_name), caption=caption)
    else:
        await ctx.bot.send_message(
            chat_id,
            f"{caption}\n\n📥 خیلی بزرگه برای Telegram. مسیر فایل:\n<code>{result.output_path}</code>",
            parse_mode="HTML",
        )

    wizard._wipe(ctx, chat_id)
    return ConversationHandler.END


# ───────────────────────────── /assets ────────────────────────────────────

async def cmd_assets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    if not args:
        brands = assets.list_brands(_assets_root())
        await update.message.reply_text("برندها: " + ", ".join(brands) + "\nمثال: /assets AlumGlass")
        return
    brand = args[0]
    try:
        brand_json = assets.load_brand(_assets_root(), brand)
    except FileNotFoundError:
        await update.message.reply_text(f"برند {brand!r} پیدا نشد.")
        return
    lines = [f"<b>{brand_json['displayName']}</b>"]
    for kind in ("logo", "products", "projects"):
        items = assets.list_assets(_assets_root(), brand, kind)
        lines.append(f"<b>{kind}</b> ({len(items)}): " + (", ".join(items) if items else "—"))
    music = assets.list_music(_assets_root())
    lines.append(f"<b>shared/music</b> ({len(music)}): " + ", ".join(music[:8]) + (" …" if len(music) > 8 else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ───────────────────────────── /upload_music ──────────────────────────────

async def cmd_upload_music(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["awaiting_music_upload"] = True
    await update.message.reply_text("یک فایل mp3/wav (≤۲۰MB) بفرست. /cancel برای لغو.")


async def on_audio_or_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get("awaiting_music_upload"):
        return  # not our message; other handlers (e.g. existing on_document) will handle
    ctx.user_data["awaiting_music_upload"] = False

    audio = update.message.audio or update.message.document
    if audio is None:
        await update.message.reply_text("فایل صوتی نبود.")
        return
    name = (audio.file_name or "track.mp3").lower()
    if not name.endswith((".mp3", ".wav")):
        await update.message.reply_text("فقط mp3/wav قابل قبوله.")
        return
    if (audio.file_size or 0) > 20 * 1024 * 1024:
        await update.message.reply_text("فایل بزرگ‌تر از ۲۰MB.")
        return
    stem = _safe_name(Path(name).stem)
    target = _assets_root() / "_shared" / "music" / f"{stem}{Path(name).suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    f = await audio.get_file()
    await f.download_to_drive(custom_path=str(target))
    await update.message.reply_text(f"✅ ذخیره شد: {target.name}")


# ───────────────────────────── /renders ───────────────────────────────────

async def cmd_renders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    folder = _renders_dir()
    items = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    if not items:
        await update.message.reply_text("هیچ render ای وجود نداره.")
        return
    lines = ["<b>۱۰ render اخیر:</b>"]
    for p in items:
        size = p.stat().st_size // (1024 * 1024)
        lines.append(f"• <code>{p.name}</code> ({size} MB)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ────────────────────────── register on app ────────────────────────────────

def register(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("video", wizard.start)],
        states={
            wizard.S_BRAND:    [CallbackQueryHandler(wizard.on_brand,    pattern=r"^brand:")],
            wizard.S_TEMPLATE: [CallbackQueryHandler(wizard.on_template, pattern=r"^tpl:")],
            wizard.S_ASPECT:   [CallbackQueryHandler(wizard.on_aspect,   pattern=r"^asp:")],
            wizard.S_LOGO:     [CallbackQueryHandler(wizard.on_logo,     pattern=r"^logo:")],
            wizard.S_PRODUCTS: [CallbackQueryHandler(wizard.on_product,  pattern=r"^(prod:|prod_done)")],
            wizard.S_HEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard.on_headline)],
            wizard.S_SUBHEAD:  [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard.on_subhead),
                                CommandHandler("skip", wizard.on_subhead)],
            wizard.S_PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard.on_price),
                                CommandHandler("skip", wizard.on_price)],
            wizard.S_CTA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard.on_cta),
                                CommandHandler("skip", wizard.on_cta)],
            wizard.S_DURATION: [CallbackQueryHandler(wizard.on_duration, pattern=r"^dur:")],
            wizard.S_MUSIC:    [CallbackQueryHandler(wizard.on_music,    pattern=r"^mus:")],
            wizard.S_NARRATION_CHOICE: [CallbackQueryHandler(wizard.on_narration_choice, pattern=r"^narr:")],
            wizard.S_NARRATION_TEXT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard.on_narration_text)],
            wizard.S_CONFIRM:  [CallbackQueryHandler(on_confirm,         pattern=r"^go:")],
        },
        fallbacks=[CommandHandler("cancel", wizard.cancel)],
        per_user=True,
        per_chat=True,
        name="video_wizard",
        persistent=False,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("assets",       cmd_assets))
    app.add_handler(CommandHandler("upload_music", cmd_upload_music))
    app.add_handler(CommandHandler("renders",      cmd_renders))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, on_audio_or_doc), group=1)
