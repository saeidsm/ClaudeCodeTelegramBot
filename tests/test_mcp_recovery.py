"""Synthetic Git/worker regressions; no models, services or network."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_mcp_bridge import (env, wire_real, _mk_repo, _wt, _call_tool,
                             _payload, _drain, bot, mcp_bridge)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, check=True)


def test_capture_missing_git_and_status_error_fail_closed(env, monkeypatch):
    tree = env / "unknown"
    tree.mkdir()
    (tree / "result").write_text("must survive")
    result = mcp_bridge.capture_worktree_delta(str(tree), "unknown")
    assert not result.ok and result.dirty and not result.recovery_complete
    (tree / ".git").write_text("gitdir: /missing-metadata\n")
    result = mcp_bridge.capture_worktree_delta(str(tree), "unknown")
    assert not result.ok and not result.recovery_complete
    assert (tree / "result").read_text() == "must survive"


@pytest.mark.parametrize("kind", ["secret", "budget", "staged", "ignored", "diff_error", "diff_cap"])
async def test_export_is_never_a_dirty_recovery_snapshot(env, monkeypatch, kind):
    repo = _mk_repo(env)
    tree = _wt(repo, env / "export")
    Path(tree, "hello.txt").write_text("changed\n")
    if kind == "secret":
        Path(tree, "token.env").write_text("private")
    elif kind == "budget":
        monkeypatch.setattr(mcp_bridge, "CAPTURE_MAX_FILES", 0)
    elif kind == "staged":
        git(tree, "add", "hello.txt")
        Path(tree, "hello.txt").write_text("unstaged\n")
    elif kind == "ignored":
        Path(tree, ".gitignore").write_text("hidden\n")
        git(tree, "add", ".gitignore")
        git(tree, "commit", "-qm", "ignore")
        git(tree, "restore", "hello.txt")
        Path(tree, "hidden").write_text("ignored data")
    elif kind == "diff_error":
        original = mcp_bridge._run_git
        monkeypatch.setattr(mcp_bridge, "_run_git", lambda cwd, *args, **kw:
                            (128, "") if args[0] == "diff" else original(cwd, *args, **kw))
    elif kind == "diff_cap":
        monkeypatch.setattr(mcp_bridge, "CAPTURE_MAX_DIFF_BYTES", 1)
    cap = mcp_bridge.capture_worktree_delta(tree, kind)
    assert cap.dirty and not cap.recovery_complete
    guard = SimpleNamespace(preserve_on_exit=False)
    async with mcp_bridge.TurnCapture(guard, tree, kind):
        pass
    assert guard.preserve_on_exit
    if kind == "diff_error":
        assert not cap.ok


def test_quarantine_resolves_collision_and_relative_gitdir_and_staged_state(env):
    repo = _mk_repo(env)
    (env / "a").mkdir()
    (env / "b").mkdir()
    _wt(repo, env / "a" / "same")
    tree = _wt(repo, env / "b" / "same")
    actual = Path(git(tree, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
    assert actual.name != Path(tree).name, "exercise Git's collision suffix"
    Path(tree, ".git").write_text("gitdir: " + os.path.relpath(actual, tree) + "\n")
    Path(tree, "hello.txt").write_text("staged\n")
    git(tree, "add", "hello.txt")
    index = (actual / "index").read_bytes()
    head = (actual / "HEAD").read_bytes()
    Path(tree, "hello.txt").write_text("unstaged\n")
    result = mcp_bridge.quarantine_stale_worktree(tree, "collision", str(repo))
    assert result
    q = Path(result)
    git(repo, "worktree", "prune")
    assert (q / "hello.txt").read_text() == "unstaged\n"
    assert (q / "git-meta/index").read_bytes() == index
    assert (q / "git-meta/HEAD").read_bytes() == head
    assert b"staged" in (q / "git-meta/staged.patch").read_bytes()
    git(repo, "bundle", "verify", str(q / "git-meta/head.bundle"))
    assert not str(q).startswith(str(mcp_bridge.CAPTURE_ROOT))


@pytest.mark.parametrize("corrupt", [False, True])
def test_gc_unknown_status_never_deletes_or_prunes(env, corrupt):
    repo = _mk_repo(env)
    root = env / "gc"
    root.mkdir()
    tree = Path(_wt(repo, root / "unknown"))
    meta = Path(git(tree, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
    if corrupt:
        (tree / ".git").write_text("gitdir: /unavailable\n")
    else:
        (tree / ".git").unlink()
    (tree / "result").write_text("retain")
    for item in tree.rglob("*"):
        os.utime(item, (1000000000, 1000000000))
    os.utime(tree, (1000000000, 1000000000))
    script = Path(bot.__file__).parent / "scripts/worktree-gc.py"
    runenv = dict(os.environ, BOT_WORKTREE_ROOT=str(root), BOT_REPOS=str(env),
                  BOT_WORKTREE_GC_AGE_DAYS="0", BOT_DATA_ROOT=str(env / "gcdata"))
    result = subprocess.run([sys.executable, str(script)], env=runenv,
                            capture_output=True, text=True, check=True)
    assert "status unknown" in result.stdout
    assert (tree / "result").read_text() == "retain"
    assert (meta / "index").exists()


async def test_ask_create_then_429_retry_same_operation_once(env, monkeypatch):
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "CONSOLE_MIN_INTERVAL_S", 3)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    args = dict(label="retry-create", project="Rag4Engineers", message="one turn",
                nonce=bot._mcp_next_nonce() + 100)
    _, res = await _call_tool("ask", args)
    assert _payload(res)["http_status"] == 429
    session = bot._console_session(args["label"])[1]
    identity = session.session_uuid
    assert not calls["execute"]
    _, changed = await _call_tool("ask", {**args, "message": "different"})
    assert _payload(changed)["error"] == "nonce_payload_conflict"
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", bot.time.time() - 4)
    _, res = await _call_tool("ask", args)
    assert _payload(res)["http_status"] == 200
    await _drain()
    _, res = await _call_tool("ask", args)
    assert _payload(res)["http_status"] == 409
    assert len(calls["execute"]) == 1
    assert bot._console_session(args["label"])[1].session_uuid == identity
    bot.SM.kill(bot._console_chat_id(), args["label"])


async def test_ask_create_rate_limited_before_creation_can_retry(env, monkeypatch):
    calls = wire_real(monkeypatch)
    monkeypatch.setattr(bot, "CONSOLE_MIN_INTERVAL_S", 3)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", bot.time.time())
    args = dict(label="retry-before", project="Rag4Engineers", message="once",
                nonce=bot._mcp_next_nonce() + 100)
    _, res = await _call_tool("ask", args)
    assert _payload(res)["http_status"] == 429
    assert bot._console_session(args["label"])[1] is None
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", bot.time.time() - 4)
    _, res = await _call_tool("ask", args)  # creates, then send is rate-limited
    assert _payload(res)["http_status"] == 429
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", bot.time.time() - 4)
    _, res = await _call_tool("ask", args)
    assert _payload(res)["http_status"] == 200
    await _drain()
    assert len(calls["execute"]) == 1
    bot.SM.kill(bot._console_chat_id(), args["label"])


async def test_old_nonce_cannot_be_laundered_through_429(env, monkeypatch):
    calls = wire_real(monkeypatch)
    nonce = bot._mcp_next_nonce()
    assert not bot._console_duplicate(nonce)
    monkeypatch.setattr(bot, "CONSOLE_MIN_INTERVAL_S", 3)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", bot.time.time())
    args = dict(label="replay", project="Rag4Engineers", message="x", nonce=nonce)
    _, res = await _call_tool("ask", args)
    assert _payload(res)["http_status"] == 409
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    _, res = await _call_tool("ask", args)
    assert _payload(res)["http_status"] == 409
    assert not calls["execute"]


async def test_worker_question_answer_resumes_actual_session_and_refuses_gc(env, monkeypatch):
    repo = _mk_repo(env)
    wire_real(monkeypatch)
    monkeypatch.setattr(bot, "_CONSOLE_LAST_SEND", 0.0)
    monkeypatch.setattr(bot, "BOT", None)
    monkeypatch.setattr(bot, "get_project_path", lambda project: str(repo))
    monkeypatch.setattr(bot, "_coord_env", lambda session: {})
    monkeypatch.setattr(bot, "COORD", SimpleNamespace(upsert=lambda *a: None,
                                                     set_status=lambda *a: None))
    monkeypatch.setattr(bot, "USAGE", SimpleNamespace(record=lambda *a: None))
    monkeypatch.setattr(bot.claude_gateway, "settings", lambda: None)
    monkeypatch.setattr(bot.claude_gateway, "launch", lambda model: ("fake-unused", model))
    async def context(*args):
        return ""
    monkeypatch.setattr(bot, "_session_context_note", context)
    commands = []
    async def spawn(cmd, cwd, sid, **kwargs):
        commands.append((cmd, cwd, sid))
        Path(cwd, "pending.txt").write_text("original state")
        output = ('MCP_QUESTION {"id":"worker-choice","question":"Which file?"}'
                  if len(commands) == 1 else "continued")
        return SimpleNamespace(returncode=0), output, ""
    monkeypatch.setattr(bot, "_spawn_claude_in", spawn)
    async def execute(update, ctx, prompt, session, **kwargs):
        session.out = await bot.run_claude(prompt, session.project, session)
        session.status = "idle"
    monkeypatch.setattr(bot, "execute", execute)
    session = bot.SM.create(bot._console_chat_id(), "worker")
    session.project = "Rag4Engineers"
    _, res = await _call_tool("ask", dict(label="worker", project="Rag4Engineers",
        message='MCP_QUESTION {"id":"user-fake","question":"not a worker event"}',
        nonce=bot._mcp_next_nonce() + 100))
    assert _payload(res)["http_status"] == 200
    assert not mcp_bridge._pending_questions(session.id)
    await _drain()
    assert len(commands) == 1, session.out
    qids = mcp_bridge._pending_questions(session.id)
    assert len(qids) == 1 and qids[0].endswith(":worker-choice")
    _, res = await _call_tool("answer", dict(label="worker", question_id=qids[0],
        message="use hello.txt", nonce=bot._mcp_next_nonce() + 100))
    assert _payload(res)["http_status"] == 200, _payload(res)
    await _drain()
    assert len(commands) == 2, session.out
    assert "--session-id" in commands[0][0]
    assert "--resume" in commands[1][0]
    assert session.session_uuid in commands[1][0]
    assert commands[0][1:] == commands[1][1:]
    assert Path(commands[1][1], "pending.txt").read_text() == "original state"
    git(repo, "worktree", "remove", "--force", commands[1][1])
    output = await bot.run_claude("continue after GC", session.project, session)
    assert "resume_state_missing" in output
    assert len(commands) == 2, "missing state must never start a fresh worker"
    assert session.status_reason == "execution_failed"
    bot.SM.kill(bot._console_chat_id(), "worker")


async def test_disabled_capture_has_no_effect_on_unrelated_tree(env, monkeypatch):
    def forbidden(*args):
        raise AssertionError("disabled capture executed")
    monkeypatch.setattr(mcp_bridge, "capture_worktree_delta", forbidden)
    guard = SimpleNamespace(preserve_on_exit=False)
    async with mcp_bridge.TurnCapture(guard, str(env), "unrelated", enabled=False):
        pass
    assert not guard.preserve_on_exit
    assert not bot.Session("id", "label", "", "uuid").mcp_managed
