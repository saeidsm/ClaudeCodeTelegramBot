"""Tests for Telegram update_id dedup (Phase 0 A3 Task 1 / risk R3).

Covers: the HWM check-and-set primitive, the group=-1 gate's
ApplicationHandlerStop behavior (real dup vs new id vs a distinct callback
double-tap), and the save_state/load_state persistence round-trip."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


def test_is_duplicate_update_accepts_increasing_ids(monkeypatch):
    monkeypatch.setattr(bot, "_UPDATE_ID_HWM", 0)
    assert bot._is_duplicate_update(100) is False   # first time -> new
    assert bot._is_duplicate_update(100) is True     # exact retry -> duplicate
    assert bot._is_duplicate_update(99) is True      # older id -> duplicate
    assert bot._is_duplicate_update(101) is False    # next id -> new
    assert bot._is_duplicate_update(101) is True     # retry of the new one


def test_is_duplicate_update_never_forgets(monkeypatch):
    # Unlike a capped LRU, the HWM ceiling can't be evicted by intervening
    # traffic — a slow retry of an old id is still caught after many new ones.
    monkeypatch.setattr(bot, "_UPDATE_ID_HWM", 0)
    for i in range(1, 1000):
        assert bot._is_duplicate_update(i) is False
    assert bot._is_duplicate_update(1) is True  # very first id, long after


@pytest.mark.asyncio
async def test_dedup_gate_stops_on_retry_but_not_new_id(monkeypatch):
    monkeypatch.setattr(bot, "_UPDATE_ID_HWM", 0)
    ctx = SimpleNamespace()

    upd_a = SimpleNamespace(update_id=500)
    upd_a_retry = SimpleNamespace(update_id=500)
    upd_b = SimpleNamespace(update_id=501)

    await bot._dedup_gate(upd_a, ctx)          # new -> no exception

    with pytest.raises(ApplicationHandlerStop):
        await bot._dedup_gate(upd_a_retry, ctx)  # exact retry -> stopped

    await bot._dedup_gate(upd_b, ctx)          # different id -> no exception


@pytest.mark.asyncio
async def test_dedup_gate_does_not_dedup_by_callback_data(monkeypatch):
    # Two distinct update_ids carrying the SAME callback_data (a legitimate
    # double-tap) must both pass — dedup is update_id-only, by design.
    monkeypatch.setattr(bot, "_UPDATE_ID_HWM", 0)
    ctx = SimpleNamespace()
    tap1 = SimpleNamespace(update_id=900, callback_query=SimpleNamespace(data="do:deploy_ask"))
    tap2 = SimpleNamespace(update_id=901, callback_query=SimpleNamespace(data="do:deploy_ask"))
    await bot._dedup_gate(tap1, ctx)
    await bot._dedup_gate(tap2, ctx)  # no ApplicationHandlerStop despite same callback_data


def test_save_state_persists_hwm(tmp_path, monkeypatch):
    sf = tmp_path / "bot-state.json"
    monkeypatch.setattr(bot, "STATE_FILE", str(sf))
    monkeypatch.setattr(bot, "_UPDATE_ID_HWM", 424242)
    monkeypatch.setattr(bot.SM, "sessions", {})
    monkeypatch.setattr(bot.SM, "msg_to_session", {})
    monkeypatch.setattr(bot.SM, "color_index", {})
    monkeypatch.setattr(bot, "ACTIVE_SESSION", {})

    bot.save_state()

    saved = json.loads(sf.read_text(encoding="utf-8"))
    assert saved["update_dedup_hwm"] == 424242


def test_load_state_restores_hwm(tmp_path, monkeypatch):
    state = {
        "version": 1, "saved_at": time.time(),
        "sessions": {}, "msg_to_session": {}, "color_index": {},
        "active_session": {}, "update_dedup_hwm": 314159,
    }
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(bot, "STATE_FILE", str(sf))
    monkeypatch.setattr(bot, "_UPDATE_ID_HWM", 0)

    bot.load_state()

    assert bot._UPDATE_ID_HWM == 314159


def test_load_state_missing_hwm_defaults_to_zero(tmp_path, monkeypatch):
    # Old state files (pre-Task-1) never wrote this key — must not crash.
    state = {
        "version": 1, "saved_at": time.time(),
        "sessions": {}, "msg_to_session": {}, "color_index": {}, "active_session": {},
    }
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(bot, "STATE_FILE", str(sf))
    monkeypatch.setattr(bot, "_UPDATE_ID_HWM", 7)

    bot.load_state()

    assert bot._UPDATE_ID_HWM == 7  # max(7, 0) -> unchanged, no crash
