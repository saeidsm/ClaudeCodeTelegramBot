"""Tests for the NW_BIND2 second-listener backport (Section C).

Covers the address validator (empty / same / valid-private / wildcard /
public / invalid) and the site-starter (primary mandatory, secondary failure
does not tear down the primary)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402

PRIMARY = "127.0.0.1"


@pytest.mark.parametrize("bind,expected_ok,reason", [
    ("", False, "empty"),
    ("   ", False, "empty"),
    ("127.0.0.1", False, "same_as_primary"),
    ("10.108.0.4", True, "ok"),          # DO VPC private IP — the prod case
    ("192.168.1.5", True, "ok"),
    ("127.0.0.5", True, "ok"),           # a different loopback addr
    ("0.0.0.0", False, "wildcard"),
    ("::", False, "wildcard"),
    ("8.8.8.8", False, "public"),
    ("224.0.0.1", False, "multicast"),
    ("not-an-ip", False, "invalid"),
    ("10.999.0.1", False, "invalid"),
])
def test_second_bind_validation(bind, expected_ok, reason):
    ok, why = bot._nw_second_bind_ok(bind, PRIMARY)
    assert ok is expected_ok
    assert why == reason


def test_empty_second_bind_default_is_empty():
    # repo default must be empty (never hardcode 10.108.0.4)
    ok, why = bot._nw_second_bind_ok("", PRIMARY)
    assert ok is False and why == "empty"


@pytest.mark.asyncio
async def test_start_sites_primary_and_second():
    started = []

    async def starter(b):
        started.append(b)

    bound = await bot._nw_start_sites(starter, ["127.0.0.1", "10.108.0.4"])
    assert bound == ["127.0.0.1", "10.108.0.4"]
    assert started == ["127.0.0.1", "10.108.0.4"]


@pytest.mark.asyncio
async def test_second_listener_failure_keeps_primary():
    started = []

    async def starter(b):
        if b == "10.108.0.4":
            raise OSError("address unavailable")
        started.append(b)

    bound = await bot._nw_start_sites(starter, ["127.0.0.1", "10.108.0.4"])
    # primary bound, second dropped, no exception propagated
    assert bound == ["127.0.0.1"]
    assert started == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_primary_failure_is_fatal():
    async def starter(b):
        raise OSError("primary cannot bind")

    with pytest.raises(OSError):
        await bot._nw_start_sites(starter, ["127.0.0.1", "10.108.0.4"])
