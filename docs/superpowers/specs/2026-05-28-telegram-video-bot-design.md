# Telegram `/video` — Remotion Video Bot

**Status:** Approved design, ready for implementation plan
**Date:** 2026-05-28
**Owner:** Saeid Madarshahi

## Goal

Add a `/video` command to the existing Claude Code Telegram bot so promotional / explainer videos can be produced end-to-end from the chat: brand selection, parameter wizard, optional ElevenLabs narration, Remotion render, MP4 delivered back to Telegram (or download link when too large).

Two execution paths:
- **Quick mode** — two pre-built Remotion templates (`ProductPromo`, `ServicePromo`) rendered directly by a Node subprocess. Fast, deterministic, no LLM in the render loop.
- **Custom mode** — free-text intent + reference images dispatched to a Claude Code session in the Remotion project. Claude writes a one-off composition and renders it. Slower, flexible, optional.

## Non-goals (v1)

- Pre-built `ScienceExplainer` / `StoryCard` templates (these go to Custom mode for now, become Quick templates in v2)
- 1:1 aspect ratio (v2)
- Uploading logos / product images via Telegram (use scp / web-terminal for v1; music upload IS supported)
- Editing `brand.json` from inside the bot
- Parallel render queue (one render per user at a time)
- SFX library and scene-transition library

## High-level architecture

```
Telegram ─► bot.py (existing) ─► video_module/* (new)
                                    │
                          ┌─────────┼─────────┐
                          ▼         ▼         ▼
              /tmp/remotion-assets  ElevenLabs  /mnt/devopsstorage/repos/video
                  (read-only        TTS API     (Remotion project)
                   except music)                       │
                                                       ▼
                                          subprocess: npx remotion render
                                                       │
                                                       ▼
                                          renders/<timestamp>-…mp4
                                                       │
                                                       ▼
                                          Telegram send_video (≤50 MB)
                                          or report URL (>50 MB)
```

## Asset directory layout

```
/tmp/remotion-assets/
├── _shared/
│   ├── fonts/             # IRANSansX-Bold/Regular, Vazirmatn-*  (already populated)
│   └── music/             # 10 royalty-free tracks downloaded during setup + user uploads
├── AlumGlass/
│   ├── brand.json
│   ├── logo/
│   ├── products/
│   └── projects/
├── NanoShield/
│   ├── brand.json         # parent: "راهکارهای پایدار زیگورات"
│   ├── logo/
│   ├── products/
│   └── projects/
└── Shahrzad/
    ├── brand.json
    ├── logo/
    ├── products/
    └── projects/
```

### `brand.json` schema

```jsonc
{
  "name":         "string (slug, must match folder name)",
  "displayName":  "string (Persian/English brand display)",
  "parent":       "string (optional — company name for sub-brands)",
  "tagline_fa":   "string",
  "tagline_en":   "string",
  "colors": {
    "primary":    "#hex",
    "accent":     "#hex",
    "dark":       "#hex",
    "light":      "#hex",
    "secondary":  "#hex (optional)"
  },
  "fonts": {
    "heading":    "/tmp/remotion-assets/_shared/fonts/<file>",
    "body":       "/tmp/remotion-assets/_shared/fonts/<file>"
  },
  "aesthetic":    "free-text — used in Custom-mode prompts",
  "voiceTone":    "free-text — used in Custom-mode prompts",
  "audience":     "string (optional)",
  "website":      "string (optional)",
  "tts": {
    "provider":   "elevenlabs",
    "voiceId":    "string",
    "modelId":    "eleven_multilingual_v2"
  }
}
```

### Brand seed values

| Brand      | Primary  | Accent   | Heading font     | ElevenLabs voice  |
|------------|----------|----------|------------------|-------------------|
| AlumGlass  | #0B486B  | #FED03D  | IRANSansX-Bold   | Adrian (`BognUUMX6W1qmZKB2TOw`) |
| NanoShield | #0F172A  | #06B6D4  | Vazirmatn-Bold   | Adrian (`BognUUMX6W1qmZKB2TOw`) |
| Shahrzad   | #0D9488  | #F59E0B  | Vazirmatn-Black* | Nazy (`WwAjIyMBDBNl1dvId9Xe`) |

\* Vazirmatn-Black substitutes for SecularOne in v1; swap when SecularOne TTF is added.

## Remotion project (`/mnt/devopsstorage/repos/video/`)

```
src/
├── index.ts                       # registerRoot(RemotionRoot)
├── Root.tsx                       # Composition registry — 4 entries (2 templates × 2 aspects)
├── compositions/
│   ├── ProductPromo.tsx           # T1
│   └── ServicePromo.tsx           # T2
├── scenes/
│   ├── LogoReveal.tsx
│   ├── ProductHero.tsx
│   ├── ProjectGallery.tsx
│   ├── PriceBadge.tsx
│   ├── StatsCounter.tsx
│   └── CtaEnd.tsx
└── lib/
    ├── theme.ts                   # brand.json → CSS variables / theme props
    └── audio.ts                   # narration + music layer composition

public/                            # populated per-job: copies (not symlinks) of needed assets
renders/                           # mp4 outputs + .log.json siblings
remotion.config.ts                 # existing (Tailwind v4 already wired)
```

### Composition IDs

| Template      | Aspect | Composition ID            | Resolution |
|---------------|--------|---------------------------|------------|
| ProductPromo  | 9:16   | `ProductPromo_Vertical`   | 1080×1920  |
| ProductPromo  | 16:9   | `ProductPromo_Horizontal` | 1920×1080  |
| ServicePromo  | 9:16   | `ServicePromo_Vertical`   | 1080×1920  |
| ServicePromo  | 16:9   | `ServicePromo_Horizontal` | 1920×1080  |

### Input props contract (shared by both templates)

```ts
type PromoProps = {
  brand: BrandJson;            // full brand.json injected
  logoFile: string;            // path relative to public/
  productImages: string[];     // 1..5 paths
  headline: string;
  subheadline?: string;
  priceOrStat?: string;
  cta: string;
  durationInSeconds: 15 | 20 | 30;
  music?: { file: string; volume: number };
  narration?: { file: string; volume: number };
};
```

Duration in frames = `durationInSeconds * 30` (30 fps fixed). Aspect picks the registered composition ID.

## Bot extension (Python)

### New module layout (alongside `bot.py`)

```
video_module/
├── __init__.py
├── handlers.py          # CommandHandler / CallbackQueryHandler / MessageHandler glue
├── wizard.py            # FSM via ConversationHandler — states + transitions
├── assets.py            # read /tmp/remotion-assets, parse brand.json, list files
├── props.py             # build PromoProps JSON from wizard state
├── renderer.py          # asyncio subprocess wrapper around `npx remotion render`
├── tts.py               # ElevenLabs HTTPS client (aiohttp), writes mp3 to job tmp dir
└── jobs.py              # JSON-on-disk job state at /tmp/video-jobs/<chat_id>.json
```

### Changes to `bot.py`

Minimal, additive only:
1. `from video_module.handlers import register as register_video` near other imports.
2. One call `register_video(app)` inside the handler registration block (around `bot.py:3224`).
3. Four entries added to `set_my_commands` (around `bot.py:2965`):
   - `BotCommand("video", "🎬 ساخت ویدئو")`
   - `BotCommand("assets", "📁 لیست asset برند")`
   - `BotCommand("upload_music", "🎵 آپلود موزیک")`
   - `BotCommand("renders", "📺 آخرین render‌ها")`

No existing handler signatures or behaviour change.

### `/video` command flow

```
/video
  │
  ▼
[Inline menu]
  ├─ 🎬 ساخت ویدئو جدید           → enter wizard
  ├─ 📁 لیست assets               → ask for brand → /assets <brand>
  ├─ 📺 Recent renders             → last 10 from renders/ with download links
  └─ ❓ راهنما                     → short usage card

[Wizard — Quick mode]
  1.  Brand           inline buttons [AlumGlass | NanoShield | Shahrzad]
  2.  Template        [ProductPromo | ServicePromo | Custom (Claude)]
  3.  Aspect          [9:16 عمودی | 16:9 افقی]
  4.  Logo            picker from <brand>/logo/
  5.  Product images  picker from <brand>/products/, 1–5
  6.  Headline        free text (max 80 chars)
  7.  Subheadline     free text or /skip
  8.  Price/Stat      free text or /skip
  9.  CTA             free text, default "اطلاعات بیشتر"
  10. Duration        [15s | 20s | 30s]
  11. Music           picker from _shared/music/ + [None | Auto-by-brand]
  12. Narration       [بدون] or free text → ElevenLabs TTS at brand.voiceId → mp3
  13. Confirm         summary card + [✅ Render | ✏️ Edit field N | ❌ Cancel]

[Render]
  → "🎬 در حال render… (~۲ دقیقه)" placeholder message
  → subprocess: npx remotion render <compositionId> renders/<file>.mp4 --props=<jsonfile>
  → on success:
      stat the mp4
      if size <= 50 MB:  send_video(mp4) + caption with brand/template/duration
      else:              upload via make-report.sh, reply with report URL
  → write renders/<file>.log.json: input props, render duration, exit code, stderr tail
  → on failure: surface stderr tail to user, keep .log.json for debugging

[Custom mode (T3/T4 or anything off-template)]
  → free-text intent + optional reference image
  → build structured Remotion prompt (template seeded from sample prompts in this doc)
  → forward to existing /new flow with project = `video`
  → Claude Code session writes the composition and renders it
```

### Wizard state machine

- One FSM instance per chat_id.
- Steps stored as enum; current step + collected values persisted to `/tmp/video-jobs/<chat_id>.json` after every transition (atomic write: tmp + rename).
- Restart safety: on bot startup, if `<chat_id>.json` exists, the wizard resumes at the last saved step on the user's next message.
- Each step exposes `/back` (rewind one) and `/cancel` (delete job file, exit).

### Concurrency

- One active render per chat_id. Second `/video` while rendering → reply "🎬 یک render در حال انجامه. صبر کن یا /cancel".
- Render timeout: 600s (10 min). Exceeded → kill subprocess, surface error.

## TTS (ElevenLabs)

- HTTPS POST to `https://api.elevenlabs.io/v1/text-to-speech/<voice_id>` with `model_id=eleven_multilingual_v2`.
- Persian narration text is sent verbatim. No diacritic preprocessing (Persian works well in `eleven_multilingual_v2`).
- Response is mp3 binary → written to `/tmp/video-jobs/<chat_id>/narration.mp3`.
- Passed to Remotion as a public/-relative path in `narration.file`.
- On API failure: surface error, offer "بدون narration ادامه بده" or "/cancel".

## Music library

10 royalty-free tracks downloaded during initial setup from Pixabay Music (CC0) into `/tmp/remotion-assets/_shared/music/`. Mood-tagged filenames:

```
corporate-uplifting.mp3
cinematic-tech.mp3
calm-storytelling.mp3
energetic-promo.mp3
scientific-explainer.mp3
minimal-ambient.mp3
warm-piano.mp3
modern-electronic.mp3
hopeful-acoustic.mp3
documentary-strings.mp3
```

User uploads (`/upload_music`) land in the same folder after filename sanitization (lower-cased, non-alphanumeric → `-`, `.mp3`/`.wav` only, max 20 MB).

Auto-by-brand mapping (single track per brand to keep selection deterministic):
- AlumGlass → `corporate-uplifting.mp3`
- NanoShield → `cinematic-tech.mp3`
- Shahrzad → `calm-storytelling.mp3`

Default volumes when not user-specified: `music.volume = 0.25`, `narration.volume = 1.0`. Music ducks to 0.10 while narration plays (handled inside `lib/audio.ts`).

## Render output & retention

- Path: `/mnt/devopsstorage/repos/video/renders/<UTC>-<brand>-<template>-<aspect>.mp4`
- Log sibling: `<…>.log.json` with input props, render duration, stderr tail, exit code.
- Retention: 30 days. A cron entry (added during setup) deletes anything older.

## Environment variables (live bot)

Added to `/opt/shahrzad-devops/.env` (loaded by systemd `EnvironmentFile=`):

```bash
REMOTION_PROJECT_DIR=/mnt/devopsstorage/repos/video
REMOTION_ASSETS_DIR=/tmp/remotion-assets
ELEVENLABS_API_KEY=sk_...
```

`.env.example` (in the repo) updated to document them.

## `projects.json` registration

Add to `/opt/shahrzad-devops/configs/projects.json` for Custom mode access:

```json
{
  "name": "video",
  "path": "/mnt/devopsstorage/repos/video"
}
```

This lets `/project video` from the bot point a Claude Code session into the Remotion repo when Custom mode dispatches.

## Documentation deliverables

To be produced alongside the implementation:

1. **`docs/VIDEO_BOT.md`** in the bot repo — user guide: command list, wizard walkthrough, brand setup, music upload, troubleshooting.
2. **`README.md` section** in `/mnt/devopsstorage/repos/video/` — explains the composition contract, how to add a new template, how to run `npx remotion render` manually for debugging.
3. **`docs/VIDEO_BOT_SETUP.md`** in the bot repo — DevOps setup: env vars, asset folder layout, brand.json seeding, ElevenLabs key, cron retention.
4. **`CHANGELOG.md`** entry summarizing the `/video` feature.

## Testing & verification

- **Unit:** `assets.py` brand.json parsing + path validation; `props.py` shape correctness; `tts.py` mocked HTTP.
- **Integration:** one end-to-end Quick render per template per aspect (4 renders total) against seeded sample assets.
- **Smoke:** `/video → menu → cancel` round-trip leaves no job state on disk.
- **Render budget:** 30-second 1080×1920 should complete in under 3 minutes on the dev VPS.

## Open items deferred to v2

- T3 `ScienceExplainer` + T4 `StoryCard` as first-class Quick templates
- 1:1 aspect ratio
- SFX library + scene-transition library
- Asset upload (logos / product / project images) from Telegram
- `brand.json` edit from Telegram
- Parallel render queue / multi-user contention handling beyond per-chat
- Alternate TTS providers behind the same `tts.py` interface
