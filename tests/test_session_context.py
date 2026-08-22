"""Session context notice (2I): co-tenancy + upstream divergence.

Both are ADVISORY. The properties that matter are (a) they say something true
when there IS something to say, and (b) they degrade to SILENCE rather than to a
wrong statement or a blocked session start.

AP-5 note: every "is silent" assertion here is paired with a positive control in
the same test or its neighbour, so silence is never asserted against a mechanism
that was never shown able to speak.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


def _sess(sid, label, project):
    s = bot.Session(id=sid, label=label, color_emoji="🔵", session_uuid="u-" + label)
    s.project = project
    return s


@pytest.fixture(autouse=True)
def _clear_cache():
    bot._FETCH_CACHE.clear()
    yield
    bot._FETCH_CACHE.clear()


# ── co-tenancy ───────────────────────────────────────────────────────────────

def test_cotenancy_names_the_other_session(monkeypatch):
    a = _sess("1:a", "Jarvis", "ClaudeCodeTelegramBot")
    b = _sess("1:b", "RAgg", "ClaudeCodeTelegramBot")
    monkeypatch.setattr(bot.SM, "sessions", {a.id: a, b.id: b})
    line = bot._cotenancy_line(a, "ClaudeCodeTelegramBot")
    assert '"RAgg"' in line, line
    assert "ClaudeCodeTelegramBot" in line
    assert "Jarvis" not in line, "must not name itself"


def test_cotenancy_silent_when_alone(monkeypatch):
    a = _sess("1:a", "Jarvis", "ClaudeCodeTelegramBot")
    other = _sess("1:b", "RAgg", "SomeOtherRepo")
    monkeypatch.setattr(bot.SM, "sessions", {a.id: a, other.id: other})
    # positive control first: the mechanism CAN speak, it just has nothing to say
    assert bot._cotenancy_line(a, "SomeOtherRepo") != ""
    assert bot._cotenancy_line(a, "ClaudeCodeTelegramBot") == ""


def test_cotenancy_plural_grammar(monkeypatch):
    a = _sess("1:a", "A", "P")
    monkeypatch.setattr(bot.SM, "sessions", {
        a.id: a, "1:b": _sess("1:b", "B", "P"), "1:c": _sess("1:c", "C", "P")})
    line = bot._cotenancy_line(a, "P")
    assert '"B"' in line and '"C"' in line and " are also working" in line


# ── divergence ───────────────────────────────────────────────────────────────

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def diverged(tmp_path):
    """A real local upstream/clone pair that has genuinely diverged."""
    up = tmp_path / "up"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@t"); _git(up, "config", "user.name", "t")
    (up / "shared.py").write_text("v1\n")
    _git(up, "add", "-A"); _git(up, "commit", "-qm", "one")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(up), str(clone)], check=True,
                   capture_output=True)
    _git(clone, "config", "user.email", "t@t"); _git(clone, "config", "user.name", "t")

    # upstream moves on (touching shared.py), clone moves independently
    (up / "shared.py").write_text("v2\n")
    _git(up, "add", "-A"); _git(up, "commit", "-qm", "upstream change")
    (clone / "local.py").write_text("mine\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-qm", "local change")
    return clone


def test_divergence_reports_and_names_the_file(diverged):
    line = bot._divergence_line(str(diverged))
    assert line, "expected a divergence line"
    assert "1 commit(s) ahead" in line
    assert "shared.py" in line, f"should name the contended file: {line}"


def test_divergence_silent_when_in_sync(tmp_path):
    up = tmp_path / "u"; up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@t"); _git(up, "config", "user.name", "t")
    (up / "a").write_text("x")
    _git(up, "add", "-A"); _git(up, "commit", "-qm", "one")
    clone = tmp_path / "c"
    subprocess.run(["git", "clone", "-q", str(up), str(clone)], check=True,
                   capture_output=True)
    assert bot._divergence_line(str(clone)) == ""


def test_divergence_silent_without_remote(tmp_path):
    r = tmp_path / "norem"; r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    (r / "a").write_text("x")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "one")
    assert bot._divergence_line(str(r)) == ""


def test_divergence_silent_when_fetch_fails(diverged, monkeypatch):
    """A dead network must degrade to silence, never to a wrong statement and
    never to an exception that would block session start."""
    real = subprocess.run

    def fake(args, **kw):
        if isinstance(args, list) and "fetch" in args:
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=1)
        return real(args, **kw)

    # control: it speaks when the fetch works
    assert bot._divergence_line(str(diverged)) != ""
    bot._FETCH_CACHE.clear()
    monkeypatch.setattr(bot.subprocess, "run", fake)
    assert bot._divergence_line(str(diverged)) == ""


def test_divergence_result_is_cached(diverged, monkeypatch):
    calls = {"n": 0}
    real = subprocess.run

    def counting(args, **kw):
        if isinstance(args, list) and "fetch" in args:
            calls["n"] += 1
        return real(args, **kw)

    monkeypatch.setattr(bot.subprocess, "run", counting)
    bot._divergence_line(str(diverged))
    bot._divergence_line(str(diverged))
    bot._divergence_line(str(diverged))
    assert calls["n"] == 1, f"fetch ran {calls['n']} times; TTL cache not working"


# ── the assembled note ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_note_is_empty_when_nothing_to_say(tmp_path, monkeypatch):
    a = _sess("1:a", "Solo", "P")
    monkeypatch.setattr(bot.SM, "sessions", {a.id: a})
    r = tmp_path / "q"; r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    (r / "a").write_text("x")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "one")
    note = await bot._session_context_note(a, "P", str(r))
    assert note == "", f"a quiet estate must see no preamble at all: {note!r}"


@pytest.mark.asyncio
async def test_note_carries_both_lines(diverged, monkeypatch):
    a = _sess("1:a", "Jarvis", "P")
    monkeypatch.setattr(bot.SM, "sessions", {a.id: a, "1:b": _sess("1:b", "RAgg", "P")})
    note = await bot._session_context_note(a, "P", str(diverged))
    assert note.startswith("[SESSION CONTEXT]")
    assert '"RAgg"' in note
    assert "ahead" in note
    assert note.endswith("\n\n"), "must be separated from the user's prompt"


@pytest.mark.asyncio
async def test_note_never_raises_on_a_broken_repo(monkeypatch):
    a = _sess("1:a", "Jarvis", "P")
    monkeypatch.setattr(bot.SM, "sessions", {a.id: a})
    note = await bot._session_context_note(a, "P", "/nonexistent/path/xyz")
    assert note == ""
