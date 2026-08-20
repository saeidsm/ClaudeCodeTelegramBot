"""Blocking finding 5 — chat-history directory-swap (TOCTOU) races.

The hardening opens the session directory with O_DIRECTORY|O_NOFOLLOW and does
every file op relative to that fd (openat). These tests DETERMINISTICALLY inject
a directory->symlink swap in the window right AFTER the dirfd is opened (by
wrapping ``_open_session_dirfd``) and prove that append / load / purge stay bound
to the ORIGINAL inode and never read, write, or delete an OUTSIDE sentinel.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engines.openrouter_chat as orc
from engines.openrouter_chat import ChatHistory


def _install_swap(monkeypatch, sess: Path, outside: Path):
    """Wrap _open_session_dirfd so that, on its first successful open, the path
    `sess` is swapped to a symlink -> `outside` AFTER we hold the real fd."""
    real = orc._open_session_dirfd
    state = {"done": False, "moved": None}

    def wrapper(chat_dir):
        fd = real(chat_dir)
        if fd is not None and not state["done"]:
            state["done"] = True
            moved = str(sess) + ".moved"
            os.rename(sess, moved)            # real dir slides away
            os.symlink(outside, sess)         # attacker plants a symlink here
            state["moved"] = moved
        return fd

    monkeypatch.setattr(orc, "_open_session_dirfd", wrapper)
    return state


def _mk(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    sess = root / "sess"; sess.mkdir()
    outside = tmp_path / "OUTSIDE"; outside.mkdir()
    (outside / "history.json").write_text('[{"role":"user","content":"SENTINEL"}]')
    return root, sess, outside


def test_append_swap_writes_to_original_inode_not_outside(tmp_path, monkeypatch):
    root, sess, outside = _mk(tmp_path)
    state = _install_swap(monkeypatch, sess, outside)
    h = ChatHistory(str(sess))

    assert h.append("hello", "world") is True
    # outside sentinel is untouched (write never followed the planted symlink)
    assert json.loads((outside / "history.json").read_text()) == \
        [{"role": "user", "content": "SENTINEL"}]
    # the real write landed in the ORIGINAL inode (now at sess.moved)
    moved_hist = Path(state["moved"]) / "history.json"
    assert moved_hist.exists()
    got = json.loads(moved_hist.read_text())
    assert got[-1] == {"role": "assistant", "content": "world"}
    # and the planted path is still just a symlink, not a real dir we wrote into
    assert os.path.islink(sess)


def test_load_swap_reads_original_inode_not_outside(tmp_path, monkeypatch):
    root, sess, outside = _mk(tmp_path)
    # seed the ORIGINAL session dir with distinct content
    (sess / "history.json").write_text('[{"role":"user","content":"ORIGINAL"}]')
    _install_swap(monkeypatch, sess, outside)
    h = ChatHistory(str(sess))

    got = h.load()
    # read the original inode's content, never the outside sentinel
    assert got == [{"role": "user", "content": "ORIGINAL"}]
    assert "SENTINEL" not in json.dumps(got)


def test_purge_swap_does_not_delete_outside(tmp_path, monkeypatch):
    root, sess, outside = _mk(tmp_path)
    (sess / "history.json").write_text('[{"role":"user","content":"ORIGINAL"}]')
    state = _install_swap(monkeypatch, sess, outside)
    h = ChatHistory(str(sess))

    h.purge()
    # the OUTSIDE sentinel survives; only the original inode's file is unlinked
    assert (outside / "history.json").exists()
    assert json.loads((outside / "history.json").read_text()) == \
        [{"role": "user", "content": "SENTINEL"}]
    moved_hist = Path(state["moved"]) / "history.json"
    assert not moved_hist.exists()            # original history.json removed
    # the planted symlink itself is left in place (never followed/deleted target)
    assert os.path.islink(sess)


def test_preplanted_symlink_dir_fails_closed_all_ops(tmp_path):
    # Belt-and-suspenders: a session dir that is ALREADY a symlink is refused
    # outright by the O_NOFOLLOW open (no swap needed).
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "OUT"; outside.mkdir()
    (outside / "history.json").write_text('[{"role":"user","content":"KEEP"}]')
    link = root / "sess"; link.symlink_to(outside)
    h = ChatHistory(str(link))
    assert h.load() == []
    assert h.append("u", "a") is False
    h.purge()
    assert (outside / "history.json").read_text() == '[{"role":"user","content":"KEEP"}]'
