"""Round-2 §2 — the PTB compatibility claim is reproducible from requirements,
and directly-relied-on runtime deps are pinned (not host drift)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REQ = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()


def _spec(pkg: str) -> str:
    for line in _REQ.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.lower().startswith(pkg.lower()):
            return line
    return ""


def test_ptb_has_tested_upper_bound():
    spec = _spec("python-telegram-bot")
    assert spec, "python-telegram-bot not pinned"
    # must bound the upper edge (the range FooterBot is validated against)
    assert re.search(r"<\s*23", spec), f"no tested upper bound: {spec!r}"
    assert re.search(r">=\s*21", spec), f"lower bound missing: {spec!r}"


def test_installed_ptb_within_declared_range():
    import telegram
    major = int(telegram.__version__.split(".")[0])
    assert 21 <= major < 23


def test_python_dotenv_pinned_and_importable():
    assert _spec("python-dotenv"), "python-dotenv missing from requirements"
    from dotenv import dotenv_values  # the exact API the deploy gates use
    assert callable(dotenv_values)


def test_other_direct_deps_present():
    for pkg in ("aiohttp", "openai", "google-genai", "httpx"):
        assert _spec(pkg), f"{pkg} missing from requirements"


def test_required_openrouter_favorites_present():
    # Round-2 §4: the required default chat model IDs must remain in the code
    # defaults. Offline contract guard (no OpenRouter call) — fails closed if the
    # defaults drift so a live-catalog divergence is caught at review, not runtime.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from engines.openrouter_chat import _DEFAULT_FAVORITES
    # Family-level, not version-pinned: a catalog refresh may bump GLM 5.2 → 5.3,
    # but silently dropping one of the three required families still fails closed.
    for family in ("z-ai/glm", "minimax/minimax", "qwen/qwen"):
        assert any(f.startswith(family) for f in _DEFAULT_FAVORITES), \
            f"required default favorite missing: {family}"
