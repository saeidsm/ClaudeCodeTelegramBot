"""Engine abstraction and registry (Phase 2).

A typed adapter boundary with exactly three engines: ``claude``, ``codex``,
``chat``. Adapters own their model *catalog*, model *validation*, *capabilities*
and (for the code engines) *command construction* / *output parsing*.

Execution orchestration — the per-session FIFO lock, the global heavy permit,
worktree lifecycle and process-tree cancellation — stays in ``bot.py`` and is
shared by all code engines; ``execute()`` dispatches to the right run-path by
``session.engine`` through this registry, so handlers never branch on engine
name themselves.

This module deliberately imports nothing from ``bot`` to stay unit-testable in
isolation (no Telegram / no event loop needed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ENGINE_CLAUDE = "claude"
ENGINE_CODEX = "codex"
ENGINE_CHAT = "chat"
VALID_ENGINES = (ENGINE_CLAUDE, ENGINE_CODEX, ENGINE_CHAT)


@dataclass(frozen=True)
class Capabilities:
    """Static, per-engine behaviour flags the orchestrator reads.

    Keeping these as data (not ``isinstance`` checks scattered in handlers) is
    what lets ``/new``, ``/model`` and ``execute`` stay engine-agnostic.
    """
    engine: str
    label: str                    # user-facing engine name
    uses_worktree: bool           # runs inside an isolated git worktree
    uses_repo_branch: bool        # /new asks for repo/project (+ branch)
    supports_deploy: bool         # deploy controls may be shown
    supports_attachments: bool    # file uploads are meaningful
    counts_as_heavy: bool         # consumes a MAX_RUNNING_AGENTS permit
    is_chat: bool                 # pure conversational engine (no tools)


@dataclass
class ModelInfo:
    """One selectable model. ``pricing`` is a short human string, never a key."""
    id: str
    label: str
    context: Optional[int] = None
    pricing: Optional[str] = None
    available: bool = True

    def display(self) -> str:
        bits = [self.label]
        if self.context:
            bits.append(f"{self.context // 1000}k")
        if self.pricing:
            bits.append(self.pricing)
        s = " · ".join(bits)
        if not self.available:
            s += " (unavailable)"
        return s


class EngineAdapter:
    """Base adapter. Subclasses set ``caps`` and implement the catalog API.

    ``list_models`` returns the *current* catalog (possibly cached);
    ``validate_model`` checks an id against it (adapters may accept extra
    free-form ids — e.g. Claude aliases — by overriding). ``default_model``
    returns the id used when a session has none.
    """
    caps: Capabilities

    def default_model(self) -> str:  # pragma: no cover - trivial
        return ""

    def list_models(self, *, refresh: bool = False) -> list[ModelInfo]:  # pragma: no cover
        raise NotImplementedError

    def validate_model(self, model_id: str) -> bool:
        if not model_id:
            return True  # empty == engine/CLI default
        return any(m.id == model_id for m in self.list_models())

    def resolve_label(self, model_id: str) -> str:
        if not model_id:
            return "default"
        for m in self.list_models():
            if m.id == model_id:
                return m.label
        return model_id


_REGISTRY: dict[str, EngineAdapter] = {}


def register(adapter: EngineAdapter) -> EngineAdapter:
    if adapter.caps.engine not in VALID_ENGINES:
        raise ValueError(f"unknown engine {adapter.caps.engine!r}")
    _REGISTRY[adapter.caps.engine] = adapter
    return adapter


def get_adapter(engine: str) -> Optional[EngineAdapter]:
    return _REGISTRY.get(engine)


def known_engine(engine: str) -> bool:
    return engine in _REGISTRY


def all_engines() -> list[EngineAdapter]:
    """Registered adapters in canonical order."""
    return [_REGISTRY[e] for e in VALID_ENGINES if e in _REGISTRY]


def reset_registry() -> None:
    """Test helper — clear registered adapters."""
    _REGISTRY.clear()
