"""Tests for scripts/cleanup-reports.sh (Section E).

Runs the real script against a throwaway report tree (REPORTS_ROOT_GUARD pointed
at tmp) — never touches the real reports directory."""

from __future__ import annotations

import os
import subprocess
import time
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup-reports.sh"
TOKEN = "abcdef0123456789abcdef0123456789"


def _make_report_dir(base: Path, slug: str, content="hi", age_days=0):
    d = base / slug
    d.mkdir(parents=True)
    (d / "summary.txt").write_text(content, encoding="utf-8")
    zpath = base / f"{slug}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(d / "summary.txt", f"{slug}/summary.txt")
    if age_days:
        old = time.time() - age_days * 86400
        for p in (d, d / "summary.txt", zpath):
            os.utime(p, (old, old))
    return d, zpath


def _env(tmp: Path):
    e = dict(os.environ)
    e.update({
        "REPORTS_ROOT_GUARD": str(tmp / "reports"),
        "BOT_REPORTS": str(tmp / "reports"),
        "BOT_CONFIGS_DIR": str(tmp / "configs"),
        "BOT_REPORT_RETENTION_DAYS": "15",
    })
    return e


def _setup(tmp: Path):
    rd = tmp / "reports" / TOKEN
    rd.mkdir(parents=True)
    (tmp / "configs").mkdir()
    (tmp / "configs" / "reports-token.env").write_text(f"REPORTS_PATH_TOKEN={TOKEN}\n")
    return rd


def test_dry_run_changes_nothing(tmp_path):
    rd = _setup(tmp_path)
    _make_report_dir(rd, "old-1", age_days=30)
    before = sorted(p.name for p in rd.iterdir())
    r = subprocess.run(["bash", str(SCRIPT), "--dry-run"], env=_env(tmp_path),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in rd.iterdir()) == before   # nothing deleted
    assert "would remove" in r.stdout


def test_removes_expired_keeps_fresh(tmp_path):
    rd = _setup(tmp_path)
    _make_report_dir(rd, "fresh-1", age_days=0)
    _make_report_dir(rd, "old-1", age_days=30)
    (rd / "hollow-1").mkdir()                                # empty residue dir
    r = subprocess.run(["bash", str(SCRIPT)], env=_env(tmp_path),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    names = {p.name for p in rd.iterdir()}
    assert names == {"fresh-1", "fresh-1.zip"}              # only the intact fresh triplet
    assert "retained_intact=1" in r.stdout


def test_unsafe_token_exits_nonzero(tmp_path):
    _setup(tmp_path)
    (tmp_path / "configs" / "reports-token.env").write_text("REPORTS_PATH_TOKEN=/\n")
    r = subprocess.run(["bash", str(SCRIPT)], env=_env(tmp_path),
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "unexpected format" in r.stderr


def test_refuses_base_outside_guard(tmp_path):
    _setup(tmp_path)
    e = _env(tmp_path)
    # A reports base that resolves OUTSIDE the guard root must be refused (exit 2)
    # before any deletion — the core "never operate outside reports" protection.
    outside = tmp_path / "elsewhere"
    (outside / TOKEN).mkdir(parents=True)
    e["BOT_REPORTS"] = str(outside)
    r = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True)
    assert r.returncode == 2
    assert "outside" in r.stderr


def test_handles_spaces_in_names(tmp_path):
    rd = _setup(tmp_path)
    _make_report_dir(rd, "old report with spaces", age_days=30)
    _make_report_dir(rd, "fresh one", age_days=0)
    r = subprocess.run(["bash", str(SCRIPT)], env=_env(tmp_path),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    names = {p.name for p in rd.iterdir()}
    assert names == {"fresh one", "fresh one.zip"}
