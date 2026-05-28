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
