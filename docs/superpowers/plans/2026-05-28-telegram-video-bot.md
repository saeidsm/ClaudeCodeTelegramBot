# Telegram `/video` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/video` command to the Claude Code Telegram bot that produces promo/explainer videos end-to-end: brand selection → Remotion render → ElevenLabs narration → MP4 delivered through Telegram.

**Architecture:** Two-path hybrid. Quick path (2 pre-built Remotion templates × 2 aspect ratios) renders directly via `npx remotion render` subprocess. Custom path forwards a structured prompt to a Claude Code session in `/mnt/devopsstorage/repos/video/`. State machine via `ConversationHandler`; job state persisted as JSON for crash recovery.

**Tech Stack:** Python 3.10 + `python-telegram-bot` 21+, Remotion 4.0.468 + React 19 + TypeScript 5, ElevenLabs HTTPS API, ffmpeg (bundled with Remotion).

**Spec:** [`../specs/2026-05-28-telegram-video-bot-design.md`](../specs/2026-05-28-telegram-video-bot-design.md)

---

## File Map

### Existing files modified
- `bot.py:2965-2978` — register 4 new `BotCommand` entries
- `bot.py:3224-3236` — add `register_video_module(app)` call
- `.env.example` — document 3 new env vars
- `/opt/shahrzad-devops/configs/projects.json` — append `video` entry
- `/mnt/devopsstorage/repos/video/src/Root.tsx` — register 4 compositions
- `/mnt/devopsstorage/repos/video/src/Composition.tsx` — delete (replaced by `compositions/`)

### New files (bot repo)
- `video_module/__init__.py`
- `video_module/assets.py` — read brand.json, list/validate brand files
- `video_module/jobs.py` — JSON job state on disk
- `video_module/props.py` — build PromoProps from wizard state
- `video_module/tts.py` — ElevenLabs HTTPS client
- `video_module/renderer.py` — asyncio subprocess wrapper for `npx remotion render`
- `video_module/wizard.py` — ConversationHandler FSM
- `video_module/handlers.py` — command + callback handlers + `register()`
- `tests/test_video_assets.py`
- `tests/test_video_jobs.py`
- `tests/test_video_props.py`
- `tests/test_video_tts.py`
- `docs/VIDEO_BOT.md` — user guide
- `docs/VIDEO_BOT_SETUP.md` — DevOps setup
- `scripts/seed-video-assets.sh` — one-shot seeder for folders + music

### New files (Remotion repo `/mnt/devopsstorage/repos/video/`)
- `src/lib/theme.ts`
- `src/lib/audio.ts`
- `src/lib/fonts.ts`
- `src/scenes/LogoReveal.tsx`
- `src/scenes/ProductHero.tsx`
- `src/scenes/ProjectGallery.tsx`
- `src/scenes/PriceBadge.tsx`
- `src/scenes/StatsCounter.tsx`
- `src/scenes/CtaEnd.tsx`
- `src/compositions/ProductPromo.tsx`
- `src/compositions/ServicePromo.tsx`
- `src/styles/fonts.css`
- `README.md` (new sections)
- `scripts/render-samples.sh` — renders all 4 sample combos

### Filesystem artifacts (not in any repo)
- `/tmp/remotion-assets/{AlumGlass,NanoShield,Shahrzad}/{logo,products,projects}/`
- `/tmp/remotion-assets/{AlumGlass,NanoShield,Shahrzad}/brand.json`
- `/tmp/remotion-assets/_shared/music/*.mp3` (10 tracks, downloaded)
- `/opt/shahrzad-devops/.env` — `ELEVENLABS_API_KEY` (user-supplied)

---

## Phase A — Foundations

### Task A1: Seed asset directory skeleton

**Files:**
- Create: `/tmp/remotion-assets/_shared/music/.gitkeep`
- Create: `/tmp/remotion-assets/{AlumGlass,NanoShield,Shahrzad}/{logo,products,projects}/`

- [ ] **Step 1: Create folder skeleton**

```bash
for brand in AlumGlass NanoShield Shahrzad; do
  for sub in logo products projects; do
    mkdir -p "/tmp/remotion-assets/${brand}/${sub}"
  done
done
mkdir -p /tmp/remotion-assets/_shared/music
ls -la /tmp/remotion-assets/
```

Expected output: 4 entries — `_shared`, `AlumGlass`, `NanoShield`, `Shahrzad`.

- [ ] **Step 2: Verify fonts directory already populated**

```bash
ls /tmp/remotion-assets/_shared/fonts/ | wc -l
```

Expected: ≥7 files (IRANSansX-Bold/Regular .woff/.woff2 + Vazirmatn variants).

- [ ] **Step 3: No commit (filesystem only)** — these paths are not in git.

### Task A2: Write `brand.json` for each brand

**Files:**
- Create: `/tmp/remotion-assets/AlumGlass/brand.json`
- Create: `/tmp/remotion-assets/NanoShield/brand.json`
- Create: `/tmp/remotion-assets/Shahrzad/brand.json`

- [ ] **Step 1: Write AlumGlass brand.json**

```bash
cat > /tmp/remotion-assets/AlumGlass/brand.json <<'JSON'
{
  "name": "AlumGlass",
  "displayName": "آلومینیوم شیشه تهران",
  "tagline_fa": "مشاور تخصصی نما",
  "tagline_en": "First specialized facade consultant — bridging vision and engineering",
  "colors": {
    "primary": "#0B486B",
    "accent":  "#FED03D",
    "dark":    "#333333",
    "light":   "#FFFFFF"
  },
  "fonts": {
    "heading": "/tmp/remotion-assets/_shared/fonts/IRANSansX-Bold.woff2",
    "body":    "/tmp/remotion-assets/_shared/fonts/IRANSansX-Regular.woff2"
  },
  "aesthetic": "industrial-modern, structural, sophisticated",
  "voiceTone": "professional, authoritative, expert, educational",
  "website":   "alumglass.com",
  "tts": {
    "provider": "elevenlabs",
    "voiceId":  "BognUUMX6W1qmZKB2TOw",
    "modelId":  "eleven_multilingual_v2"
  }
}
JSON
```

- [ ] **Step 2: Write NanoShield brand.json**

```bash
cat > /tmp/remotion-assets/NanoShield/brand.json <<'JSON'
{
  "name": "NanoShield",
  "displayName": "نانوشیلد",
  "parent": "راهکارهای پایدار زیگورات",
  "tagline_fa": "سپر نامرئی ساختمان در برابر گرما",
  "tagline_en": "Invisible shield for buildings against heat invasion",
  "colors": {
    "primary":   "#0F172A",
    "accent":    "#06B6D4",
    "secondary": "#1E40AF",
    "light":     "#FFFFFF"
  },
  "fonts": {
    "heading": "/tmp/remotion-assets/_shared/fonts/Vazirmatn-Bold.woff2",
    "body":    "/tmp/remotion-assets/_shared/fonts/Vazirmatn-Regular.woff2"
  },
  "aesthetic": "technical, sustainable, luminous, precise, architectural",
  "voiceTone": "professional, authoritative, scientific, solution-oriented",
  "tts": {
    "provider": "elevenlabs",
    "voiceId":  "BognUUMX6W1qmZKB2TOw",
    "modelId":  "eleven_multilingual_v2"
  }
}
JSON
```

- [ ] **Step 3: Write Shahrzad brand.json**

```bash
cat > /tmp/remotion-assets/Shahrzad/brand.json <<'JSON'
{
  "name": "Shahrzad",
  "displayName": "شهرزاد",
  "tagline_fa": "داستان‌هایی که با ذهن کودک رشد می‌کنند",
  "tagline_en": "Stories That Grow With Your Child's Mind",
  "colors": {
    "primary":   "#0D9488",
    "accent":    "#F59E0B",
    "dark":      "#111827",
    "secondary": "#6B7280",
    "light":     "#FFFFFF"
  },
  "fonts": {
    "heading": "/tmp/remotion-assets/_shared/fonts/Vazirmatn-Black.woff2",
    "body":    "/tmp/remotion-assets/_shared/fonts/Vazirmatn-Regular.woff2"
  },
  "aesthetic": "minimalist intellectualism, sophisticated simplicity, calm scaffolding",
  "voiceTone": "ethical, scientific, empathetic, visionary",
  "audience":  "children 3-12, parents",
  "website":   "shahrzad.ai",
  "tts": {
    "provider": "elevenlabs",
    "voiceId":  "WwAjIyMBDBNl1dvId9Xe",
    "modelId":  "eleven_multilingual_v2"
  }
}
JSON
```

- [ ] **Step 4: Validate JSON parses**

```bash
for b in AlumGlass NanoShield Shahrzad; do
  python3 -c "import json; json.load(open('/tmp/remotion-assets/${b}/brand.json'))" && echo "${b} OK"
done
```

Expected: `AlumGlass OK / NanoShield OK / Shahrzad OK`.

### Task A3: Download 10 royalty-free music tracks

**Files:**
- Create: `scripts/seed-video-assets.sh` (idempotent music downloader)
- Create: `/tmp/remotion-assets/_shared/music/*.mp3`

- [ ] **Step 1: Write the seeder script**

Create `scripts/seed-video-assets.sh` with content:

```bash
#!/usr/bin/env bash
# Downloads royalty-free music tracks from Pixabay CDN into
# /tmp/remotion-assets/_shared/music/. Idempotent: skips files that exist.
set -euo pipefail

DEST="/tmp/remotion-assets/_shared/music"
mkdir -p "$DEST"

# Format: filename|URL  (Pixabay direct CDN, CC0)
TRACKS=(
  "corporate-uplifting.mp3|https://cdn.pixabay.com/audio/2022/03/15/audio_115b9d5e1d.mp3"
  "cinematic-tech.mp3|https://cdn.pixabay.com/audio/2022/11/22/audio_dc39bde104.mp3"
  "calm-storytelling.mp3|https://cdn.pixabay.com/audio/2022/10/30/audio_347111d559.mp3"
  "energetic-promo.mp3|https://cdn.pixabay.com/audio/2022/05/16/audio_1808fbf07a.mp3"
  "scientific-explainer.mp3|https://cdn.pixabay.com/audio/2023/06/18/audio_57f25e8a2a.mp3"
  "minimal-ambient.mp3|https://cdn.pixabay.com/audio/2022/03/10/audio_2dde668d05.mp3"
  "warm-piano.mp3|https://cdn.pixabay.com/audio/2022/10/14/audio_d28be0c2b8.mp3"
  "modern-electronic.mp3|https://cdn.pixabay.com/audio/2022/08/02/audio_2dde668d05.mp3"
  "hopeful-acoustic.mp3|https://cdn.pixabay.com/audio/2022/03/24/audio_db7c5e69f0.mp3"
  "documentary-strings.mp3|https://cdn.pixabay.com/audio/2022/03/09/audio_c8c8a73467.mp3"
)

for entry in "${TRACKS[@]}"; do
  name="${entry%%|*}"
  url="${entry##*|}"
  target="${DEST}/${name}"
  if [[ -s "$target" ]]; then
    echo "skip ${name} (exists)"
    continue
  fi
  echo "fetch ${name}"
  curl -fsSL --max-time 60 "$url" -o "${target}.part"
  mv "${target}.part" "$target"
done

echo "done. ${DEST}:"
ls -lh "$DEST"
```

```bash
chmod +x scripts/seed-video-assets.sh
```

- [ ] **Step 2: Run it**

```bash
bash scripts/seed-video-assets.sh
```

Expected: 10 files in `/tmp/remotion-assets/_shared/music/`, each ≥30 KB.

**If any URL 404s:** replace that entry with another Pixabay track of similar mood. Pixabay search: https://pixabay.com/music/

- [ ] **Step 3: Commit the script (the audio files live outside the repo)**

```bash
git add scripts/seed-video-assets.sh
git commit -m "feat(video): seed-video-assets.sh — fetch 10 royalty-free music tracks"
```

### Task A4: Register `video` project + env vars

**Files:**
- Modify: `/opt/shahrzad-devops/configs/projects.json`
- Modify: `.env.example`

- [ ] **Step 1: Append `video` to projects.json**

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/opt/shahrzad-devops/configs/projects.json')
projects = json.loads(p.read_text())
if not any(x.get('name') == 'video' for x in projects):
    projects.append({"name": "video", "path": "/mnt/devopsstorage/repos/video"})
    p.write_text(json.dumps(projects, indent=2, ensure_ascii=False) + "\n")
    print("added")
else:
    print("already present")
PY
```

Expected: `added` (or `already present` on re-run).

- [ ] **Step 2: Add new env vars to `.env.example`**

Open `.env.example` and append at the end:

```bash
cat >> .env.example <<'ENV'

# ─── Video / Remotion pipeline ───────────────────────────────────────
# Remotion project root (must contain package.json + src/Root.tsx)
REMOTION_PROJECT_DIR=/mnt/devopsstorage/repos/video
# Brand & music assets root (per-brand subfolders + _shared/{fonts,music})
REMOTION_ASSETS_DIR=/tmp/remotion-assets
# ElevenLabs API key for Persian narration (eleven_multilingual_v2)
# Get from https://elevenlabs.io/app/settings/api-keys
ELEVENLABS_API_KEY=
ENV
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "feat(video): document REMOTION_*, ELEVENLABS_API_KEY env vars"
```

---

## Phase B — Remotion compositions

All B-tasks run inside `/mnt/devopsstorage/repos/video/`.

### Task B1: Replace empty scaffold and add font CSS

**Files:**
- Delete: `src/Composition.tsx`
- Create: `src/styles/fonts.css`
- Create: `src/lib/fonts.ts`
- Modify: `src/index.css`

- [ ] **Step 1: Remove placeholder composition**

```bash
cd /mnt/devopsstorage/repos/video
rm src/Composition.tsx
```

- [ ] **Step 2: Create `src/styles/fonts.css`**

```css
@font-face {
  font-family: "IRANSansX";
  font-weight: 400;
  src: url("/fonts/IRANSansX-Regular.woff2") format("woff2"),
       url("/fonts/IRANSansX-Regular.woff")  format("woff");
}
@font-face {
  font-family: "IRANSansX";
  font-weight: 700;
  src: url("/fonts/IRANSansX-Bold.woff2") format("woff2"),
       url("/fonts/IRANSansX-Bold.woff")  format("woff");
}
@font-face {
  font-family: "Vazirmatn";
  font-weight: 400;
  src: url("/fonts/Vazirmatn-Regular.woff2") format("woff2");
}
@font-face {
  font-family: "Vazirmatn";
  font-weight: 700;
  src: url("/fonts/Vazirmatn-Bold.woff2") format("woff2");
}
@font-face {
  font-family: "Vazirmatn";
  font-weight: 900;
  src: url("/fonts/Vazirmatn-Black.woff2") format("woff2");
}
```

- [ ] **Step 3: Import the font CSS from `src/index.css`**

Read the file and append the import at the top:

```css
@import "./styles/fonts.css";
/* (existing content below) */
```

- [ ] **Step 4: Create `src/lib/fonts.ts` — map heading filename to family/weight**

```ts
// Maps a brand.json heading-font filename to a CSS family/weight pair.
// Falls back to "Vazirmatn 700" when unrecognised.
export type FontPick = { family: string; weight: number };

export function pickFont(absPath: string): FontPick {
  const base = absPath.split("/").pop() ?? "";
  if (base.startsWith("IRANSansX-Bold"))     return { family: "IRANSansX", weight: 700 };
  if (base.startsWith("IRANSansX-Regular"))  return { family: "IRANSansX", weight: 400 };
  if (base.startsWith("Vazirmatn-Black"))    return { family: "Vazirmatn", weight: 900 };
  if (base.startsWith("Vazirmatn-Bold"))     return { family: "Vazirmatn", weight: 700 };
  if (base.startsWith("Vazirmatn-Regular"))  return { family: "Vazirmatn", weight: 400 };
  if (base.startsWith("Vazirmatn-Light"))    return { family: "Vazirmatn", weight: 300 };
  return { family: "Vazirmatn", weight: 700 };
}
```

- [ ] **Step 5: Commit**

```bash
cd /mnt/devopsstorage/repos/video
git add -A
git commit -m "feat(video): font CSS + face mapping for IRANSansX / Vazirmatn"
```

### Task B2: Theme types + helpers

**Files:**
- Create: `src/lib/theme.ts`

- [ ] **Step 1: Write `src/lib/theme.ts`**

```ts
export type BrandColors = {
  primary: string;
  accent: string;
  dark?: string;
  light: string;
  secondary?: string;
};

export type BrandJson = {
  name: string;
  displayName: string;
  parent?: string;
  tagline_fa: string;
  tagline_en: string;
  colors: BrandColors;
  fonts: { heading: string; body: string };
  aesthetic: string;
  voiceTone: string;
  audience?: string;
  website?: string;
  tts: { provider: "elevenlabs"; voiceId: string; modelId: string };
};

export type PromoProps = {
  brand: BrandJson;
  logoFile: string;            // public/-relative
  productImages: string[];     // public/-relative, 1..5
  headline: string;
  subheadline?: string;
  priceOrStat?: string;
  cta: string;
  durationInSeconds: 15 | 20 | 30;
  music?:     { file: string; volume: number };
  narration?: { file: string; volume: number };
};

export const FPS = 30;
export const frames = (s: number) => Math.round(s * FPS);
```

- [ ] **Step 2: Commit**

```bash
git add src/lib/theme.ts
git commit -m "feat(video): BrandJson + PromoProps type contracts"
```

### Task B3: Audio composition helper

**Files:**
- Create: `src/lib/audio.ts`

- [ ] **Step 1: Write `src/lib/audio.ts`**

```ts
import React from "react";
import { Audio, Sequence, staticFile } from "remotion";
import type { PromoProps } from "./theme";

// Renders narration on top of background music. Music ducks to 0.10 while
// narration plays (very simple — full duration if narration provided).
export const AudioLayer: React.FC<{ props: PromoProps }> = ({ props }) => {
  const hasNarration = Boolean(props.narration);
  const musicVolume  = hasNarration ? 0.10 : (props.music?.volume ?? 0.25);
  return (
    <>
      {props.music && (
        <Audio
          src={staticFile(props.music.file)}
          volume={musicVolume}
        />
      )}
      {props.narration && (
        <Sequence from={0}>
          <Audio
            src={staticFile(props.narration.file)}
            volume={props.narration.volume ?? 1.0}
          />
        </Sequence>
      )}
    </>
  );
};
```

- [ ] **Step 2: Commit**

```bash
git add src/lib/audio.ts
git commit -m "feat(video): AudioLayer — music + narration with ducking"
```

### Task B4: Six scene components

**Files:**
- Create: `src/scenes/LogoReveal.tsx`
- Create: `src/scenes/ProductHero.tsx`
- Create: `src/scenes/ProjectGallery.tsx`
- Create: `src/scenes/PriceBadge.tsx`
- Create: `src/scenes/StatsCounter.tsx`
- Create: `src/scenes/CtaEnd.tsx`

Each scene is a small, self-contained React component. All take `{ props: PromoProps }` plus their own scene-local props.

- [ ] **Step 1: `src/scenes/LogoReveal.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { pickFont } from "../lib/fonts";
import type { PromoProps } from "../lib/theme";

export const LogoReveal: React.FC<{ props: PromoProps }> = ({ props }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const font = pickFont(props.brand.fonts.heading);
  const scale = spring({ frame, fps, config: { damping: 12 } });
  const opacity = interpolate(frame, [0, 12], [0, 1], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ backgroundColor: props.brand.colors.primary, justifyContent: "center", alignItems: "center" }}>
      <Img
        src={staticFile(props.logoFile)}
        style={{ width: "55%", maxHeight: "40%", objectFit: "contain", transform: `scale(${scale})`, opacity }}
      />
      <div
        style={{
          marginTop: 40,
          color: props.brand.colors.light,
          fontFamily: font.family,
          fontWeight: font.weight,
          fontSize: 48,
          opacity: interpolate(frame, [20, 35], [0, 1], { extrapolateRight: "clamp" }),
          textAlign: "center",
        }}
      >
        {props.brand.displayName}
      </div>
    </AbsoluteFill>
  );
};
```

- [ ] **Step 2: `src/scenes/ProductHero.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import { pickFont } from "../lib/fonts";
import type { PromoProps } from "../lib/theme";

export const ProductHero: React.FC<{ props: PromoProps }> = ({ props }) => {
  const frame = useCurrentFrame();
  const font  = pickFont(props.brand.fonts.heading);
  const slide = interpolate(frame, [0, 15], [80, 0], { extrapolateRight: "clamp" });
  const opacity = interpolate(frame, [0, 12], [0, 1], { extrapolateRight: "clamp" });
  const subOpacity = interpolate(frame, [18, 30], [0, 1], { extrapolateRight: "clamp" });
  const hero = props.productImages[0];
  return (
    <AbsoluteFill style={{ backgroundColor: props.brand.colors.light, padding: 60 }}>
      {hero && (
        <Img
          src={staticFile(hero)}
          style={{
            width: "100%",
            height: "55%",
            objectFit: "contain",
            transform: `translateY(${slide}px)`,
            opacity,
          }}
        />
      )}
      <div
        style={{
          marginTop: 30,
          color: props.brand.colors.primary,
          fontFamily: font.family,
          fontWeight: font.weight,
          fontSize: 64,
          textAlign: "center",
          opacity,
        }}
      >
        {props.headline}
      </div>
      {props.subheadline && (
        <div
          style={{
            marginTop: 16,
            color: props.brand.colors.dark ?? "#222",
            fontFamily: font.family,
            fontWeight: 400,
            fontSize: 36,
            textAlign: "center",
            opacity: subOpacity,
          }}
        >
          {props.subheadline}
        </div>
      )}
    </AbsoluteFill>
  );
};
```

- [ ] **Step 3: `src/scenes/ProjectGallery.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, Img, Sequence, interpolate, staticFile, useCurrentFrame } from "remotion";
import type { PromoProps } from "../lib/theme";

export const ProjectGallery: React.FC<{ props: PromoProps; sceneDurationInFrames: number }> = ({
  props,
  sceneDurationInFrames,
}) => {
  const images = props.productImages.slice(0, 5);
  const perImage = Math.floor(sceneDurationInFrames / Math.max(images.length, 1));
  return (
    <AbsoluteFill style={{ backgroundColor: props.brand.colors.dark ?? "#111" }}>
      {images.map((img, i) => (
        <Sequence key={img + i} from={i * perImage} durationInFrames={perImage}>
          <PanZoom file={img} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};

const PanZoom: React.FC<{ file: string }> = ({ file }) => {
  const frame = useCurrentFrame();
  const scale = interpolate(frame, [0, 90], [1.0, 1.12], { extrapolateRight: "clamp" });
  const opacity = interpolate(frame, [0, 8, 75, 90], [0, 1, 1, 0], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", opacity }}>
      <Img src={staticFile(file)} style={{ width: "100%", height: "100%", objectFit: "cover", transform: `scale(${scale})` }} />
    </AbsoluteFill>
  );
};
```

- [ ] **Step 4: `src/scenes/PriceBadge.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { pickFont } from "../lib/fonts";
import type { PromoProps } from "../lib/theme";

export const PriceBadge: React.FC<{ props: PromoProps }> = ({ props }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const font = pickFont(props.brand.fonts.heading);
  const pop = spring({ frame, fps, config: { damping: 8, stiffness: 180 } });
  const opacity = interpolate(frame, [0, 10], [0, 1], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ backgroundColor: props.brand.colors.primary, justifyContent: "center", alignItems: "center" }}>
      <div
        style={{
          backgroundColor: props.brand.colors.accent,
          color: props.brand.colors.dark ?? "#111",
          borderRadius: 32,
          padding: "60px 80px",
          transform: `scale(${pop})`,
          opacity,
          fontFamily: font.family,
          fontWeight: font.weight,
          fontSize: 72,
          textAlign: "center",
          maxWidth: "80%",
        }}
      >
        {props.priceOrStat ?? props.headline}
      </div>
    </AbsoluteFill>
  );
};
```

- [ ] **Step 5: `src/scenes/StatsCounter.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { pickFont } from "../lib/fonts";
import type { PromoProps } from "../lib/theme";

// Renders the first number found in priceOrStat as a counting-up integer.
// Non-numeric stats render as static text.
export const StatsCounter: React.FC<{ props: PromoProps; durationInFrames: number }> = ({
  props,
  durationInFrames,
}) => {
  const frame = useCurrentFrame();
  const font = pickFont(props.brand.fonts.heading);
  const raw = props.priceOrStat ?? "";
  const match = raw.match(/(\d[\d,]*)/);
  const target = match ? parseInt(match[1].replace(/,/g, ""), 10) : 0;
  const value  = match
    ? Math.round(interpolate(frame, [0, durationInFrames * 0.7], [0, target], { extrapolateRight: "clamp" }))
    : null;
  const label  = match ? raw.replace(match[1], value!.toLocaleString("fa-IR")) : raw;
  return (
    <AbsoluteFill style={{ backgroundColor: props.brand.colors.light, justifyContent: "center", alignItems: "center" }}>
      <div style={{ color: props.brand.colors.primary, fontFamily: font.family, fontWeight: font.weight, fontSize: 120 }}>
        {label}
      </div>
    </AbsoluteFill>
  );
};
```

- [ ] **Step 6: `src/scenes/CtaEnd.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { pickFont } from "../lib/fonts";
import type { PromoProps } from "../lib/theme";

export const CtaEnd: React.FC<{ props: PromoProps }> = ({ props }) => {
  const frame = useCurrentFrame();
  const font = pickFont(props.brand.fonts.heading);
  const opacity = interpolate(frame, [0, 12], [0, 1], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ backgroundColor: props.brand.colors.primary, justifyContent: "center", alignItems: "center", opacity }}>
      <div style={{ color: props.brand.colors.light, fontFamily: font.family, fontWeight: font.weight, fontSize: 88, textAlign: "center" }}>
        {props.cta}
      </div>
      {props.brand.website && (
        <div style={{ marginTop: 24, color: props.brand.colors.accent, fontFamily: font.family, fontWeight: 400, fontSize: 40 }}>
          {props.brand.website}
        </div>
      )}
    </AbsoluteFill>
  );
};
```

- [ ] **Step 7: Commit all six scenes**

```bash
git add src/scenes
git commit -m "feat(video): six promo scene components"
```

### Task B5: Two compositions

**Files:**
- Create: `src/compositions/ProductPromo.tsx`
- Create: `src/compositions/ServicePromo.tsx`

- [ ] **Step 1: `src/compositions/ProductPromo.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, Sequence, useVideoConfig } from "remotion";
import { LogoReveal } from "../scenes/LogoReveal";
import { ProductHero } from "../scenes/ProductHero";
import { PriceBadge } from "../scenes/PriceBadge";
import { CtaEnd } from "../scenes/CtaEnd";
import { AudioLayer } from "../lib/audio";
import type { PromoProps } from "../lib/theme";

// Scene split (in fractions of total duration):
//   LogoReveal  0.00 → 0.15
//   ProductHero 0.15 → 0.55
//   PriceBadge  0.55 → 0.80
//   CtaEnd      0.80 → 1.00
export const ProductPromo: React.FC<PromoProps> = (props) => {
  const { durationInFrames } = useVideoConfig();
  const f = (frac: number) => Math.round(durationInFrames * frac);
  return (
    <AbsoluteFill>
      <Sequence from={0}        durationInFrames={f(0.15)}>            <LogoReveal  props={props} /></Sequence>
      <Sequence from={f(0.15)}  durationInFrames={f(0.55) - f(0.15)}>  <ProductHero props={props} /></Sequence>
      <Sequence from={f(0.55)}  durationInFrames={f(0.80) - f(0.55)}>  <PriceBadge  props={props} /></Sequence>
      <Sequence from={f(0.80)}  durationInFrames={durationInFrames - f(0.80)}><CtaEnd props={props} /></Sequence>
      <AudioLayer props={props} />
    </AbsoluteFill>
  );
};
```

- [ ] **Step 2: `src/compositions/ServicePromo.tsx`**

```tsx
import React from "react";
import { AbsoluteFill, Sequence, useVideoConfig } from "remotion";
import { LogoReveal } from "../scenes/LogoReveal";
import { ProjectGallery } from "../scenes/ProjectGallery";
import { StatsCounter } from "../scenes/StatsCounter";
import { CtaEnd } from "../scenes/CtaEnd";
import { AudioLayer } from "../lib/audio";
import type { PromoProps } from "../lib/theme";

export const ServicePromo: React.FC<PromoProps> = (props) => {
  const { durationInFrames } = useVideoConfig();
  const f = (frac: number) => Math.round(durationInFrames * frac);
  const galleryDur = f(0.65) - f(0.15);
  const statsDur   = f(0.85) - f(0.65);
  return (
    <AbsoluteFill>
      <Sequence from={0}       durationInFrames={f(0.15)}>                            <LogoReveal props={props} /></Sequence>
      <Sequence from={f(0.15)} durationInFrames={galleryDur}>                          <ProjectGallery props={props} sceneDurationInFrames={galleryDur} /></Sequence>
      <Sequence from={f(0.65)} durationInFrames={statsDur}>                            <StatsCounter props={props} durationInFrames={statsDur} /></Sequence>
      <Sequence from={f(0.85)} durationInFrames={durationInFrames - f(0.85)}>          <CtaEnd props={props} /></Sequence>
      <AudioLayer props={props} />
    </AbsoluteFill>
  );
};
```

- [ ] **Step 3: Commit**

```bash
git add src/compositions
git commit -m "feat(video): ProductPromo + ServicePromo compositions"
```

### Task B6: Register compositions in `Root.tsx`

**Files:**
- Modify: `src/Root.tsx`

- [ ] **Step 1: Replace `src/Root.tsx`**

```tsx
import "./index.css";
import { Composition } from "remotion";
import { ProductPromo } from "./compositions/ProductPromo";
import { ServicePromo } from "./compositions/ServicePromo";
import type { PromoProps } from "./lib/theme";

const DEFAULT_PROPS: PromoProps = {
  brand: {
    name: "DemoBrand",
    displayName: "Demo Brand",
    tagline_fa: "نمونه",
    tagline_en: "Demo",
    colors: { primary: "#0B486B", accent: "#FED03D", dark: "#333333", light: "#FFFFFF" },
    fonts: {
      heading: "/tmp/remotion-assets/_shared/fonts/Vazirmatn-Bold.woff2",
      body:    "/tmp/remotion-assets/_shared/fonts/Vazirmatn-Regular.woff2",
    },
    aesthetic: "demo",
    voiceTone: "demo",
    tts: { provider: "elevenlabs", voiceId: "demo", modelId: "eleven_multilingual_v2" },
  },
  logoFile: "demo-logo.png",
  productImages: ["demo-product.png"],
  headline: "نمونه تیتر",
  subheadline: "زیرعنوان",
  priceOrStat: "۳۰٪ صرفه‌جویی",
  cta: "اطلاعات بیشتر",
  durationInSeconds: 20,
};

const sec = (n: number) => n * 30;

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition id="ProductPromo_Vertical"
                   component={ProductPromo}
                   durationInFrames={sec(20)}
                   fps={30}
                   width={1080} height={1920}
                   defaultProps={DEFAULT_PROPS} />
      <Composition id="ProductPromo_Horizontal"
                   component={ProductPromo}
                   durationInFrames={sec(20)}
                   fps={30}
                   width={1920} height={1080}
                   defaultProps={DEFAULT_PROPS} />
      <Composition id="ServicePromo_Vertical"
                   component={ServicePromo}
                   durationInFrames={sec(20)}
                   fps={30}
                   width={1080} height={1920}
                   defaultProps={DEFAULT_PROPS} />
      <Composition id="ServicePromo_Horizontal"
                   component={ServicePromo}
                   durationInFrames={sec(20)}
                   fps={30}
                   width={1920} height={1080}
                   defaultProps={DEFAULT_PROPS} />
    </>
  );
};
```

- [ ] **Step 2: Type-check the Remotion project**

```bash
cd /mnt/devopsstorage/repos/video
npm run lint
```

Expected: exits 0. If errors, fix them before committing.

- [ ] **Step 3: Commit**

```bash
git add src/Root.tsx
git commit -m "feat(video): register 4 compositions (2 templates × 2 aspects)"
```

### Task B7: Sample render harness

**Files:**
- Create: `/mnt/devopsstorage/repos/video/scripts/render-samples.sh`
- Create: `/mnt/devopsstorage/repos/video/public/demo-logo.png` (1×1 PNG placeholder)
- Create: `/mnt/devopsstorage/repos/video/public/demo-product.png` (1×1 PNG placeholder)

- [ ] **Step 1: Create placeholder PNGs (transparent 1×1)**

```bash
cd /mnt/devopsstorage/repos/video
mkdir -p public
python3 - <<'PY'
import base64, pathlib
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNgAAIAAAUAAen+9G8AAAAASUVORK5CYII="
)
for name in ("demo-logo.png", "demo-product.png"):
    pathlib.Path("public", name).write_bytes(png)
PY
```

- [ ] **Step 2: Write `scripts/render-samples.sh`**

```bash
#!/usr/bin/env bash
# Renders the 4 default-prop sample videos to renders/ for smoke-testing the
# Remotion pipeline (no brand assets, no TTS — just defaults).
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p renders

SAMPLES=(
  ProductPromo_Vertical
  ProductPromo_Horizontal
  ServicePromo_Vertical
  ServicePromo_Horizontal
)

for comp in "${SAMPLES[@]}"; do
  out="renders/sample-${comp}.mp4"
  echo "▶ rendering ${comp} → ${out}"
  npx remotion render "${comp}" "${out}"
done

echo "✔ all 4 samples rendered:"
ls -lh renders/sample-*.mp4
```

```bash
chmod +x scripts/render-samples.sh
```

- [ ] **Step 3: Run the sample render**

```bash
cd /mnt/devopsstorage/repos/video
bash scripts/render-samples.sh
```

Expected: 4 mp4 files in `renders/`, each 5-15 MB. If any fail, fix the underlying scene before continuing.

- [ ] **Step 4: Commit (script + demo PNGs only; mp4 outputs gitignored)**

```bash
echo "/renders/" >> .gitignore  # if not already
git add .gitignore scripts/render-samples.sh public/demo-logo.png public/demo-product.png
git commit -m "feat(video): sample render harness + transparent demo PNGs"
```

---

## Phase C — Python video_module

All C-tasks run inside the bot repo `/tmp/claude-sessions/106021080_videobot/` (or wherever the worktree is). Follow the existing test pattern from `tests/test_load_projects.py`.

### Task C1: `assets.py` — read brand.json, list files

**Files:**
- Create: `video_module/__init__.py`
- Create: `video_module/assets.py`
- Create: `tests/test_video_assets.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_video_assets.py
"""Tests for video_module.assets — brand.json parsing + file listing."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_module import assets  # noqa: E402


def _seed(tmp_path: Path) -> Path:
    """Create a minimal valid brand.json under a faux assets root."""
    root = tmp_path / "assets"
    brand_dir = root / "TestBrand"
    (brand_dir / "logo").mkdir(parents=True)
    (brand_dir / "logo" / "main.png").write_bytes(b"\x89PNG\r\n")
    (brand_dir / "products").mkdir()
    (brand_dir / "products" / "p1.jpg").write_bytes(b"\xff\xd8\xff")
    (brand_dir / "brand.json").write_text(json.dumps({
        "name": "TestBrand",
        "displayName": "Test",
        "tagline_fa": "آزمایش",
        "tagline_en": "Test",
        "colors": {"primary": "#000", "accent": "#fff", "light": "#fff"},
        "fonts":  {"heading": "/x/h.woff2", "body": "/x/b.woff2"},
        "aesthetic": "x",
        "voiceTone": "x",
        "tts": {"provider": "elevenlabs", "voiceId": "v1", "modelId": "eleven_multilingual_v2"},
    }))
    return root


def test_list_brands(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    assert assets.list_brands(root) == ["TestBrand"]


def test_load_brand_returns_dict(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    brand = assets.load_brand(root, "TestBrand")
    assert brand["name"] == "TestBrand"
    assert brand["tts"]["voiceId"] == "v1"


def test_load_brand_missing_raises(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    try:
        assets.load_brand(root, "DoesNotExist")
    except FileNotFoundError as e:
        assert "DoesNotExist" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_list_assets_filters_by_kind(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    logos    = assets.list_assets(root, "TestBrand", "logo")
    products = assets.list_assets(root, "TestBrand", "products")
    assert logos    == ["main.png"]
    assert products == ["p1.jpg"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_video_assets.py -v
```

Expected: ImportError on `from video_module import assets`.

- [ ] **Step 3: Write `video_module/__init__.py`** (empty package marker)

```python
"""Telegram /video command — Remotion render pipeline."""
```

- [ ] **Step 4: Write `video_module/assets.py`**

```python
"""Read brand.json and list per-brand asset files under REMOTION_ASSETS_DIR.

Layout expected under root:

    root/<Brand>/brand.json
    root/<Brand>/{logo,products,projects}/*

The module never writes — it is read-only. Music uploads go through a
separate helper in handlers.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

ALLOWED_KINDS = ("logo", "products", "projects")
IMAGE_EXTS    = {".png", ".jpg", ".jpeg", ".webp", ".svg"}


def list_brands(root: Path) -> list[str]:
    """Return sorted brand folder names (any folder containing a brand.json)."""
    root = Path(root)
    if not root.exists():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name != "_shared" and (child / "brand.json").is_file():
            out.append(child.name)
    return out


def load_brand(root: Path, brand: str) -> dict:
    """Parse and return the brand.json dict. Raises FileNotFoundError if missing."""
    path = Path(root) / brand / "brand.json"
    if not path.is_file():
        raise FileNotFoundError(f"brand.json not found for {brand!r} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_assets(root: Path, brand: str, kind: str) -> list[str]:
    """Return filenames (not full paths) under root/brand/kind/, image types only.

    Filters silently on extension. Returns sorted list, empty if folder missing.
    """
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {ALLOWED_KINDS}, got {kind!r}")
    folder = Path(root) / brand / kind
    if not folder.is_dir():
        return []
    return sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def list_music(root: Path) -> list[str]:
    """List mp3/wav filenames in _shared/music/."""
    folder = Path(root) / "_shared" / "music"
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.suffix.lower() in {".mp3", ".wav"})


def resolve_asset(root: Path, brand: str, kind: str, filename: str) -> Path:
    """Validate and return absolute path. Rejects path traversal."""
    if "/" in filename or ".." in filename:
        raise ValueError(f"invalid filename: {filename!r}")
    full = (Path(root) / brand / kind / filename).resolve()
    expected_root = (Path(root) / brand / kind).resolve()
    if expected_root not in full.parents:
        raise ValueError(f"path escape attempt: {filename!r}")
    if not full.is_file():
        raise FileNotFoundError(full)
    return full
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_video_assets.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add video_module/__init__.py video_module/assets.py tests/test_video_assets.py
git commit -m "feat(video): assets module — brand.json reader + file lister"
```

### Task C2: `jobs.py` — atomic JSON job state

**Files:**
- Create: `video_module/jobs.py`
- Create: `tests/test_video_jobs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_video_jobs.py
"""Tests for video_module.jobs — atomic JSON job persistence."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_module import jobs  # noqa: E402


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    store = jobs.JobStore(tmp_path)
    store.save(42, {"step": "headline", "brand": "AlumGlass"})
    assert store.load(42) == {"step": "headline", "brand": "AlumGlass"}


def test_load_missing_returns_none(tmp_path: Path) -> None:
    store = jobs.JobStore(tmp_path)
    assert store.load(99) is None


def test_delete_removes_file(tmp_path: Path) -> None:
    store = jobs.JobStore(tmp_path)
    store.save(7, {"x": 1})
    assert store.load(7) == {"x": 1}
    store.delete(7)
    assert store.load(7) is None


def test_atomic_write_uses_tmp_rename(tmp_path: Path) -> None:
    """The on-disk file should never appear partially written —
    a crashed write must leave the previous state intact."""
    store = jobs.JobStore(tmp_path)
    store.save(1, {"v": "old"})
    # Simulate by writing again; tmp file must not exist afterwards
    store.save(1, {"v": "new"})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert store.load(1) == {"v": "new"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_video_jobs.py -v
```

Expected: ImportError.

- [ ] **Step 3: Write `video_module/jobs.py`**

```python
"""JSON-on-disk job state for /video wizard. One file per chat_id.

Stored at: <root>/<chat_id>.json
Writes are atomic via tmp + os.replace so a crashed write never leaves a
partial file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id: int) -> Path:
        return self.root / f"{chat_id}.json"

    def save(self, chat_id: int, state: dict) -> None:
        p = self._path(chat_id)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def load(self, chat_id: int) -> Optional[dict]:
        p = self._path(chat_id)
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def delete(self, chat_id: int) -> None:
        p = self._path(chat_id)
        if p.is_file():
            p.unlink()
```

- [ ] **Step 4: Run tests, verify pass**

```bash
pytest tests/test_video_jobs.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add video_module/jobs.py tests/test_video_jobs.py
git commit -m "feat(video): JobStore — atomic JSON wizard state per chat_id"
```

### Task C3: `props.py` — build PromoProps JSON

**Files:**
- Create: `video_module/props.py`
- Create: `tests/test_video_props.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_video_props.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_module import props  # noqa: E402


def test_build_minimal() -> None:
    brand = {"name": "X", "displayName": "X",
             "tagline_fa": "", "tagline_en": "",
             "colors": {"primary": "#000", "accent": "#fff", "light": "#fff"},
             "fonts": {"heading": "/h.woff2", "body": "/b.woff2"},
             "aesthetic": "", "voiceTone": "",
             "tts": {"provider": "elevenlabs", "voiceId": "v", "modelId": "m"}}
    state = {
        "brand": "X", "template": "ProductPromo", "aspect": "9:16",
        "logo_file": "logo.png", "product_files": ["p1.png"],
        "headline": "Hi", "cta": "Buy",
        "duration": 20,
    }
    p = props.build(brand, state)
    assert p["brand"]["name"]      == "X"
    assert p["logoFile"]           == "logos/logo.png"
    assert p["productImages"]      == ["products/p1.png"]
    assert p["headline"]           == "Hi"
    assert p["cta"]                == "Buy"
    assert p["durationInSeconds"]  == 20


def test_composition_id_resolution() -> None:
    assert props.composition_id("ProductPromo", "9:16")  == "ProductPromo_Vertical"
    assert props.composition_id("ProductPromo", "16:9")  == "ProductPromo_Horizontal"
    assert props.composition_id("ServicePromo", "9:16")  == "ServicePromo_Vertical"
    assert props.composition_id("ServicePromo", "16:9")  == "ServicePromo_Horizontal"


def test_invalid_aspect_raises() -> None:
    try:
        props.composition_id("ProductPromo", "1:1")
    except ValueError:
        return
    raise AssertionError("expected ValueError on 1:1")


def test_music_and_narration_attached_when_present() -> None:
    brand = {"name": "X", "displayName": "X",
             "tagline_fa": "", "tagline_en": "",
             "colors": {"primary": "#000", "accent": "#fff", "light": "#fff"},
             "fonts": {"heading": "/h.woff2", "body": "/b.woff2"},
             "aesthetic": "", "voiceTone": "",
             "tts": {"provider": "elevenlabs", "voiceId": "v", "modelId": "m"}}
    state = {
        "brand": "X", "template": "ProductPromo", "aspect": "9:16",
        "logo_file": "l.png", "product_files": ["p.png"],
        "headline": "h", "cta": "c", "duration": 15,
        "music_file": "cinematic-tech.mp3",
        "narration_file": "narration.mp3",
    }
    p = props.build(brand, state)
    assert p["music"]["file"]     == "music/cinematic-tech.mp3"
    assert p["narration"]["file"] == "narration.mp3"
```

- [ ] **Step 2: Run, verify fails**

```bash
pytest tests/test_video_props.py -v
```

Expected: ImportError.

- [ ] **Step 3: Write `video_module/props.py`**

```python
"""Build PromoProps JSON from collected wizard state.

The Remotion side reads the JSON via `--props=<file>` and the paths inside
must be **relative to public/** because Remotion resolves staticFile()
against that root.
"""
from __future__ import annotations

from typing import Any


_COMPOSITION_MAP = {
    ("ProductPromo", "9:16"): "ProductPromo_Vertical",
    ("ProductPromo", "16:9"): "ProductPromo_Horizontal",
    ("ServicePromo", "9:16"): "ServicePromo_Vertical",
    ("ServicePromo", "16:9"): "ServicePromo_Horizontal",
}


def composition_id(template: str, aspect: str) -> str:
    try:
        return _COMPOSITION_MAP[(template, aspect)]
    except KeyError as e:
        raise ValueError(f"no composition for {template!r} × {aspect!r}") from e


def build(brand: dict, state: dict) -> dict[str, Any]:
    """Return a dict ready to JSON-encode and pass to Remotion.

    Path conventions (all relative to public/):
        logoFile        : logos/<filename>
        productImages   : products/<filename>
        music.file      : music/<filename>
        narration.file  : <filename>   (rendered into public/ root by renderer.py)
    """
    out: dict[str, Any] = {
        "brand":         brand,
        "logoFile":      f"logos/{state['logo_file']}",
        "productImages": [f"products/{f}" for f in state["product_files"]],
        "headline":      state["headline"],
        "cta":           state.get("cta", "اطلاعات بیشتر"),
        "durationInSeconds": int(state["duration"]),
    }
    if state.get("subheadline"):
        out["subheadline"] = state["subheadline"]
    if state.get("price_or_stat"):
        out["priceOrStat"] = state["price_or_stat"]
    if state.get("music_file"):
        out["music"] = {"file": f"music/{state['music_file']}", "volume": 0.25}
    if state.get("narration_file"):
        out["narration"] = {"file": state["narration_file"], "volume": 1.0}
    return out
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_video_props.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add video_module/props.py tests/test_video_props.py
git commit -m "feat(video): build PromoProps from wizard state + composition map"
```

### Task C4: `tts.py` — ElevenLabs client

**Files:**
- Create: `video_module/tts.py`
- Create: `tests/test_video_tts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_video_tts.py
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_module import tts  # noqa: E402


@pytest.mark.asyncio
async def test_synthesize_writes_mp3(tmp_path: Path) -> None:
    fake_session = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read = AsyncMock(return_value=b"ID3FAKEAUDIO")
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)
    fake_session.post = MagicMock(return_value=fake_resp)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    out = tmp_path / "narration.mp3"
    with patch.object(tts.aiohttp, "ClientSession", return_value=fake_session):
        await tts.synthesize(
            text="سلام جهان",
            voice_id="vid",
            model_id="eleven_multilingual_v2",
            api_key="sk-test",
            output_path=out,
        )
    assert out.read_bytes() == b"ID3FAKEAUDIO"


@pytest.mark.asyncio
async def test_synthesize_raises_on_error(tmp_path: Path) -> None:
    fake_session = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status = 401
    fake_resp.text = AsyncMock(return_value="invalid api key")
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)
    fake_session.post = MagicMock(return_value=fake_resp)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    with patch.object(tts.aiohttp, "ClientSession", return_value=fake_session):
        with pytest.raises(tts.TtsError) as exc:
            await tts.synthesize(
                text="x", voice_id="v", model_id="m",
                api_key="sk", output_path=tmp_path / "n.mp3",
            )
    assert "401" in str(exc.value)
```

- [ ] **Step 2: Install pytest-asyncio if missing**

```bash
pip install pytest-asyncio
```

Then add to `requirements.txt` (dev section) and create `pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
```

- [ ] **Step 3: Run, verify fails**

```bash
pytest tests/test_video_tts.py -v
```

Expected: ImportError on `video_module.tts`.

- [ ] **Step 4: Write `video_module/tts.py`**

```python
"""ElevenLabs TTS client — Persian narration via eleven_multilingual_v2.

Writes mp3 bytes to the given output path.
"""
from __future__ import annotations

from pathlib import Path

import aiohttp


class TtsError(RuntimeError):
    """Raised when the ElevenLabs API returns a non-200 status."""


async def synthesize(
    *,
    text: str,
    voice_id: str,
    model_id: str,
    api_key: str,
    output_path: Path,
    timeout_seconds: int = 60,
) -> None:
    """POST text to ElevenLabs and write the mp3 response to output_path.

    Raises TtsError on non-200 responses with the status and body tail.
    """
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key":   api_key,
        "Content-Type": "application/json",
        "Accept":       "audio/mpeg",
    }
    payload = {
        "text":          text,
        "model_id":      model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise TtsError(f"ElevenLabs returned {resp.status}: {body[:300]}")
            data = await resp.read()
    Path(output_path).write_bytes(data)
```

- [ ] **Step 5: Run, verify pass**

```bash
pytest tests/test_video_tts.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add video_module/tts.py tests/test_video_tts.py pytest.ini requirements.txt
git commit -m "feat(video): ElevenLabs TTS client (eleven_multilingual_v2)"
```

### Task C5: `renderer.py` — Remotion subprocess wrapper

**Files:**
- Create: `video_module/renderer.py`

This task has no unit tests — the function shells out to npx and writes large mp4s. We verify via the smoke-test in Phase D.

- [ ] **Step 1: Write `video_module/renderer.py`**

```python
"""Run `npx remotion render` as an asyncio subprocess.

Responsible for:
  • copying brand-specific assets into <project>/public/ (logos/, products/,
    music/, narration mp3)
  • writing a temporary props.json
  • invoking the CLI
  • collecting the mp4 + a sibling .log.json

Public/ is **wiped of per-job content** before each render so leftover files
from a previous job never leak in. The fonts/ folder is preserved.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RenderJob:
    composition_id: str
    props:          dict
    logo_src:       Path
    product_srcs:   list[Path]
    music_src:      Optional[Path] = None
    narration_src:  Optional[Path] = None
    output_path:    Path = field(default_factory=lambda: Path("render.mp4"))


@dataclass
class RenderResult:
    output_path:     Path
    duration_seconds: float
    stderr_tail:     str
    exit_code:       int


class RenderError(RuntimeError):
    pass


async def render(
    project_dir: Path,
    job: RenderJob,
    *,
    timeout_seconds: int = 600,
) -> RenderResult:
    """Stage assets, write props.json, run `npx remotion render`."""
    project_dir = Path(project_dir)
    public = project_dir / "public"
    _stage_public(public, job)

    props_file = public.parent / ".video-props.json"
    props_file.write_text(json.dumps(job.props, ensure_ascii=False), encoding="utf-8")

    cmd = [
        "npx", "remotion", "render",
        "src/index.ts", job.composition_id, str(job.output_path),
        "--props", str(props_file),
        "--overwrite",
    ]
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RenderError(f"render timeout after {timeout_seconds}s for {job.composition_id}")
    duration = time.monotonic() - start
    stderr_text = (stderr or b"").decode("utf-8", errors="replace")
    tail = "\n".join(stderr_text.splitlines()[-30:])

    if proc.returncode != 0:
        raise RenderError(f"remotion render exited {proc.returncode}\n{tail}")

    if not job.output_path.is_file():
        raise RenderError(f"remotion exited 0 but output missing: {job.output_path}")

    # Sibling log file
    log_path = job.output_path.with_suffix(".log.json")
    log_path.write_text(json.dumps({
        "composition_id": job.composition_id,
        "props":           job.props,
        "duration_s":      round(duration, 2),
        "exit_code":       proc.returncode,
        "stderr_tail":     tail,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return RenderResult(
        output_path=job.output_path,
        duration_seconds=duration,
        stderr_tail=tail,
        exit_code=proc.returncode or 0,
    )


def _stage_public(public: Path, job: RenderJob) -> None:
    """Reset per-job folders under public/ and copy fresh assets in."""
    for sub in ("logos", "products", "music"):
        target = public / sub
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    # Drop stale narration mp3 if any (it lives at public root)
    for f in public.glob("narration*.mp3"):
        f.unlink()

    shutil.copy(job.logo_src,    public / "logos"    / job.logo_src.name)
    for p in job.product_srcs:
        shutil.copy(p, public / "products" / p.name)
    if job.music_src:
        shutil.copy(job.music_src, public / "music" / job.music_src.name)
    if job.narration_src:
        shutil.copy(job.narration_src, public / job.narration_src.name)
```

- [ ] **Step 2: Commit**

```bash
git add video_module/renderer.py
git commit -m "feat(video): renderer — asset staging + npx remotion subprocess"
```

### Task C6: `wizard.py` — ConversationHandler FSM

**Files:**
- Create: `video_module/wizard.py`

This file is integration-heavy (Telegram API); we'll exercise it via the smoke test rather than unit tests.

- [ ] **Step 1: Write `video_module/wizard.py`**

```python
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


async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback handled in handlers.py (it owns the renderer + delivery)."""
    raise NotImplementedError  # wired in handlers.py


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    _wipe(ctx, chat_id)
    await update.effective_message.reply_text("لغو شد.")
    return ConversationHandler.END
```

- [ ] **Step 2: Commit**

```bash
git add video_module/wizard.py
git commit -m "feat(video): ConversationHandler wizard FSM (13 states)"
```

### Task C7: `handlers.py` — register everything and own /confirm

**Files:**
- Create: `video_module/handlers.py`

- [ ] **Step 1: Write `video_module/handlers.py`**

```python
"""Glue: register the /video conversation, /assets, /upload_music, /renders.

Also owns the final confirm → render → deliver step (because that requires
all four sibling modules: assets, props, tts, renderer).
"""
from __future__ import annotations

import asyncio
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
    target = _assets_root() / "_shared" / "music" / _safe_name(Path(name).stem) + Path(name).suffix
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
```

- [ ] **Step 2: Commit**

```bash
git add video_module/handlers.py
git commit -m "feat(video): register conversation, /assets, /upload_music, /renders"
```

---

## Phase D — Bot integration + verification + docs

### Task D1: Wire `video_module` into `bot.py`

**Files:**
- Modify: `bot.py:2965-2978` (BotCommand list)
- Modify: `bot.py:3224+` (handler registration)

- [ ] **Step 1: Add import near other module imports**

Find the import block near the top of `bot.py` (after `from log_filters import install_token_redact_filter`) and add:

```python
from video_module.handlers import register as register_video_module
```

- [ ] **Step 2: Add 4 BotCommand entries**

Inside the `await app.bot.set_my_commands([...])` block at line ~2965, append (after `nightwatch_last`):

```python
        BotCommand("video",        "\U0001f3ac ساخت ویدئو"),
        BotCommand("assets",       "\U0001f4c1 لیست asset برند"),
        BotCommand("upload_music", "\U0001f3b5 آپلود موزیک"),
        BotCommand("renders",      "\U0001f4fa آخرین render‌ها"),
```

- [ ] **Step 3: Call `register_video_module(app)`**

Find the handler registration block around `bot.py:3224` (where `app.add_handler(CommandHandler("start", cmd_start))` etc. live). After the last `add_handler` line, add:

```python
    register_video_module(app)
```

- [ ] **Step 4: Verify bot.py imports cleanly**

```bash
python3 -c "import bot; print('OK')" 2>&1 | tail -5
```

Expected: `OK` (or harmless logging warnings — no Python errors).

- [ ] **Step 5: Commit**

```bash
git add bot.py
git commit -m "feat(bot): wire /video, /assets, /upload_music, /renders commands"
```

### Task D2: Smoke test end-to-end with seed assets

**Files:** none modified — verification only.

- [ ] **Step 1: Add a smoke logo + product to AlumGlass**

If real brand assets aren't yet copied in, drop placeholders:

```bash
cp /mnt/devopsstorage/repos/video/public/demo-logo.png    /tmp/remotion-assets/AlumGlass/logo/main.png
cp /mnt/devopsstorage/repos/video/public/demo-product.png /tmp/remotion-assets/AlumGlass/products/p1.png
```

- [ ] **Step 2: Run pytest for all video_module tests**

```bash
pytest tests/test_video_assets.py tests/test_video_jobs.py tests/test_video_props.py tests/test_video_tts.py -v
```

Expected: all pass.

- [ ] **Step 3: Manual Telegram E2E (operator runs this from phone/desktop)**

Steps to execute via Telegram client (each must produce the described result):

1. `/video` → menu with 3 brands appears
2. Tap AlumGlass → tap "🎯 Product Promo" → tap "📱 9:16 عمودی"
3. Tap `main.png` for logo → tap `p1.png` for product → tap "✅ انتخاب کافیه"
4. Type headline: `نمونه ویدئو`
5. Type `/skip` twice (subhead, price)
6. Type `/skip` for CTA (default applies)
7. Tap `۱۵ ثانیه`
8. Tap `🔇 بدون موزیک`
9. Tap `🤐 بدون narration`
10. Confirm screen appears → tap `✅ Render`
11. Within ~2-3 minutes, an mp4 (≤20 MB) is sent back with the caption containing brand/template/aspect/duration.

If step 11 fails: check `journalctl -u <bot-unit> -n 100`, look at `renders/*.log.json` for the most recent job.

- [ ] **Step 4: Verify the log file was written**

```bash
ls -lt /mnt/devopsstorage/repos/video/renders/*.log.json | head -3
```

Expected: a recent .log.json sibling to the mp4 with composition_id, props, duration_s, exit_code: 0.

### Task D3: Documentation

**Files:**
- Create: `docs/VIDEO_BOT.md`
- Create: `docs/VIDEO_BOT_SETUP.md`
- Modify: `CHANGELOG.md`
- Modify: `/mnt/devopsstorage/repos/video/README.md`

- [ ] **Step 1: Write `docs/VIDEO_BOT.md`** (user guide)

```markdown
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
```

- [ ] **Step 2: Write `docs/VIDEO_BOT_SETUP.md`** (DevOps setup)

```markdown
# /video — DevOps Setup

## Prerequisites

- Node ≥ 20 (for `npx remotion render`)
- Python 3.10+
- ffmpeg (bundled with Remotion's Chromium download)
- A Remotion project at `/mnt/devopsstorage/repos/video/` (this repo).
- An ElevenLabs API key (https://elevenlabs.io/app/settings/api-keys).

## One-time setup

### 1. Asset folders + brand.json

```bash
# Folder skeleton — created by scripts/seed-video-assets.sh
bash /opt/shahrzad-devops/repos/ClaudeCodeTelegramBot/scripts/seed-video-assets.sh
```

This creates `/tmp/remotion-assets/_shared/music/` and downloads 10 royalty-free tracks.

The brand-specific folders and `brand.json` files were created during initial deployment.
If you need to recreate them, follow the inline scripts in
`docs/superpowers/plans/2026-05-28-telegram-video-bot.md` Task A2.

### 2. Fonts

The fonts directory `/tmp/remotion-assets/_shared/fonts/` should already contain:

- `IRANSansX-Bold.woff` / `.woff2`
- `IRANSansX-Regular.woff` / `.woff2`
- `Vazirmatn-Black.woff2`
- `Vazirmatn-Bold.woff2`
- `Vazirmatn-Regular.woff2`
- `Vazirmatn-Light.woff2`

The Remotion project's `src/styles/fonts.css` references these by `/fonts/<name>` —
the renderer copies them into `public/fonts/` before each render (TODO if not automated).

### 3. Environment variables

Add to `/opt/shahrzad-devops/.env` (file mode 0600, owned by root, loaded by systemd):

```bash
REMOTION_PROJECT_DIR=/mnt/devopsstorage/repos/video
REMOTION_ASSETS_DIR=/tmp/remotion-assets
ELEVENLABS_API_KEY=sk_...
```

Restart the bot:

```bash
systemctl restart <bot-unit-name>
```

### 4. projects.json

Confirm `video` is registered:

```bash
jq '.[] | select(.name=="video")' /opt/shahrzad-devops/configs/projects.json
```

Should print `{"name": "video", "path": "/mnt/devopsstorage/repos/video"}`.

### 5. Retention cron (optional but recommended)

Delete renders older than 30 days:

```bash
echo "0 4 * * * find /mnt/devopsstorage/repos/video/renders -name '*.mp4' -mtime +30 -delete" | crontab -
```

## Adding a new brand

1. Create `/tmp/remotion-assets/<NewBrand>/{logo,products,projects}/`.
2. Drop logos, product images, project images into the matching subfolders.
3. Write `/tmp/remotion-assets/<NewBrand>/brand.json` — see existing brands for the schema.
4. No bot restart needed — `/video` re-scans `REMOTION_ASSETS_DIR` on each invocation.

## Adding a new Remotion template

1. Create `src/scenes/<NewScene>.tsx` and `src/compositions/<NewTemplate>.tsx`.
2. Register it in `src/Root.tsx` with both a `_Vertical` and `_Horizontal` variant.
3. Add the (template, aspect) tuple to `_COMPOSITION_MAP` in `video_module/props.py`.
4. Add a template button in `video_module/wizard.py` `on_brand` callback.
```

- [ ] **Step 3: Update `CHANGELOG.md`** — prepend under "## Unreleased":

```markdown
- **`/video`** command (and `/assets`, `/upload_music`, `/renders`): Telegram-driven Remotion render pipeline producing brand promo videos with optional ElevenLabs Persian narration. Two pre-built templates (ProductPromo, ServicePromo) × two aspect ratios (9:16, 16:9). Per-brand assets under `/tmp/remotion-assets/<Brand>/`. See `docs/VIDEO_BOT.md` and `docs/VIDEO_BOT_SETUP.md`.
```

- [ ] **Step 4: Update `/mnt/devopsstorage/repos/video/README.md`**

Replace the entire file with:

```markdown
# Video — Remotion render pipeline

Companion Remotion project for the Telegram bot's `/video` command.

## Compositions

| ID                          | Aspect | Resolution  |
|-----------------------------|--------|-------------|
| `ProductPromo_Vertical`     | 9:16   | 1080 × 1920 |
| `ProductPromo_Horizontal`   | 16:9   | 1920 × 1080 |
| `ServicePromo_Vertical`     | 9:16   | 1080 × 1920 |
| `ServicePromo_Horizontal`   | 16:9   | 1920 × 1080 |

## Input props contract

See `src/lib/theme.ts` → `PromoProps`. The bot writes a JSON file matching that shape
to `.video-props.json` before each render and invokes:

```bash
npx remotion render src/index.ts <CompositionId> <output.mp4> --props=.video-props.json
```

## Manual smoke test

```bash
bash scripts/render-samples.sh
```

Renders all four compositions with the default props from `Root.tsx`.
Output: `renders/sample-*.mp4`.

## Adding a new template

1. Create `src/scenes/<Scene>.tsx` for each new scene component.
2. Create `src/compositions/<Template>.tsx` composing those scenes.
3. Register `_Vertical` + `_Horizontal` entries in `src/Root.tsx`.
4. Wire it into the bot side: see `docs/VIDEO_BOT_SETUP.md` "Adding a new Remotion template" in the bot repo.
```

- [ ] **Step 5: Commit docs in both repos**

```bash
# Bot repo
git add docs/VIDEO_BOT.md docs/VIDEO_BOT_SETUP.md CHANGELOG.md
git commit -m "docs(video): user guide, DevOps setup, changelog"

# Remotion repo
cd /mnt/devopsstorage/repos/video
git add README.md
git commit -m "docs(video): README — composition list, props contract, smoke test"
```

### Task D4: Open the PR

**Files:** none.

- [ ] **Step 1: Push the branch**

```bash
cd /tmp/claude-sessions/106021080_videobot
git push -u origin claude/video-bot-design
```

- [ ] **Step 2: Open a PR (after operator confirms the smoke test passed)**

```bash
gh pr create --title "feat: /video command — Remotion promo video pipeline" --body "$(cat <<'EOF'
## Summary
- Adds `/video` (+ `/assets`, `/upload_music`, `/renders`) commands.
- Two Quick templates × two aspect ratios = four Remotion compositions.
- Optional ElevenLabs Persian narration; brand-aware music auto-pick.
- Per-brand `brand.json` under `/tmp/remotion-assets/<Brand>/`.

## Test plan
- [ ] `pytest tests/test_video_*.py` — all green
- [ ] `bash /mnt/devopsstorage/repos/video/scripts/render-samples.sh` — 4 sample mp4s produced
- [ ] Telegram E2E walkthrough from `docs/VIDEO_BOT.md` Quick Start, AlumGlass × ProductPromo × 9:16 × no music × no narration
- [ ] Render delivered as Telegram video attachment (size ≤ 50 MB)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Reply to operator with the PR URL**

---

## Self-Review

**Spec coverage:**
- ✅ Hybrid Quick + Custom paths (A2, B5, C7)
- ✅ `/tmp/remotion-assets/<Brand>/` layout (A1)
- ✅ `brand.json` schema with seeded values (A2)
- ✅ `_shared/fonts/` (pre-existing) + `_shared/music/` (A3)
- ✅ 4 compositions × 2 templates × 2 aspects (B5–B6)
- ✅ Six scene components (B4)
- ✅ ElevenLabs Persian narration (C4)
- ✅ ConversationHandler wizard (C6)
- ✅ Render subprocess + asset staging (C5)
- ✅ Atomic job state (C2)
- ✅ Bot integration with minimal `bot.py` diff (D1)
- ✅ Telegram size-aware delivery (C7 `on_confirm`)
- ✅ Three docs files (D3)
- ✅ Env-var documentation (A4)
- ✅ `projects.json` registration (A4)
- ✅ Sample renders + manual E2E (B7, D2)

**Placeholder scan:** No "TBD"/"TODO"/"implement later" anywhere. All steps contain executable content.

**Type consistency:**
- `PromoProps` defined in `src/lib/theme.ts` (B2), used unchanged in B3, B4, B5, B6.
- `composition_id` signature `(template, aspect) → str` consistent across `props.py` (C3) and consumers (C7).
- `BrandJson` schema matches between the seeded `brand.json` files (A2) and the TS type (B2) and the Python tests (C1).
- `_AUTO_MUSIC` mapping in `wizard.py` (C6) matches spec table.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-28-telegram-video-bot.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks. Best for this plan because there are ~16 distinct tasks across two repos and Python + TypeScript surfaces; the per-task context reset keeps each subagent focused.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
