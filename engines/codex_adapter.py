"""Codex CLI engine adapter (Phase 2).

Owns the Codex catalog/validation/capabilities and the *pure* command
construction + JSONL parsing. Execution orchestration (worktree, heavy permit,
tree-kill) is shared with Claude in ``bot.run_codex``.

Verified against codex-cli 0.144.4 (Phase A recon):

- Non-interactive run:  ``codex exec --json --skip-git-repo-check
  -c approval_policy="never" -c sandbox_mode="<mode>" -m <slug> [-i img] <prompt>``
- ``--json`` streams JSONL events. First turn emits
  ``{"type":"thread.started","thread_id":"<uuid>"}`` — the id to persist.
  The answer is ``{"type":"item.completed","item":{"type":"agent_message",
  "text":...}}``; ``turn.completed`` carries token usage.
- Resume:  ``codex exec resume <thread_id> --json --skip-git-repo-check
  -c ... -m <slug> [-i img] <prompt>`` (resume does not accept ``-s``/``-C``;
  cwd comes from the subprocess, sandbox from ``-c sandbox_mode``).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .base import Capabilities, EngineAdapter, ModelInfo, ENGINE_CODEX

# Verified account-visible slugs (`codex debug models`, visibility=list).
_DEFAULT_CATALOG = [
    ModelInfo("gpt-5.6-sol", "Sol"),
    ModelInfo("gpt-5.6-terra", "Terra"),
    ModelInfo("gpt-5.6-luna", "Luna"),
    ModelInfo("gpt-5.5", "GPT-5.5"),
    ModelInfo("gpt-5.4", "GPT-5.4"),
    ModelInfo("gpt-5.4-mini", "GPT-5.4 Mini"),
]
_DEFAULT_MODEL = "gpt-5.6-sol"


def _parse_env_catalog() -> list[ModelInfo] | None:
    raw = os.environ.get("BOT_CODEX_MODELS", "").strip()
    if not raw:
        return None
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        mid, _, label = part.partition(":")
        out.append(ModelInfo(mid.strip(), (label or mid).strip()))
    return out or None


def parse_codex_models(stdout: str) -> list[ModelInfo]:
    """Parse `codex debug models` JSON into visible, api-supported models."""
    try:
        doc = json.loads(stdout)
    except Exception:
        return []
    out = []
    for m in doc.get("models", []):
        if m.get("visibility") != "list" or not m.get("supported_in_api", True):
            continue
        out.append(ModelInfo(m.get("slug", ""), m.get("display_name") or m.get("slug", "")))
    return [m for m in out if m.id]


def _sandbox_mode() -> str:
    """Map the shared BOT_TOOL_PERMISSION_MODE onto a Codex sandbox policy.

    'skip' == full access (parity with Claude's --dangerously-skip-permissions);
    anything else keeps the workspace-write sandbox. Override with
    BOT_CODEX_SANDBOX (read-only|workspace-write|danger-full-access).
    """
    override = os.environ.get("BOT_CODEX_SANDBOX", "").strip()
    if override:
        return override
    if os.environ.get("BOT_TOOL_PERMISSION_MODE", "skip").strip() == "skip":
        return "danger-full-access"
    return "workspace-write"


def build_codex_cmd(prompt: str, model: str, thread_id: str | None,
                    images: list[str] | None = None) -> list[str]:
    """Build the codex argv. First turn if ``thread_id`` is falsy, else resume.

    cwd is set by the spawner (the isolated worktree); sandbox/approval come via
    ``-c`` so the exact same set works for first-turn and resume.
    """
    mode = _sandbox_mode()
    base = ["codex", "exec"]
    if thread_id:
        base += ["resume", thread_id]
    base += [
        "--json", "--skip-git-repo-check",
        "-c", 'approval_policy="never"',
        "-c", f'sandbox_mode="{mode}"',
    ]
    if model:
        base += ["-m", model]
    for img in (images or []):
        base += ["-i", img]
    base.append(prompt)
    return base


@dataclass
class CodexResult:
    thread_id: str | None = None
    text: str = ""
    diagnostics: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    saw_agent_message: bool = False
    saw_turn_completed: bool = False

    @property
    def malformed(self) -> bool:
        return not self.saw_agent_message and not self.saw_turn_completed


def parse_codex_output(stdout: str) -> CodexResult:
    """Parse Codex JSONL stdout. Never raises; ignores non-JSON/garbage lines.

    Accumulates every ``agent_message`` (Codex may emit more than one) and keeps
    the thread id and usage. ``error`` items are collected as diagnostics —
    some (e.g. the skills-budget notice) are non-fatal, so we only surface them
    when there is no agent message.
    """
    res = CodexResult()
    texts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        t = evt.get("type")
        if t == "thread.started":
            res.thread_id = evt.get("thread_id") or res.thread_id
        elif t == "item.completed":
            item = evt.get("item") or {}
            it = item.get("type")
            if it == "agent_message":
                txt = (item.get("text") or "").strip()
                if txt:
                    texts.append(txt)
                    res.saw_agent_message = True
            elif it == "error":
                msg = (item.get("message") or "").strip()
                if msg:
                    res.diagnostics.append(msg)
        elif t == "turn.completed":
            res.saw_turn_completed = True
            usage = evt.get("usage") or {}
            res.input_tokens = int(usage.get("input_tokens") or 0)
            res.output_tokens = int(usage.get("output_tokens") or 0)
        elif t == "turn.failed" or t == "error":
            msg = ""
            if isinstance(evt.get("error"), dict):
                msg = evt["error"].get("message", "")
            res.diagnostics.append(msg or json.dumps(evt)[:300])
    res.text = "\n\n".join(texts).strip()
    return res


# Failure classification keywords (checked on combined stdout+stderr, lowercased)
_RATE_KW = ("rate limit", "rate_limit", "quota", "overloaded", "429",
            "too many requests", "usage limit")
_AUTH_KW = ("not logged in", "unauthorized", "401", "authentication",
            "please run codex login", "login required", "invalid api key")


def classify_codex_failure(rc: int | None, stdout: str, stderr: str) -> str:
    """Return 'ok' | 'rate_limit' | 'auth' | 'nonzero'. ('timeout'/'malformed'
    are decided by the caller / parser respectively.)"""
    combined = f"{stdout}\n{stderr}".lower()
    if any(k in combined for k in _AUTH_KW):
        return "auth"
    if any(k in combined for k in _RATE_KW):
        return "rate_limit"
    if rc not in (0, None):
        return "nonzero"
    return "ok"


class CodexAdapter(EngineAdapter):
    caps = Capabilities(
        engine=ENGINE_CODEX,
        label="Codex",
        uses_worktree=True,
        uses_repo_branch=True,
        supports_deploy=True,
        supports_attachments=True,
        counts_as_heavy=True,
        is_chat=False,
    )

    def __init__(self):
        # last-known-good discovered catalog (populated by refresh); None => static
        self._discovered: list[ModelInfo] | None = None

    def default_model(self) -> str:
        return os.environ.get("BOT_CODEX_DEFAULT_MODEL", "").strip() or _DEFAULT_MODEL

    def list_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        env = _parse_env_catalog()
        if env:
            return env
        if self._discovered:
            return list(self._discovered)
        return list(_DEFAULT_CATALOG)

    def set_discovered(self, models: list[ModelInfo]) -> None:
        """Store a runtime-discovered catalog (from `codex debug models`)."""
        if models:
            self._discovered = models

    def validate_model(self, model_id: str) -> bool:
        if not model_id:
            return True
        # Reject cross-engine ids: openrouter ('vendor/model') and claude ids.
        if "/" in model_id or model_id.lower().startswith("claude"):
            return False
        return any(m.id == model_id for m in self.list_models())
