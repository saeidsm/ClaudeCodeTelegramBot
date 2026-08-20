"""Codex command construction + JSONL parsing + failure classification (§F)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engines.codex_adapter import (
    build_codex_cmd, parse_codex_output, classify_codex_failure,
    parse_codex_models,
)


def test_build_cmd_first_turn():
    cmd = build_codex_cmd("do it", "gpt-5.6-sol", None)
    assert cmd[:2] == ["codex", "exec"]
    assert "resume" not in cmd
    assert "--json" in cmd and "--skip-git-repo-check" in cmd
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-5.6-sol"
    assert cmd[-1] == "do it"
    # approval + sandbox forced non-interactive
    assert 'approval_policy="never"' in cmd


def test_build_cmd_resume_uses_thread_id():
    cmd = build_codex_cmd("more", "gpt-5.6-luna", "abc-123")
    assert cmd[:3] == ["codex", "exec", "resume"]
    assert cmd[3] == "abc-123"
    assert cmd[-1] == "more"


def test_build_cmd_images():
    cmd = build_codex_cmd("look", "gpt-5.6-sol", None, images=["/a.png", "/b.jpg"])
    assert cmd.count("-i") == 2


def test_sandbox_mode_maps_permission(monkeypatch):
    monkeypatch.delenv("BOT_CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("BOT_TOOL_PERMISSION_MODE", "skip")
    cmd = build_codex_cmd("x", "gpt-5.6-sol", None)
    assert any('sandbox_mode="danger-full-access"' in c for c in cmd)
    monkeypatch.setenv("BOT_TOOL_PERMISSION_MODE", "strict")
    cmd = build_codex_cmd("x", "gpt-5.6-sol", None)
    assert any('sandbox_mode="workspace-write"' in c for c in cmd)


FIRST_TURN = "\n".join([
    "Reading additional input from stdin...",  # non-JSON noise, must be ignored
    '{"type":"thread.started","thread_id":"019f-uuid"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"i0","type":"error","message":"skills note"}}',
    '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"pong"}}',
    '{"type":"turn.completed","usage":{"input_tokens":22421,"output_tokens":5}}',
])


def test_parse_first_turn():
    r = parse_codex_output(FIRST_TURN)
    assert r.thread_id == "019f-uuid"
    assert r.text == "pong"
    assert r.output_tokens == 5 and r.input_tokens == 22421
    assert r.saw_agent_message and r.saw_turn_completed
    assert not r.malformed
    # non-fatal error item captured as diagnostic, not surfaced (text present)
    assert any("skills note" in d for d in r.diagnostics)


def test_parse_multiple_agent_messages_concatenated():
    s = "\n".join([
        '{"type":"item.completed","item":{"type":"agent_message","text":"part1"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"part2"}}',
        '{"type":"turn.completed","usage":{}}',
    ])
    r = parse_codex_output(s)
    assert r.text == "part1\n\npart2"


def test_parse_malformed_and_garbage():
    r = parse_codex_output("not json at all\n{broken\n")
    assert r.text == "" and r.malformed


def test_classify_failure():
    assert classify_codex_failure(0, "ok", "") == "ok"
    assert classify_codex_failure(1, "hit rate limit", "") == "rate_limit"
    assert classify_codex_failure(1, "", "please run codex login") == "auth"
    assert classify_codex_failure(3, "boom", "") == "nonzero"
    # auth wins over rate keyword if both present (more actionable)
    assert classify_codex_failure(1, "429 too many", "unauthorized") == "auth"


def test_parse_models_filters_visibility():
    doc = (
        '{"models":[{"slug":"a","display_name":"A","visibility":"list","supported_in_api":true},'
        '{"slug":"h","display_name":"H","visibility":"hide","supported_in_api":true},'
        '{"slug":"n","display_name":"N","visibility":"list","supported_in_api":false}]}'
    )
    got = parse_codex_models(doc)
    assert [m.id for m in got] == ["a"]
    assert parse_codex_models("garbage") == []
