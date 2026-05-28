#!/usr/bin/env bash
# seed-video-assets.sh — Download 10 royalty-free music tracks for Remotion compositions
#
# Sources: Free Music Archive (freemusicarchive.org) — CC BY / CC0 licenses
#          Pixabay CDN — Pixabay Content License (commercial use, no attribution required)
#
# Load-bearing filenames (hard-referenced by video_module/assets.py):
#   corporate-uplifting.mp3  → AlumGlass brand
#   cinematic-tech.mp3       → NanoShield brand
#   calm-storytelling.mp3    → Shahrzad brand
#
# Usage:
#   bash scripts/seed-video-assets.sh
#   Re-running is safe — existing non-empty files are skipped.

set -euo pipefail

DEST="/tmp/remotion-assets/_shared/music"
mkdir -p "$DEST"

# ---------------------------------------------------------------------------
# Track table: "dest-filename|source-url|description"
#   FMA stream URLs (freemusicarchive.org/track/HANDLE/stream/) are permanent
#   stable handles that redirect to a fresh storage token on every request.
#   curl -fsSL -L follows the redirect and writes the real MP3.
# ---------------------------------------------------------------------------
declare -a TRACKS=(
  # LOAD-BEARING — names referenced in video_module/assets.py auto-mapping
  "corporate-uplifting.mp3|https://freemusicarchive.org/track/ambient-corporate-inspiration/stream/|Ambient Corporate Inspiration (FMA / CC BY)"
  "cinematic-tech.mp3|https://freemusicarchive.org/track/electronic-beats-part-2/stream/|Electronic Beats Part 2 (FMA / CC BY)"
  "calm-storytelling.mp3|https://freemusicarchive.org/track/calm-corporate-1mp3/stream/|Calm Corporate 1 (FMA / CC BY)"

  # VARIETY — used in picker UI, mood labels are indicative
  "energetic-promo.mp3|https://freemusicarchive.org/track/corporate-motivation-8/stream/|Corporate Motivation (FMA / CC BY)"
  "scientific-explainer.mp3|https://freemusicarchive.org/track/conductive-path/stream/|Conductive Path (FMA / CC BY)"
  "minimal-ambient.mp3|https://freemusicarchive.org/track/ambient-music-1/stream/|Ambient Music (FMA / CC BY)"
  "warm-piano.mp3|https://freemusicarchive.org/track/summer-rain-medium-version-acoustic-guitar-music/stream/|Summer Rain Acoustic Guitar (FMA / CC BY)"
  "modern-electronic.mp3|https://freemusicarchive.org/track/modern-funkmp3/stream/|Modern Funk (FMA / CC BY)"
  "hopeful-acoustic.mp3|https://freemusicarchive.org/track/inspired-by-life-happy-acoustic-folk/stream/|Inspired By Life Acoustic Folk (FMA / CC BY)"
  "documentary-strings.mp3|https://freemusicarchive.org/track/corporate-product/stream/|Corporate Product (FMA / CC BY)"
)

MIN_BYTES=30720  # 30 KB minimum — anything smaller is a download error page

ok=0
skipped=0
failed=0

for entry in "${TRACKS[@]}"; do
  IFS='|' read -r filename url description <<< "$entry"
  dest_file="$DEST/$filename"

  # Idempotency: skip if file exists and is non-empty (above minimum threshold)
  if [[ -f "$dest_file" ]]; then
    actual_size=$(stat -c%s "$dest_file" 2>/dev/null || echo 0)
    if [[ "$actual_size" -ge "$MIN_BYTES" ]]; then
      echo "  SKIP  $filename ($(( actual_size / 1024 )) KB already present)"
      (( skipped++ )) || true
      continue
    else
      echo "  RETRY $filename (existing file is too small: ${actual_size}B, re-downloading)"
    fi
  fi

  echo "  GET   $filename"
  echo "        $description"

  part_file="${dest_file}.part"
  # -L follows the FMA redirect to the actual storage token URL
  if curl -fsSL --max-time 60 -L -o "$part_file" "$url"; then
    actual_size=$(stat -c%s "$part_file" 2>/dev/null || echo 0)
    if [[ "$actual_size" -ge "$MIN_BYTES" ]]; then
      mv "$part_file" "$dest_file"
      echo "        saved $(( actual_size / 1024 )) KB"
      (( ok++ )) || true
    else
      rm -f "$part_file"
      echo "  FAIL  $filename — downloaded ${actual_size}B which is below minimum (${MIN_BYTES}B)"
      (( failed++ )) || true
    fi
  else
    rm -f "$part_file"
    echo "  FAIL  $filename — curl error for $url"
    (( failed++ )) || true
  fi
done

echo ""
echo "Done: $ok downloaded, $skipped skipped, $failed failed"
echo ""
ls -lh "$DEST"

if [[ "$failed" -gt 0 ]]; then
  echo ""
  echo "WARNING: $failed track(s) failed to download. Re-run to retry." >&2
  exit 1
fi
