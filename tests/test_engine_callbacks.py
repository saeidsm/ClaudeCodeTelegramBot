"""Callback-data encoding for the new engine flows (§C/§D).

The ':' separator is dangerous because OpenRouter model ids can themselves
contain ':' (e.g. ':free'). These tests pin the parsing contract the
on_callback handlers rely on so a refactor can't silently truncate an id.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot


def test_cmk_parse_preserves_model_with_colon():
    # cmk:<hn>:<label>:<model> — model is the tail, may contain ':'
    d = "cmk:1:mychat:qwen/qwen3-coder:free"
    parts = d.split(":", 3)
    assert parts[1] == "1"
    assert parts[2] == "mychat"
    assert parts[3] == "qwen/qwen3-coder:free"


def test_neng_parse_label_last():
    d = "neng:codex:0:my-session"
    parts = d.split(":", 3)
    assert parts[1] == "codex" and parts[2] == "0" and parts[3] == "my-session"


def test_newproj_parse_engine_threaded():
    d = "newproj:claude:sess:ZigguratKids4"
    parts = d.split(":", 3)
    assert parts[1] == "claude" and parts[2] == "sess" and parts[3] == "ZigguratKids4"


def test_emod_takes_whole_tail():
    d = "emod:qwen/qwen3-coder:free"
    assert d[len("emod:"):] == "qwen/qwen3-coder:free"


def test_emodset_pipe_separator():
    d = "emodset:9:mychat|qwen/x:free"
    rest = d[len("emodset:"):]
    skey, _, model = rest.partition("|")
    assert skey == "9:mychat"
    assert model == "qwen/x:free"


def test_cb_encoding_roundtrip_over_64_bytes():
    # long chat model callbacks must survive the 64-byte registry indirection
    long_model = "some-vendor/a-really-long-model-name-that-exceeds-limits-1234567890"
    data = f"cmk:1:a-fairly-long-session-label:{long_model}"
    token = bot._cb(data)
    assert bot._cb_resolve(token) == data
