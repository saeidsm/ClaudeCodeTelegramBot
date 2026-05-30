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
    assert props.composition_id("ProductPromo", "9:16")  == "ProductPromo-Vertical"
    assert props.composition_id("ProductPromo", "16:9")  == "ProductPromo-Horizontal"
    assert props.composition_id("ServicePromo", "9:16")  == "ServicePromo-Vertical"
    assert props.composition_id("ServicePromo", "16:9")  == "ServicePromo-Horizontal"


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
