"""Resource footer: parse, cache, idempotency, length/HTML safety (§J)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resource_footer as rf


MEMINFO = """MemTotal:       16384000 kB
MemAvailable:    8192000 kB
SwapTotal:       2048000 kB
SwapFree:        1024000 kB
Buffers:          100000 kB
"""


def test_parse_meminfo():
    m = rf.parse_meminfo(MEMINFO)
    assert m["MemTotal"] == 16384000 * 1024
    assert m["MemAvailable"] == 8192000 * 1024
    assert m["SwapFree"] == 1024000 * 1024


def test_parse_meminfo_ignores_garbage():
    m = rf.parse_meminfo("nonsense line\nMemTotal: notanumber kB\nMemFree: 5 kB\n")
    assert "MemTotal" not in m
    assert m["MemFree"] == 5 * 1024


def test_footer_text_is_cached(monkeypatch):
    rf._reset_cache_for_test()
    calls = {"n": 0}
    orig = rf._compute

    def counting():
        calls["n"] += 1
        return "🖥 RAM 1G/2G · Swap 0B/0B · Disk 3G/4G"

    monkeypatch.setattr(rf, "_compute", counting)
    a = rf.footer_text(now=1000.0)
    b = rf.footer_text(now=1002.0)          # within TTL
    assert a == b and calls["n"] == 1
    c = rf.footer_text(now=1000.0 + rf._CACHE_TTL + 1)  # past TTL
    assert calls["n"] == 2


def test_with_footer_appends_once(monkeypatch):
    rf._reset_cache_for_test()
    monkeypatch.setattr(rf, "_compute", lambda: "🖥 RAM 1G/2G · Swap 0B/0B · Disk 3G/4G")
    txt = "hello"
    once = rf.with_footer(txt)
    assert rf.FOOTER_MARK in once
    twice = rf.with_footer(once)            # idempotent — no double footer
    assert once == twice
    assert twice.count(rf.FOOTER_MARK) == 1


def test_with_footer_length_aware(monkeypatch):
    rf._reset_cache_for_test()
    monkeypatch.setattr(rf, "_compute", lambda: "🖥 footer")
    big = "x" * (rf._TELEGRAM_MAX - 3)
    out = rf.with_footer(big)
    assert out == big                        # no footer when it would overflow


def test_human_units():
    assert rf._human(0) == "0B"
    assert rf._human(1500).endswith("K")
    assert rf._human(2 * 1024 * 1024 * 1024).endswith("G")
