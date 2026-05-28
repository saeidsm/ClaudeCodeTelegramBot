"""Build PromoProps JSON from collected wizard state.

The Remotion side reads the JSON via `--props=<file>` and the paths inside
must be **relative to public/** because Remotion resolves staticFile()
against that root.
"""
from __future__ import annotations

from typing import Any


_COMPOSITION_MAP = {
    ("ProductPromo", "9:16"): "ProductPromo-Vertical",
    ("ProductPromo", "16:9"): "ProductPromo-Horizontal",
    ("ServicePromo", "9:16"): "ServicePromo-Vertical",
    ("ServicePromo", "16:9"): "ServicePromo-Horizontal",
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
