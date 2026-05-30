# /video — DevOps Setup

## Prerequisites

- Node >= 20 (for `npx remotion render`)
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

This downloads 10 royalty-free music tracks into `/tmp/remotion-assets/_shared/music/`.

The brand-specific folders and `brand.json` files were created during initial deployment.
If you need to recreate them, see the design spec at
`docs/superpowers/specs/2026-05-28-telegram-video-bot-design.md`.

### 2. Fonts

The fonts directory `/tmp/remotion-assets/_shared/fonts/` should already contain:

- `IRANSansX-Bold.woff` / `.woff2`
- `IRANSansX-Regular.woff` / `.woff2`
- `Vazirmatn-Black.woff2`
- `Vazirmatn-Bold.woff2`
- `Vazirmatn-Regular.woff2`
- `Vazirmatn-Light.woff2`

The Remotion repo has a copy in `public/fonts/` (committed to git). If you add a new font face,
copy it to both locations.

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

Should print `{"name": "video", "path": "..."}`.

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

1. Create `src/scenes/<NewScene>.tsx` and `src/compositions/<NewTemplate>.tsx` in `/mnt/devopsstorage/repos/video/`.
2. Register it in `src/Root.tsx` with both a `-Vertical` and `-Horizontal` variant (use hyphen, not underscore).
3. Add the (template, aspect) tuple to `_COMPOSITION_MAP` in `video_module/props.py`.
4. Add a template button in `video_module/wizard.py` `on_brand` callback.
