"""OpenRouter Chat engine (Phase 2).

A pure conversational engine: **no tools, shell, worktree, repo or deploy
actions**. Uses an async ``aiohttp`` client with bounded timeouts, limited
429/5xx retry honouring ``Retry-After``, and API-key redaction from all logs
and error strings. Conversation history is bounded, atomic and stored per
chat-session *outside* ``bot-state.json``.

Catalog is fetched from ``GET /api/v1/models`` with a TTL cache plus a
persistent last-known-good file. Favorites are ten owner-configured ids
(NOT an objective popularity ranking), resolved against the live catalog;
missing ids are marked unavailable, never requested.

Verified favorite ids (Phase A recon) — must include representatives of
GLM 5.2 / MiniMax M3 / Qwen plus seven strong current alternatives.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass

from .base import Capabilities, EngineAdapter, ModelInfo, ENGINE_CHAT

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Ten owner-configured favorites (verified live). Override via BOT_CHAT_FAVORITES.
_DEFAULT_FAVORITES = [
    "z-ai/glm-5.2",                  # GLM 5.2  (required)
    "minimax/minimax-m3",            # MiniMax M3 (required)
    "qwen/qwen3.7-max",              # Qwen     (required)
    "anthropic/claude-opus-4.8",
    "anthropic/claude-sonnet-5",
    "openai/gpt-5.6-luna",
    "google/gemini-3.1-pro-preview",
    "x-ai/grok-4.5",
    "deepseek/deepseek-v4-pro",
    "meta-llama/llama-4-maverick",
]

CHAT_SYSTEM_PROMPT = (
    "You are a helpful, honest general-purpose assistant reached over Telegram. "
    "You have no access to tools, shell, files, repositories or deployment — "
    "answer from your own knowledge and reasoning. If a request needs code to be "
    "run, files changed or something deployed, say plainly that this chat cannot "
    "do that and suggest using a Claude Code or Codex session instead. Be concise."
)


def _favorites_ids() -> list[str]:
    raw = os.environ.get("BOT_CHAT_FAVORITES", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return list(_DEFAULT_FAVORITES)


def _redact(text: str, key: str | None) -> str:
    if not text:
        return text
    if key:
        text = text.replace(key, "***")
    # Also scrub any Bearer token that might slip into an error string.
    return re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", text)


class ChatError(Exception):
    """Normalized chat failure. ``kind`` in {rate_limit, auth, timeout,
    http, network, empty}."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _catalog_cache_path() -> str:
    d = os.environ.get("BOT_CONFIGS_DIR") or os.environ.get("BOT_DATA_ROOT") or "/tmp"
    return os.path.join(d, "openrouter-catalog.json")


def _model_from_raw(m: dict) -> ModelInfo:
    pricing = None
    pr = m.get("pricing") or {}
    try:
        comp = float(pr.get("completion") or 0)
        if comp > 0:
            pricing = f"${comp * 1_000_000:.2f}/M out"
    except (TypeError, ValueError):
        pricing = None
    return ModelInfo(
        id=m.get("id", ""),
        label=m.get("name") or m.get("id", ""),
        context=m.get("context_length"),
        pricing=pricing,
    )


class OpenRouterClient:
    """Thin async client. ``session_factory`` is injectable for offline tests."""

    def __init__(self, api_key: str | None, referer: str = "", title: str = "",
                 session_factory=None):
        self.api_key = api_key
        self.referer = referer
        self.title = title
        self._session_factory = session_factory  # () -> aiohttp.ClientSession

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.referer:
            h["HTTP-Referer"] = self.referer
        if self.title:
            h["X-Title"] = self.title
        return h

    def _new_session(self, timeout: float):
        if self._session_factory is not None:
            return self._session_factory()
        import aiohttp
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

    async def fetch_models(self, timeout: float = 20.0) -> list[dict]:
        session = self._new_session(timeout)
        try:
            async with session.get(OPENROUTER_MODELS_URL, headers=self._headers()) as resp:
                if resp.status != 200:
                    raise ChatError("http", f"models HTTP {resp.status}")
                doc = await resp.json()
            return doc.get("data", []) or []
        except asyncio.CancelledError:
            raise
        except ChatError:
            raise
        except Exception as e:
            raise ChatError("network", _redact(str(e), self.api_key))
        finally:
            await session.close()

    async def chat(self, model: str, messages: list[dict], *, timeout: float = 120.0,
                   max_retries: int = 3) -> str:
        """One completion. Retries 429/5xx honouring Retry-After. Cancellation
        closes the session and re-raises. All errors are redacted."""
        if not self.api_key:
            raise ChatError("auth", "OPENROUTER_API_KEY is not set")
        payload = {"model": model, "messages": messages}
        attempt = 0
        while True:
            attempt += 1
            session = self._new_session(timeout)
            try:
                async with session.post(OPENROUTER_CHAT_URL, headers=self._headers(),
                                        json=payload) as resp:
                    if resp.status == 200:
                        doc = await resp.json()
                        try:
                            txt = doc["choices"][0]["message"]["content"]
                        except (KeyError, IndexError, TypeError):
                            raise ChatError("empty", "malformed completion response")
                        if not txt:
                            raise ChatError("empty", "empty completion")
                        return txt
                    if resp.status in (401, 403):
                        raise ChatError("auth", f"HTTP {resp.status}")
                    if resp.status == 429 or 500 <= resp.status < 600:
                        if attempt > max_retries:
                            kind = "rate_limit" if resp.status == 429 else "http"
                            raise ChatError(kind, f"HTTP {resp.status} after {attempt-1} retries")
                        delay = _retry_after(resp.headers) or min(2 ** attempt, 30)
                        await asyncio.sleep(delay)
                        continue
                    body = _redact((await resp.text())[:300], self.api_key)
                    raise ChatError("http", f"HTTP {resp.status}: {body}")
            except asyncio.CancelledError:
                raise
            except ChatError:
                raise
            except asyncio.TimeoutError:
                if attempt > max_retries:
                    raise ChatError("timeout", f"timeout after {attempt-1} retries")
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            except Exception as e:
                raise ChatError("network", _redact(str(e), self.api_key))
            finally:
                await session.close()


def _retry_after(headers) -> float | None:
    try:
        v = headers.get("Retry-After")
        if v is None:
            return None
        return max(0.0, min(float(v), 60.0))
    except (TypeError, ValueError):
        return None


class ChatCatalog:
    """TTL cache + persistent last-known-good over OpenRouter's model list."""

    def __init__(self, client: OpenRouterClient, ttl: float = 3600.0,
                 min_refresh_interval: float = 30.0, cache_path: str | None = None):
        self.client = client
        self.ttl = ttl
        self.min_refresh_interval = min_refresh_interval
        self.cache_path = cache_path or _catalog_cache_path()
        self._models: list[ModelInfo] = []
        self._fetched_at = 0.0
        self._last_refresh_attempt = 0.0
        self.stale = False

    def _load_persistent(self) -> bool:
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                doc = json.load(f)
            self._models = [_model_from_raw(m) for m in doc.get("data", [])]
            self._fetched_at = doc.get("fetched_at", 0.0)
            return bool(self._models)
        except Exception:
            return False

    def _save_persistent(self, raw: list[dict]) -> None:
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": time.time(), "data": raw}, f)
            os.replace(tmp, self.cache_path)
        except Exception:
            pass

    async def get(self, *, refresh: bool = False, now: float | None = None) -> list[ModelInfo]:
        now = time.time() if now is None else now
        fresh = self._models and (now - self._fetched_at) < self.ttl
        if fresh and not refresh:
            self.stale = False
            return list(self._models)
        # Rate-limit manual refresh.
        if refresh and (now - self._last_refresh_attempt) < self.min_refresh_interval and self._models:
            return list(self._models)
        self._last_refresh_attempt = now
        try:
            raw = await self.client.fetch_models()
            self._models = [_model_from_raw(m) for m in raw if m.get("id")]
            self._fetched_at = now
            self.stale = False
            self._save_persistent(raw)
        except Exception:
            # Discovery failed: fall back to in-memory, then persistent cache.
            if not self._models:
                self._load_persistent()
            self.stale = True
        return list(self._models)

    def valid_id(self, model_id: str) -> bool:
        return any(m.id == model_id for m in self._models)

    async def favorites(self) -> list[ModelInfo]:
        catalog = {m.id: m for m in await self.get()}
        out = []
        for fid in _favorites_ids():
            m = catalog.get(fid)
            if m:
                out.append(m)
            else:
                out.append(ModelInfo(fid, fid.split("/")[-1], available=False))
        return out

    async def search(self, query: str, limit: int = 20) -> list[ModelInfo]:
        q = query.strip().lower()
        if not q:
            return []
        cat = await self.get()
        return [m for m in cat if q in m.id.lower() or q in m.label.lower()][:limit]


# ── Bounded, atomic, per-session chat history ──────────────────────────────

def _history_max_turns() -> int:
    try:
        return max(2, int(os.environ.get("BOT_CHAT_HISTORY_MAX_TURNS", "20")))
    except ValueError:
        return 20


def _history_max_chars() -> int:
    try:
        return max(2000, int(os.environ.get("BOT_CHAT_HISTORY_MAX_CHARS", "40000")))
    except ValueError:
        return 40000


class ChatHistory:
    """Per-session message log at ``<chat_dir>/history.json``. Bounded + atomic.

    Isolated by session (each session has its own directory). Removed only on
    explicit session kill (``purge``).
    """

    def __init__(self, chat_dir: str):
        self.chat_dir = chat_dir
        self.path = os.path.join(chat_dir, "history.json")

    def load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _bound(self, msgs: list[dict]) -> list[dict]:
        msgs = msgs[-_history_max_turns() * 2:]  # a turn == user+assistant
        total = 0
        kept: list[dict] = []
        for m in reversed(msgs):
            total += len(m.get("content", ""))
            if total > _history_max_chars() and kept:
                break
            kept.append(m)
        kept.reverse()
        return kept

    def append(self, user: str, assistant: str) -> None:
        msgs = self.load()
        msgs.append({"role": "user", "content": user})
        msgs.append({"role": "assistant", "content": assistant})
        msgs = self._bound(msgs)
        os.makedirs(self.chat_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def build_messages(self, new_user: str) -> list[dict]:
        return ([{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
                + self.load()
                + [{"role": "user", "content": new_user}])

    def purge(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except Exception:
            pass


class ChatAdapter(EngineAdapter):
    caps = Capabilities(
        engine=ENGINE_CHAT,
        label="Chat",
        uses_worktree=False,
        uses_repo_branch=False,
        supports_deploy=False,
        supports_attachments=False,
        counts_as_heavy=False,
        is_chat=True,
    )

    def __init__(self, catalog: ChatCatalog):
        self.catalog = catalog

    def default_model(self) -> str:
        favs = _favorites_ids()
        return favs[0] if favs else ""

    def list_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        # Sync view over whatever the catalog last loaded (favorites first).
        favs = {f for f in _favorites_ids()}
        cached = {m.id: m for m in self.catalog._models}
        out = []
        for fid in _favorites_ids():
            out.append(cached.get(fid) or ModelInfo(fid, fid.split("/")[-1],
                                                     available=fid in cached or not self.catalog._models))
        return out

    def validate_model(self, model_id: str) -> bool:
        if not model_id:
            return True
        # Chat ids are OpenRouter 'vendor/model'. Reject bare code-engine ids.
        if "/" not in model_id:
            return False
        # If we have a catalog, require membership; otherwise accept (offline).
        if self.catalog._models:
            return self.catalog.valid_id(model_id) or model_id in _favorites_ids()
        return True
