"""Tests for scripts/cleanup-reports.sh (Sections E + 8).

Production deletion scope is HARDCODED; tests retarget it ONLY via the explicit
--test-root flag, and only to a path beneath /tmp (pytest's tmp_path qualifies).
Never touches the real reports directory.
"""

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


def _env(tmp: Path, **extra):
    e = dict(os.environ)
    e.pop("REPORTS_ROOT_GUARD", None)        # not set by tests unless a case wants it
    e.update({
        "BOT_CONFIGS_DIR": str(tmp / "configs"),
        "BOT_REPORT_RETENTION_DAYS": "15",
    })
    e.update(extra)
    return e


def _setup(tmp: Path, token_line=f"REPORTS_PATH_TOKEN={TOKEN}\n", make_token_dir=True):
    reports = tmp / "reports"
    if make_token_dir:
        (reports / TOKEN).mkdir(parents=True)
    else:
        reports.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "configs"; cfg.mkdir()
    (cfg / "reports-token.env").write_text(token_line)
    return reports / TOKEN


def _run(tmp: Path, *args, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), "--test-root", str(tmp / "reports"), *args],
        env=env or _env(tmp), capture_output=True, text=True)


# ── functional ───────────────────────────────────────────────────────────────

def test_dry_run_changes_nothing(tmp_path):
    rd = _setup(tmp_path)
    _make_report_dir(rd, "old-1", age_days=30)
    before = sorted(p.name for p in rd.iterdir())
    r = _run(tmp_path, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in rd.iterdir()) == before
    assert "would remove" in r.stdout


def test_removes_expired_keeps_fresh(tmp_path):
    rd = _setup(tmp_path)
    _make_report_dir(rd, "fresh-1", age_days=0)
    _make_report_dir(rd, "old-1", age_days=30)
    (rd / "hollow-1").mkdir()
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert {p.name for p in rd.iterdir()} == {"fresh-1", "fresh-1.zip"}
    assert "retained_intact=1" in r.stdout


def test_handles_spaces_in_names(tmp_path):
    rd = _setup(tmp_path)
    _make_report_dir(rd, "old report with spaces", age_days=30)
    _make_report_dir(rd, "fresh one", age_days=0)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert {p.name for p in rd.iterdir()} == {"fresh one", "fresh one.zip"}


# ── Section 8 safety ─────────────────────────────────────────────────────────

def test_missing_token_line(tmp_path):
    _setup(tmp_path, token_line="SOMETHING_ELSE=1\n")
    r = _run(tmp_path)
    assert r.returncode == 2
    assert "line not found" in r.stderr


def test_empty_token(tmp_path):
    _setup(tmp_path, token_line="REPORTS_PATH_TOKEN=\n")
    r = _run(tmp_path)
    assert r.returncode == 2
    assert "is empty" in r.stderr


def test_malformed_token(tmp_path):
    _setup(tmp_path, token_line="REPORTS_PATH_TOKEN=/\n")
    r = _run(tmp_path)
    assert r.returncode == 2
    assert "unexpected format" in r.stderr


def test_nonexistent_target(tmp_path):
    _setup(tmp_path, make_token_dir=False)     # token valid, but no <token> dir
    r = _run(tmp_path)
    assert r.returncode == 2
    assert "token report dir not found" in r.stderr


def test_unsafe_guard_not_under_tmp(tmp_path):
    _setup(tmp_path)
    # --test-root must be beneath /tmp; anything else is rejected outright.
    r = subprocess.run(
        ["bash", str(SCRIPT), "--test-root", "/var/tmp/evil"],
        env=_env(tmp_path), capture_output=True, text=True)
    assert r.returncode == 2
    assert "beneath /tmp" in r.stderr


def test_guard_env_mismatch_rejected(tmp_path):
    _setup(tmp_path)
    # A stray REPORTS_ROOT_GUARD that does not match the (test) guard must abort:
    # config can confirm the scope but never redefine it.
    env = _env(tmp_path, REPORTS_ROOT_GUARD="/opt/shahrzad-devops/reports")
    r = _run(tmp_path, env=env)
    assert r.returncode == 2
    assert "does not match pinned guard" in r.stderr


def test_unsafe_base_outside_guard(tmp_path):
    # WITHOUT --test-root the guard is the hardcoded prod root; a base elsewhere
    # (here under /tmp) is outside it and must be refused before any deletion.
    _setup(tmp_path)
    env = _env(tmp_path, BOT_REPORTS=str(tmp_path / "reports"))
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert r.returncode == 2
    assert "outside guard" in r.stderr


def test_rejects_opt_shahrzad_devops_as_base(tmp_path):
    _setup(tmp_path)
    env = _env(tmp_path, BOT_REPORTS="/opt/shahrzad-devops")
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert r.returncode == 2
    assert "unsafe reports base" in r.stderr
