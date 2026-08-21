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
import secrets
import time
from dataclasses import dataclass

from .base import Capabilities, EngineAdapter, ModelInfo, ENGINE_CHAT

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Ten owner-configured favorites — newest model of each family, verified live
# against GET /api/v1/models. Override via BOT_CHAT_FAVORITES.
_DEFAULT_FAVORITES = [
    "z-ai/glm-5.3",                  # GLM      (required)
    "minimax/minimax-m3",            # MiniMax  (required)
    "qwen/qwen3.8-max",              # Qwen     (required)
    "moonshotai/kimi-k3",            # Kimi
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "openai/gpt-5.6-sol",
    "google/gemini-3.1-pro-preview",
    "x-ai/grok-4.6",
    "deepseek/deepseek-v4-pro-0813",
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
    """Resolve the OpenRouter catalog cache path.

    Precedence: explicit ``BOT_CHAT_CATALOG_CACHE`` > ``${BOT_CONFIGS_DIR}`` >
    ``${BOT_DATA_ROOT}/configs`` > ``/tmp/configs``. The default is consistently
    *under the configs dir* (matching the bot's ``BOT_CONFIGS_DIR`` default of
    ``$BOT_DATA_ROOT/configs``) so the deploy manifest backs up the exact file the
    bot actually uses — no more ``BOT_DATA_ROOT/`` vs ``configs/`` mismatch."""
    explicit = os.environ.get("BOT_CHAT_CATALOG_CACHE")
    if explicit:
        return explicit
    configs = os.environ.get("BOT_CONFIGS_DIR")
    if not configs:
        data = os.environ.get("BOT_DATA_ROOT") or "/tmp"
        configs = os.path.join(data, "configs")
    return os.path.join(configs, "openrouter-catalog.json")


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
        # Never emit 'Authorization: Bearer None'. Public catalog discovery may
        # omit auth entirely; completions fail closed earlier if no key is set.
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
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
                        txt = _extract_content(doc)
                        if not txt:
                            raise ChatError("empty", "empty or malformed completion")
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


def _extract_content(doc) -> str:
    """Safely pull assistant text from an OpenRouter/OpenAI completion.

    Handles ``content`` as a plain string or as a list of typed parts
    (``[{"type":"text","text":...}]``). Returns "" on any malformed shape so
    the caller raises a normalized empty/malformed error — never a traceback,
    never leaking the raw body."""
    try:
        msg = doc["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "".join(parts).strip()
    return ""


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
        # Unique temp name per write so concurrent refreshes can't race over one
        # fixed ".tmp" path (last atomic os.replace wins; none corrupts).
        try:
            d = os.path.dirname(self.cache_path) or "."
            os.makedirs(d, exist_ok=True)
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix=".orcat-", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"fetched_at": time.time(), "data": raw}, f)
                os.replace(tmp, self.cache_path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
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

    async def is_selectable(self, model_id: str) -> bool:
        """Fail-closed pre-request check: the id must be a real 'vendor/model'
        present in the current OR last-known-good catalog. If no catalog can be
        obtained at all, refuse — never accept an arbitrary id just because the
        catalog hasn't loaded."""
        if not model_id or "/" not in model_id:
            return False
        await self.get()  # respects TTL / last-known-good; never raises
        if not self._models:
            return False
        return self.valid_id(model_id)


# ── Chat session dir safety (opaque key + canonical containment) ────────────

def safe_session_dirname(session_id: str) -> str:
    """Deterministic, filesystem-safe directory name for a session.

    A bounded readable prefix (alnum/_/- only) plus a SHA-256 digest of the raw
    id. Never derived from raw label/path text, so labels containing
    ``/ .. : | Unicode`` or control chars cannot alter the on-disk path."""
    import hashlib
    import re
    prefix = re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:24]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}" if prefix else f"chat-{digest}"


def canonical_chat_root(root: str) -> str:
    return os.path.realpath(root)


def derive_chat_dir(root: str, session_id: str) -> str:
    """Always-safe derived directory: immediate child of the canonical root."""
    return os.path.join(canonical_chat_root(root), safe_session_dirname(session_id))


def validate_chat_dir(root: str, path: str) -> bool:
    """A history dir is acceptable only if it is an immediate child of the
    canonical chat root, is not the root itself, and is not (nor reached via) a
    symlink. Does not require the dir to exist yet."""
    if not path:
        return False
    try:
        rroot = canonical_chat_root(root)
        ap = os.path.abspath(path.rstrip("/"))
        base = os.path.basename(ap)
        if not base or base in (".", ".."):
            return False
        parent = os.path.dirname(ap)
        # Parent must canonically BE the chat root (blocks ../ escapes and
        # symlinked parents pointing elsewhere).
        if os.path.realpath(parent) != rroot:
            return False
        # The dir itself must not be a symlink (blocks a planted symlink child).
        if os.path.islink(ap):
            return False
        return True
    except Exception:
        return False


def resolve_chat_dir(root: str, session_id: str, existing_ref: str) -> str:
    """Return the safe dir to use: honour a still-valid existing ref (history
    continuity), else the derived-safe path. Re-validated on every call so a
    tampered ref in bot-state.json is never trusted at runtime."""
    if existing_ref and validate_chat_dir(root, existing_ref):
        return existing_ref
    return derive_chat_dir(root, session_id)


# ── Bounded, atomic, per-session chat history (symlink-hardened) ────────────

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


_HISTORY_FILENAME = "history.json"


def _sanitize_records(data) -> list[dict]:
    """Keep only well-formed ``{role in (user,assistant), content:str}`` records.

    Drops anything malformed/tampered — crucially any stored ``system`` message,
    so a tampered history can never replace the fixed system prompt or inject
    instructions. Content is coerced to str and length-bounded per record."""
    if not isinstance(data, list):
        return []
    cap = _history_max_chars()
    out: list[dict] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        out.append({"role": role, "content": content[:cap]})
    return out


def _open_session_dirfd(chat_dir: str) -> "int | None":
    """Open the session directory itself with ``O_DIRECTORY | O_NOFOLLOW``.

    This is the race fix: a path-based ``islink`` check followed by an operation
    on the same *pathname* (mkstemp / open / remove) can be defeated by swapping
    the directory entry to a symlink between the check and the use (TOCTOU). By
    holding an fd to the real directory inode and doing every file op relative to
    it (``dir_fd=``, openat-style), a later swap of the path cannot redirect us,
    and ``O_NOFOLLOW`` refuses a directory that is *already* a symlink (ELOOP).
    Returns the fd (caller must close) or None (symlink / not-a-dir / missing)."""
    try:
        return os.open(chat_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None


def _load_via_dirfd(dfd: int) -> list[dict]:
    try:
        fd = os.open(_HISTORY_FILENAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except OSError:
        return []  # missing, or history.json itself is a symlink (ELOOP)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return _sanitize_records(data)


class ChatHistory:
    """Per-session message log at ``<chat_dir>/history.json``. Bounded, atomic,
    and race-hardened.

    Every operation opens the session directory with ``O_DIRECTORY|O_NOFOLLOW``
    and does its file I/O relative to that fd (openat semantics), so a directory
    -> symlink swap between check and use cannot redirect a read/write/delete to
    an outside target, and a symlinked session dir is refused outright. Removed
    only by explicit lifecycle events (``purge``)."""

    def __init__(self, chat_dir: str):
        self.chat_dir = chat_dir
        self.path = os.path.join(chat_dir, _HISTORY_FILENAME)

    def load(self) -> list[dict]:
        dfd = _open_session_dirfd(self.chat_dir)
        if dfd is None:
            return []
        try:
            return _load_via_dirfd(dfd)
        finally:
            os.close(dfd)

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

    def append(self, user: str, assistant: str) -> bool:
        """Append one turn atomically. Returns False (no-op) if the session dir
        is unsafe (a symlink / not a directory) — never writes through a swap."""
        # Create the dir on first use; makedirs never creates through a symlink
        # for the final component, and the O_NOFOLLOW open below re-checks anyway.
        try:
            os.makedirs(self.chat_dir, exist_ok=True)
        except OSError:
            return False
        dfd = _open_session_dirfd(self.chat_dir)
        if dfd is None:
            return False  # symlink / non-dir occupying the path -> fail closed
        try:
            msgs = _load_via_dirfd(dfd)
            msgs.append({"role": "user", "content": str(user)})
            msgs.append({"role": "assistant", "content": str(assistant)})
            msgs = self._bound(msgs)
            tmpname = "." + secrets.token_hex(8) + ".hist.tmp"
            try:
                fd = os.open(tmpname,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=dfd)
            except OSError:
                return False
            ok = False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(msgs, f, ensure_ascii=False)
                # Atomic openat rename within the SAME real directory inode.
                os.replace(tmpname, _HISTORY_FILENAME, src_dir_fd=dfd, dst_dir_fd=dfd)
                ok = True
                return True
            finally:
                if not ok:
                    try:
                        os.unlink(tmpname, dir_fd=dfd)
                    except OSError:
                        pass
        finally:
            os.close(dfd)

    def build_messages(self, new_user: str) -> list[dict]:
        # Fixed system prompt ALWAYS first; sanitized history can never inject it.
        return ([{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
                + self.load()
                + [{"role": "user", "content": new_user}])

    def purge(self) -> None:
        """Remove this session's history file and its (empty) directory only.

        Opens the session dir with O_NOFOLLOW and unlinks ``history.json``
        relative to that fd, so a swapped chat_dir symlink can never make us
        delete an outside target. rmdir never follows a final-component symlink
        and only removes an empty real directory."""
        dfd = _open_session_dirfd(self.chat_dir)
        if dfd is not None:
            try:
                try:
                    os.unlink(_HISTORY_FILENAME, dir_fd=dfd)
                except (FileNotFoundError, OSError):
                    pass
            finally:
                os.close(dfd)
        # Remove the session directory ONLY if it is a real, now-empty directory.
        try:
            if not os.path.islink(self.chat_dir) and os.path.isdir(self.chat_dir):
                os.rmdir(self.chat_dir)  # fails (kept) if not empty — never rmtree
        except OSError:
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
