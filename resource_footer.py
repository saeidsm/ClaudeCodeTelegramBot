"""Compact cached resource footer (Phase 2 §J).

Appends ``RAM available/total | Swap free/total | Disk free/total`` to primary
user-visible responses. Reads ``/proc/meminfo`` + ``os.statvfs`` (no subprocess
per message), caches 5–10s, is HTML-safe and Telegram-length-aware, and is
idempotent so edits/reports never get a doubled footer.

Excluded surfaces (callback acks, binary uploads, logs) simply don't call
``with_footer`` — the send helper decides.
"""
from __future__ import annotations

import os
import time

FOOTER_MARK = "🖥"
_CACHE_TTL = 8.0            # seconds (within the 5–10s window)
_TELEGRAM_MAX = 4096

_cache: tuple[float, str] = (0.0, "")


def _human(nbytes: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if nbytes < 1024 or unit == "T":
            return f"{nbytes:.0f}{unit}" if unit in ("B", "K") else f"{nbytes:.1f}{unit}"
        nbytes /= 1024
    return f"{nbytes:.1f}T"


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo body into a {key: bytes} dict (values are kB * 1024)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        val = parts[1].strip().split()
        try:
            kb = int(val[0])
        except (ValueError, IndexError):
            continue
        out[key] = kb * 1024
    return out


def _disk(path: str) -> tuple[int, int]:
    try:
        st = os.statvfs(path)
        free = st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize
        return free, total
    except Exception:
        return 0, 0


def _compute() -> str:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem = parse_meminfo(f.read())
    except Exception:
        mem = {}
    ram_avail = mem.get("MemAvailable", 0)
    ram_total = mem.get("MemTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_total = mem.get("SwapTotal", 0)
    disk_path = os.environ.get("BOT_DATA_ROOT") or "/"
    disk_free, disk_total = _disk(disk_path)
    return (f"{FOOTER_MARK} RAM {_human(ram_avail)}/{_human(ram_total)} · "
            f"Swap {_human(swap_free)}/{_human(swap_total)} · "
            f"Disk {_human(disk_free)}/{_human(disk_total)}")


def footer_text(now: float | None = None) -> str:
    """Cached one-line footer (no HTML tags — caller wraps it)."""
    global _cache
    now = time.time() if now is None else now
    ts, val = _cache
    if val and (now - ts) < _CACHE_TTL:
        return val
    val = _compute()
    _cache = (now, val)
    return val


def with_footer(text: str, *, html: bool = True) -> str:
    """Append the footer once. Idempotent and length-aware.

    Skips if a footer is already present (edit re-render) or if appending would
    exceed Telegram's message limit.
    """
    if FOOTER_MARK in text[-220:]:
        return text
    foot = footer_text()
    block = f"\n\n<code>{foot}</code>" if html else f"\n\n{foot}"
    if len(text) + len(block) > _TELEGRAM_MAX:
        return text
    return text + block


def _reset_cache_for_test() -> None:
    global _cache
    _cache = (0.0, "")
