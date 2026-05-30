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
