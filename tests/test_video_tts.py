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
