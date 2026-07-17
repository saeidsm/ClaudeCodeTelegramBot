"""Claude Code engine adapter (Phase 2).

Owns only the Claude *catalog*, *validation* and *capabilities*. The actual
execution (``run_claude`` — UUID/resume, worktree, permissions, rate-limit
flow) stays in ``bot.py`` unchanged, so there is zero regression risk to the
Phase 1 Claude path. This adapter is what ``/model`` and ``/new`` read to render
Claude choices and to reject cross-engine ids.
"""
from __future__ import annotations

import os

from .base import Capabilities, EngineAdapter, ModelInfo, ENGINE_CLAUDE

# Account-visible catalog verified via `claude models` (Phase A recon).
# Env-overridable if the account catalog changes, so we never hardcode a
# marketing label as an unverified id long-term.
_DEFAULT_CATALOG = [
    ModelInfo("", "Default (CLI default)"),
    ModelInfo("claude-fable-5", "Fable 5"),
    ModelInfo("claude-opus-4-8", "Opus 4.8"),
    ModelInfo("claude-sonnet-5", "Sonnet 5"),
    ModelInfo("claude-haiku-4-5-20251001", "Haiku 4.5"),
]

# `--model` also accepts short aliases; keep them selectable/validatable.
_ALIASES = {"fable", "opus", "sonnet", "haiku"}


def _parse_env_catalog() -> list[ModelInfo] | None:
    """BOT_CLAUDE_MODELS = 'id:Label,id2:Label2' overrides the default catalog."""
    raw = os.environ.get("BOT_CLAUDE_MODELS", "").strip()
    if not raw:
        return None
    out = [ModelInfo("", "Default (CLI default)")]
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        mid, _, label = part.partition(":")
        out.append(ModelInfo(mid.strip(), (label or mid).strip()))
    return out


class ClaudeAdapter(EngineAdapter):
    caps = Capabilities(
        engine=ENGINE_CLAUDE,
        label="Claude Code",
        uses_worktree=True,
        uses_repo_branch=True,
        supports_deploy=True,
        supports_attachments=True,
        counts_as_heavy=True,
        is_chat=False,
    )

    def default_model(self) -> str:
        # Empty => inherit BOT_DEFAULT_CLAUDE_MODEL (handled in bot build_cmd).
        return ""

    def list_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        return _parse_env_catalog() or list(_DEFAULT_CATALOG)

    def validate_model(self, model_id: str) -> bool:
        if not model_id:
            return True
        mid = model_id.strip()
        if mid in _ALIASES:
            return True
        if any(m.id == mid for m in self.list_models()):
            return True
        # Accept any plausible Claude id (custom / Bedrock / 1M variants) but
        # reject cross-engine ids (codex slugs, openrouter 'vendor/model').
        low = mid.lower()
        if "/" in mid:
            return False
        return low.startswith("claude") or low.startswith("us.anthropic") or "[1m]" in low

    def resolve_label(self, model_id: str) -> str:
        if not model_id:
            return "default"
        for m in self.list_models():
            if m.id == model_id:
                return m.label
        return model_id
