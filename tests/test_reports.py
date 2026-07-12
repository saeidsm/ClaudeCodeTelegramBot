"""Tests for the Phase 1 report layer (Section D).

Covers: successful build, summary content/encoding, ZIP content, collision-safe
slugs, simulated archive failure, no phantom ZIP link on failure, clickable HTML
output, and every-caller tolerance of partial/error results.
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402

URL = "https://example.test/reports/TOK"


def test_build_report_success(tmp_path):
    res = bot._build_report(str(tmp_path), URL, "proj-task", "hello world")
    assert res.status == "ok" and res.ok
    assert set(res.links) == {"Summary", "Browse", "ZIP"}
    slug = res.slug
    assert (tmp_path / slug / "summary.txt").is_file()
    assert (tmp_path / f"{slug}.zip").is_file()
    assert res.links["ZIP"] == f"{URL}/{slug}.zip"


def test_summary_content_and_utf8(tmp_path):
    content = "گزارش نهایی — Ünïcode ✅\nline2"
    res = bot._build_report(str(tmp_path), URL, "p", content)
    data = (tmp_path / res.slug / "summary.txt").read_text(encoding="utf-8")
    assert data == content


def test_zip_contains_summary(tmp_path):
    res = bot._build_report(str(tmp_path), URL, "p", "payload-XYZ")
    zpath = tmp_path / f"{res.slug}.zip"
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        assert f"{res.slug}/summary.txt" in names
        assert zf.read(f"{res.slug}/summary.txt").decode("utf-8") == "payload-XYZ"


def test_slugs_unique_rapid():
    slugs = {bot._report_slug("proj") for _ in range(200)}
    assert len(slugs) == 200  # random suffix prevents same-second collisions


def test_build_reports_rapid_no_overwrite(tmp_path):
    a = bot._build_report(str(tmp_path), URL, "p", "AAA")
    b = bot._build_report(str(tmp_path), URL, "p", "BBB")
    assert a.slug != b.slug
    assert (tmp_path / a.slug / "summary.txt").read_text() == "AAA"
    assert (tmp_path / b.slug / "summary.txt").read_text() == "BBB"


def test_archive_failure_is_partial(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(bot.zipfile, "ZipFile", boom)
    res = bot._build_report(str(tmp_path), URL, "p", "content")
    assert res.status == "partial"
    # summary is still written and its links exist...
    assert (tmp_path / res.slug / "summary.txt").is_file()
    assert "Summary" in res.links and "Browse" in res.links
    # ...but NO phantom ZIP link, and no leftover tmp archive.
    assert "ZIP" not in res.links
    assert not any(p.name.endswith(".zip.tmp") or p.suffix == ".zip"
                   for p in tmp_path.iterdir())


def test_no_phantom_zip_when_verification_fails(tmp_path, monkeypatch):
    # Build a ZipFile whose archive verifies as empty/namelist-missing.
    real_zipfile = bot.zipfile.ZipFile

    class EmptyZip:
        def __init__(self, path, mode="r", *a, **k):
            self._path = path
            self._mode = mode
        def __enter__(self):
            if "w" in self._mode:
                open(self._path, "wb").close()  # create an empty file
            return self
        def __exit__(self, *a):
            return False
        def write(self, *a, **k):
            pass
        def testzip(self):
            return None
        def namelist(self):
            return []
    monkeypatch.setattr(bot.zipfile, "ZipFile", EmptyZip)
    res = bot._build_report(str(tmp_path), URL, "p", "content")
    assert res.status == "partial"
    assert "ZIP" not in res.links


def test_fmt_links_clickable_html(tmp_path):
    res = bot._build_report(str(tmp_path), URL, "p", "x")
    html_out = bot.fmt_links(res)
    assert "<a href=" in html_out           # deliberate clickable anchor
    assert res.links["Summary"] in html_out  # raw URL still visible as fallback
    # never wrap the link ONLY in <code> (the old broken behavior)
    assert '<a href="' in html_out


def test_fmt_links_partial_notes_missing_zip(tmp_path):
    res = bot.ReportResult("partial", bot.OrderedDict(
        [("Summary", f"{URL}/s/summary.txt"), ("Browse", f"{URL}/s/")]), "s", "zip error")
    out = bot.fmt_links(res)
    # partial warning present
    assert "ZIP unavailable" in out
    # Summary + Browse present (link + anchor)
    assert f"{URL}/s/summary.txt" in out and f"{URL}/s/" in out
    assert out.count("<a href=") == 2                 # exactly Summary + Browse anchors
    # NO phantom ZIP url and NO ZIP anchor
    assert ".zip" not in out
    assert ">ZIP:" not in out and "ZIP:</b>" not in out


def test_fmt_links_error_is_safe():
    res = bot.ReportResult("error", bot.OrderedDict(), "", "boom")
    out = bot.fmt_links(res)
    assert "failed" in out.lower()          # caller can send this, no crash


def test_fmt_links_accepts_plain_dict():
    # backward-compatible mapping input still renders
    out = bot.fmt_links({"Summary": "https://x/y"})
    assert "<a href=" in out


@pytest.mark.asyncio
async def test_make_report_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "REPORTS", str(tmp_path))
    monkeypatch.setattr(bot, "REPORT_URL", URL)
    res = await bot.make_report("proj-auto", "big output")
    assert res.status == "ok"
    assert (tmp_path / res.slug / "summary.txt").is_file()


# ── Section 9: slug sanitization / path-traversal safety ──────────────────────

@pytest.mark.parametrize("name", [
    "../../outside",
    "/absolute/path",
    "a\\b",
    "...",
    "..",
    ".",
    "with\nnewline\tand\x00control",
    "پروژه-فارسی",                     # readable Persian must survive
    "x" * 500,                          # very long
    "",                                 # empty
])
def test_slug_is_single_safe_segment(name):
    slug = bot._report_slug(name)
    # never a traversal or separator; never empty; bounded
    assert "/" not in slug and "\\" not in slug
    assert ".." not in slug
    assert not slug.startswith(".")
    assert "\n" not in slug and "\t" not in slug and "\x00" not in slug
    assert slug and len(slug) < 200
    # os.path.basename round-trips → it IS a single path segment
    assert os.path.basename(slug) == slug


def test_persian_name_preserved():
    slug = bot._report_slug("پروژه")
    assert "پروژه" in slug             # readable Unicode retained


@pytest.mark.parametrize("evil", ["../../etc/passwd", "/abs/x", "a\\b\\c", "..", "."])
def test_report_dir_and_zip_are_immediate_children(tmp_path, evil):
    res = bot._build_report(str(tmp_path), URL, evil, "payload")
    # whatever the name, the dir + zip are direct children of tmp_path
    assert (tmp_path / res.slug).parent == tmp_path
    assert (tmp_path / res.slug / "summary.txt").is_file()
    if res.status == "ok":
        zp = tmp_path / f"{res.slug}.zip"
        assert zp.parent == tmp_path and zp.is_file()
    # nothing escaped tmp_path
    for child in tmp_path.iterdir():
        assert tmp_path in child.resolve().parents or child.resolve() == tmp_path or child.parent == tmp_path


def test_build_report_rejects_direct_traversal_slug(tmp_path):
    # even if a caller passes an explicit unsafe slug, it is replaced
    res = bot._build_report(str(tmp_path), URL, "x", "data", slug="..")
    assert res.slug != ".." and (tmp_path / res.slug).parent == tmp_path
