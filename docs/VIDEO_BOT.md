# /video — Promo Video Creation

Use the bot to create promo videos for AlumGlass, NanoShield, and Shahrzad.

## Quick start

1. Send `/video`
2. Tap a brand
3. Tap a template:
   - **🎯 Product Promo** — one product + headline + price + CTA
   - **🏗 Service / Project Promo** — multiple project photos + stats
   - **✨ Custom (Claude Code)** — free-text, builds custom composition
4. Tap an aspect: `9:16` (Reels / Shorts) or `16:9` (YouTube / desktop)
5. Pick a logo, then 1–5 product/project images
6. Type headline, optional subheadline, optional price/stat, CTA text
7. Pick duration (15 / 20 / 30 sec)
8. Pick background music (or "Auto" / "بدون موزیک")
9. Optional narration — type Persian text, ElevenLabs synthesises it
10. Confirm. After ~2 minutes, the mp4 lands in the chat.

## Other commands

- `/assets <Brand>` — list available logos, product images, project images for a brand
- `/upload_music` — send an mp3/wav file to add to the shared music library
- `/renders` — list the 10 most recent rendered videos

## Adding new assets

For now, asset files are managed manually. Copy your files into:

```
/tmp/remotion-assets/<Brand>/logo/
/tmp/remotion-assets/<Brand>/products/
/tmp/remotion-assets/<Brand>/projects/
```

Music files can be uploaded via `/upload_music` from Telegram directly.

## Troubleshooting

- **"هیچ برندی پیدا نشد"** — `/tmp/remotion-assets/` is missing or empty. See `VIDEO_BOT_SETUP.md`.
- **"لوگویی پیدا نشد"** — drop a `.png`/`.jpg` into the brand's `logo/` folder.
- **"ELEVENLABS_API_KEY تنظیم نشده"** — narration disabled; add the key to `/opt/shahrzad-devops/.env` and restart the bot.
- **Render failed** — check `/mnt/devopsstorage/repos/video/renders/*.log.json` for the most recent job — `stderr_tail` shows the Remotion error.
