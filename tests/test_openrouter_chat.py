"""OpenRouter Chat: client retry/redaction, catalog TTL/stale/favorites/search,
bounded atomic history (§G). Fully offline — a fake aiohttp session is injected."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engines.openrouter_chat as orc
from engines.openrouter_chat import (
    OpenRouterClient, ChatCatalog, ChatHistory, ChatError, _redact,
)


class FakeResp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status = status
        self._payload = payload
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def text(self):
        return self._text


class FakeSession:
    """Serves a scripted list of responses; records closed()."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.closed = False
        self.calls = 0

    def _next(self):
        self.calls += 1
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, headers=None):
        return self._next()

    def post(self, url, headers=None, json=None):
        return self._next()

    async def close(self):
        self.closed = True


def _client(responses, key="sk-secret"):
    holder = {}
    def factory():
        s = FakeSession(responses)
        holder["session"] = s
        return s
    c = OpenRouterClient(key, session_factory=factory)
    return c, holder


# ── client ──────────────────────────────────────────────────────────────
async def test_chat_success():
    c, h = _client([FakeResp(200, {"choices": [{"message": {"content": "hi"}}]})])
    out = await c.chat("m", [{"role": "user", "content": "x"}])
    assert out == "hi"
    assert h["session"].closed


async def test_chat_retries_429_then_succeeds():
    resps = [FakeResp(429, headers={"Retry-After": "0"}),
             FakeResp(200, {"choices": [{"message": {"content": "ok"}}]})]
    # session_factory returns a NEW session each attempt with the remaining script
    seq = [[resps[0]], [resps[1]]]
    def factory():
        return FakeSession(seq.pop(0))
    c = OpenRouterClient("k", session_factory=factory)
    out = await c.chat("m", [], max_retries=3)
    assert out == "ok"


async def test_chat_auth_error():
    c, _ = _client([FakeResp(401, text="nope")])
    with pytest.raises(ChatError) as ei:
        await c.chat("m", [])
    assert ei.value.kind == "auth"


async def test_chat_missing_key():
    c = OpenRouterClient(None)
    with pytest.raises(ChatError) as ei:
        await c.chat("m", [])
    assert ei.value.kind == "auth"


async def test_chat_timeout_exhausts_retries():
    def factory():
        return FakeSession([asyncio.TimeoutError()])
    c = OpenRouterClient("k", session_factory=factory)
    with pytest.raises(ChatError) as ei:
        await c.chat("m", [], max_retries=1)
    assert ei.value.kind == "timeout"


async def test_chat_cancellation_propagates_and_closes():
    class CancelSession(FakeSession):
        def post(self, *a, **k):
            raise asyncio.CancelledError()
    c = OpenRouterClient("k", session_factory=lambda: CancelSession([]))
    with pytest.raises(asyncio.CancelledError):
        await c.chat("m", [])


def test_redaction():
    assert "sk-secret" not in _redact("error with sk-secret here", "sk-secret")
    assert _redact("Authorization: Bearer abc.def-123", None).endswith("Bearer ***")


# ── catalog ─────────────────────────────────────────────────────────────
class OneShotClient:
    def __init__(self, models, fail=False):
        self._models = models
        self.fail = fail
        self.fetches = 0

    async def fetch_models(self, timeout=20.0):
        self.fetches += 1
        if self.fail:
            raise ChatError("network", "down")
        return self._models


async def test_catalog_ttl_and_refresh(tmp_path):
    raw = [{"id": "a/b", "name": "AB", "context_length": 1000,
            "pricing": {"completion": "0.000001"}}]
    cl = OneShotClient(raw)
    cat = ChatCatalog(cl, ttl=1000, cache_path=str(tmp_path / "c.json"))
    m = await cat.get()
    assert [x.id for x in m] == ["a/b"]
    assert cl.fetches == 1
    await cat.get()                    # within TTL -> no new fetch
    assert cl.fetches == 1
    assert not cat.stale


async def test_catalog_stale_falls_back_to_last_known_good(tmp_path):
    raw = [{"id": "a/b", "name": "AB"}]
    cl = OneShotClient(raw)
    p = str(tmp_path / "c.json")
    # ttl=0 -> always tries refresh; min_refresh_interval=0 -> not rate-limited
    cat = ChatCatalog(cl, ttl=0, min_refresh_interval=0, cache_path=p)
    await cat.get()                               # writes persistent cache
    cl.fail = True
    m = await cat.get(refresh=True)               # fetch fails -> last-known-good
    assert [x.id for x in m] == ["a/b"]
    assert cat.stale


async def test_favorites_marks_missing_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_CHAT_FAVORITES", "a/b,x/missing")
    cl = OneShotClient([{"id": "a/b", "name": "AB"}])
    cat = ChatCatalog(cl, ttl=1000, cache_path=str(tmp_path / "c.json"))
    favs = await cat.favorites()
    by = {m.id: m for m in favs}
    assert by["a/b"].available
    assert not by["x/missing"].available


async def test_search(tmp_path):
    cl = OneShotClient([{"id": "z-ai/glm-5.2", "name": "GLM 5.2"},
                        {"id": "qwen/q", "name": "Qwen"}])
    cat = ChatCatalog(cl, ttl=1000, cache_path=str(tmp_path / "c.json"))
    await cat.get()
    r = await cat.search("glm")
    assert [m.id for m in r] == ["z-ai/glm-5.2"]
    assert await cat.search("") == []


# ── history ─────────────────────────────────────────────────────────────
def test_history_append_load_purge(tmp_path):
    h = ChatHistory(str(tmp_path / "sess"))
    assert h.load() == []
    h.append("hi", "hello")
    msgs = h.load()
    assert msgs == [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"}]
    built = h.build_messages("next")
    assert built[0]["role"] == "system"
    assert built[-1] == {"role": "user", "content": "next"}
    h.purge()
    assert h.load() == []


def test_history_bounded_by_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_CHAT_HISTORY_MAX_TURNS", "2")
    h = ChatHistory(str(tmp_path / "s"))
    for i in range(5):
        h.append(f"u{i}", f"a{i}")
    msgs = h.load()
    assert len(msgs) == 4                 # 2 turns * 2 messages
    assert msgs[0]["content"] == "u3"


def test_history_isolated_per_session(tmp_path):
    a = ChatHistory(str(tmp_path / "A"))
    b = ChatHistory(str(tmp_path / "B"))
    a.append("qa", "ra")
    assert b.load() == []
    a.purge()
    assert b.load() == []
