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
