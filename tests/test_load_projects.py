"""Tests for load_projects() — handles corrupt/wrong-type projects.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


def test_load_projects_dict_fallback(tmp_path, monkeypatch) -> None:
    """If projects.json contains {} (dict), load_projects() must return a list,
    not raise AttributeError on the caller's .append()."""
    projects_file = tmp_path / "projects.json"
    projects_file.write_text("{}")

    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()

    monkeypatch.setattr(bot, "PROJECTS_FILE", str(projects_file))
    monkeypatch.setattr(bot, "REPOS", str(repos_dir))

    result = bot.load_projects()

    assert isinstance(result, list)
    # auto-generate overwrote the bad {} with a proper []
    assert json.loads(projects_file.read_text()) == []
    # .append() must not crash — this is the actual regression we're guarding
    result.append({"name": "x", "path": "/tmp/x"})
