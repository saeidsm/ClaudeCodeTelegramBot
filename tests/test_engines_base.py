"""Engine registry + capabilities + cross-engine model validation (§B/§D)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engines.base as eb
from engines.claude_adapter import ClaudeAdapter
from engines.codex_adapter import CodexAdapter
from engines.openrouter_chat import ChatAdapter, ChatCatalog, OpenRouterClient


def _fresh_registry():
    eb.reset_registry()
    eb.register(ClaudeAdapter())
    eb.register(CodexAdapter())
    cat = ChatCatalog(OpenRouterClient(None), cache_path="/tmp/nope-catalog.json")
    eb.register(ChatAdapter(cat))
    return cat


def test_exactly_three_engines_in_order():
    _fresh_registry()
    assert [a.caps.engine for a in eb.all_engines()] == ["claude", "codex", "chat"]


def test_capabilities_shape():
    _fresh_registry()
    claude = eb.get_adapter("claude")
    chat = eb.get_adapter("chat")
    assert claude.caps.uses_worktree and claude.caps.counts_as_heavy and not claude.caps.is_chat
    assert chat.caps.is_chat and not chat.caps.uses_worktree and not chat.caps.counts_as_heavy
    assert not chat.caps.supports_deploy and not chat.caps.uses_repo_branch


def test_unknown_engine_is_none():
    _fresh_registry()
    assert eb.get_adapter("nope") is None
    assert not eb.known_engine("nope")


def test_claude_validation_accepts_aliases_and_rejects_cross_engine():
    a = ClaudeAdapter()
    assert a.validate_model("")            # default
    assert a.validate_model("opus")        # alias
    assert a.validate_model("claude-opus-4-8")
    assert a.validate_model("claude-sonnet-4-6[1m]")
    assert not a.validate_model("gpt-5.6-sol")     # codex slug
    assert not a.validate_model("z-ai/glm-5.2")    # openrouter id


def test_codex_validation_rejects_cross_engine():
    a = CodexAdapter()
    assert a.validate_model("gpt-5.6-sol")
    assert not a.validate_model("claude-opus-4-8")
    assert not a.validate_model("z-ai/glm-5.2")
    assert not a.validate_model("made-up-slug")


def test_chat_validation_requires_vendor_slash():
    cat = _fresh_registry()
    a = eb.get_adapter("chat")
    # No catalog loaded yet -> accept plausible ids, reject bare code ids.
    assert a.validate_model("z-ai/glm-5.2")
    assert not a.validate_model("gpt-5.6-sol")
    assert not a.validate_model("opus")


def test_codex_default_and_labels():
    a = CodexAdapter()
    assert a.default_model() == "gpt-5.6-sol"
    labels = {m.id: m.label for m in a.list_models()}
    assert labels["gpt-5.6-sol"] == "Sol"
    assert labels["gpt-5.6-terra"] == "Terra"
    assert labels["gpt-5.6-luna"] == "Luna"


def test_env_catalog_override(monkeypatch):
    monkeypatch.setenv("BOT_CODEX_MODELS", "x-model:XM,y-model:YM")
    a = CodexAdapter()
    ids = [m.id for m in a.list_models()]
    assert ids == ["x-model", "y-model"]
    assert a.validate_model("x-model")
    assert not a.validate_model("gpt-5.6-sol")
