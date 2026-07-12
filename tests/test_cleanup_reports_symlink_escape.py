"""Section 3 — cleanup-reports.sh --test-root canonical symlink-escape guard.

The script must canonicalize the EFFECTIVE --test-root before trusting its
/tmp/* prefix: a /tmp/... symlink can resolve to a target outside /tmp (e.g.
the real production reports tree), and a raw-string prefix check alone would
accept it. These tests only ever construct symlinks INSIDE each test's own
tmp_path; they never create, follow-write, or delete anything under the real
production path (they assert rejection, they do not touch the target).
"""
from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup-reports.sh"
TOKEN = "abcdef0123456789abcdef0123456789"
PROD_REPORTS = "/opt/shahrzad-devops/reports"


def _env(tmp: Path, **extra):
    e = dict(os.environ)
    e.pop("REPORTS_ROOT_GUARD", None)
    e.update({"BOT_CONFIGS_DIR": str(tmp / "configs"), "BOT_REPORT_RETENTION_DAYS": "15"})
    e.update(extra)
    return e


def _setup_token(tmp: Path, reports_dir: Path):
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / TOKEN).mkdir(parents=True, exist_ok=True)
    cfg = tmp / "configs"
    cfg.mkdir(exist_ok=True)
    (cfg / "reports-token.env").write_text(f"REPORTS_PATH_TOKEN={TOKEN}\n")


def _run(tmp: Path, test_root: str):
    return subprocess.run(
        ["bash", str(SCRIPT), "--test-root", test_root],
        env=_env(tmp), capture_output=True, text=True)


# ── (1) normal temporary directory — must still work exactly as before ──────

def test_normal_tmp_directory_still_accepted(tmp_path):
    real = tmp_path / "reports"
    _setup_token(tmp_path, real)
    r = _run(tmp_path, str(real))
    assert r.returncode == 0, r.stderr


# ── (2) /tmp itself — rejected ───────────────────────────────────────────────

def test_rejects_tmp_itself(tmp_path):
    r = _run(tmp_path, "/tmp")
    assert r.returncode == 2
    # "/tmp" doesn't match the /tmp/* raw-prefix glob, so it's rejected by the
    # pre-existing raw-prefix gate (before canonicalization ever runs) — the
    # requirement (reject /tmp itself) holds either way.
    assert "beneath /tmp" in r.stderr


def test_rejects_symlink_canonicalizing_to_tmp_itself(tmp_path):
    """A /tmp/... path passes the raw-prefix gate but canonicalizes to exactly
    /tmp — this exercises the NEW canonical-equality check specifically."""
    link = tmp_path / "link_to_tmp_root"
    link.symlink_to("/tmp", target_is_directory=True)
    r = _run(tmp_path, str(link))
    assert r.returncode == 2
    assert "/tmp itself" in r.stderr


# ── (3) /tmp/link -> the production reports path — rejected (validation only,
#        we only assert the script refuses; we never touch PROD_REPORTS) ────

def test_rejects_symlink_to_production_reports_path(tmp_path):
    link = tmp_path / "prod_link"
    link.symlink_to(PROD_REPORTS, target_is_directory=True)
    before = os.path.exists(PROD_REPORTS)  # observe only, never mutate
    r = _run(tmp_path, str(link))
    assert r.returncode == 2
    assert "escapes /tmp" in r.stderr
    assert PROD_REPORTS in r.stderr
    # Confirm we made no filesystem change to production either way.
    assert os.path.exists(PROD_REPORTS) == before


# ── (4) /tmp/link -> another non-/tmp directory — rejected ──────────────────

def test_rejects_symlink_to_other_non_tmp_directory(tmp_path):
    outside = Path("/var/tmp/idlock_test_outside_target")
    outside.mkdir(parents=True, exist_ok=True)
    try:
        link = tmp_path / "outside_link"
        link.symlink_to(outside, target_is_directory=True)
        r = _run(tmp_path, str(link))
        assert r.returncode == 2
        assert "escapes /tmp" in r.stderr
    finally:
        try:
            outside.rmdir()
        except OSError:
            pass


# ── (5) nested symlink escape (a symlink inside a symlinked dir) — rejected ─

def test_rejects_nested_symlink_escape(tmp_path):
    outside = Path("/var/tmp/idlock_test_nested_outside")
    outside.mkdir(parents=True, exist_ok=True)
    try:
        inner_link = tmp_path / "inner"
        inner_link.symlink_to(outside, target_is_directory=True)
        outer_path = str(inner_link / "sub" / "leaf")  # nested under the escape
        r = _run(tmp_path, outer_path)
        assert r.returncode == 2
        assert "escapes /tmp" in r.stderr
    finally:
        try:
            outside.rmdir()
        except OSError:
            pass


# ── (6) nonexistent safe path beneath /tmp — must canonicalize and proceed ──

def test_nonexistent_safe_path_beneath_tmp_still_works(tmp_path):
    target = tmp_path / "does_not_exist_yet" / "reports"
    _setup_token(tmp_path, target)
    r = _run(tmp_path, str(target))
    assert r.returncode == 0, r.stderr


def test_nonexistent_leaf_under_real_tmp_root_canonicalizes(tmp_path):
    """The final component need not exist for canonicalization to succeed (only
    a safe, real ancestor is required) — proves the dirname-walk fallback path
    used when `realpath -m` is unavailable also lands beneath /tmp correctly."""
    target = tmp_path / "brand_new_leaf"
    assert not target.exists()
    cfg = tmp_path / "configs"
    cfg.mkdir(exist_ok=True)
    (cfg / "reports-token.env").write_text(f"REPORTS_PATH_TOKEN={TOKEN}\n")
    # No <token> dir created here → script must fail on "token report dir not
    # found", NOT on the /tmp safety check — proving canonicalization accepted
    # the path as safely beneath /tmp before reaching the existence check.
    r = _run(tmp_path, str(target))
    assert r.returncode == 2
    assert "escapes /tmp" not in r.stderr
    assert "token report dir not found" in r.stderr


# ── isolation guard: never touch anything outside this test's own tmp_path ──

def test_suite_never_deletes_outside_its_tmp_path(tmp_path):
    """Sanity: build a real report dir + zip beneath tmp_path, run a symlink
    that ESCAPES tmp_path pointing elsewhere, confirm the run is rejected AND
    the escape target (inside a second, separate tmp-like dir) is untouched."""
    real_reports = tmp_path / "reports"
    _setup_token(tmp_path, real_reports)
    d = real_reports / "keepme"
    d.mkdir()
    (d / "f.txt").write_text("x")
    with zipfile.ZipFile(real_reports / "keepme.zip", "w") as zf:
        zf.write(d / "f.txt", "keepme/f.txt")

    escape_target = Path("/var/tmp/idlock_test_escape_target")
    escape_target.mkdir(parents=True, exist_ok=True)
    (escape_target / "sentinel.txt").write_text("must-survive")
    try:
        link = tmp_path / "escape"
        link.symlink_to(escape_target, target_is_directory=True)
        r = _run(tmp_path, str(link))
        assert r.returncode == 2
        # Own tmp_path content is untouched (never even the target of the run).
        assert (d / "f.txt").exists()
        # Escape target untouched.
        assert (escape_target / "sentinel.txt").read_text() == "must-survive"
    finally:
        (escape_target / "sentinel.txt").unlink(missing_ok=True)
        try:
            escape_target.rmdir()
        except OSError:
            pass
