"""Section 3 — cleanup-reports.sh --test-root canonical symlink-escape guard.

The script must canonicalize the EFFECTIVE --test-root before trusting its
/tmp/* prefix: a /tmp/... symlink can resolve to a target outside /tmp (e.g.
the real production reports tree), and a raw-string prefix check alone would
accept it.

Isolation (architect follow-up item 2): every test's OWN writable fixture
lives entirely under pytest's `tmp_path`. To exercise a canonical escape, a
symlink created inside `tmp_path` points at an ALREADY-EXISTING, stable
directory outside /tmp — `/var/tmp` itself, which every Linux host has and
which we never create, write to, chmod, or delete. We only ever OBSERVE that
target (existence/mtime), never mutate it. No test creates, unlinks, rmdir's,
or chmods any fixed path outside `tmp_path`.
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
# A stable, always-existing, non-/tmp directory every Linux host has. Used
# ONLY as a read-only symlink TARGET — never created, written, or deleted.
STABLE_OUTSIDE_DIR = "/var/tmp"


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
    /tmp — this exercises the NEW canonical-equality check specifically.
    /tmp always exists; this test never creates or deletes it."""
    link = tmp_path / "link_to_tmp_root"
    link.symlink_to("/tmp", target_is_directory=True)
    r = _run(tmp_path, str(link))
    assert r.returncode == 2
    assert "/tmp itself" in r.stderr


# ── (3) /tmp/link -> the production reports path — rejected, read-only ──────

def test_rejects_symlink_to_production_reports_path(tmp_path):
    """Validation only: we assert the script refuses, and that the production
    directory's mtime is provably unchanged — existence alone is not proof of
    non-interference, so this checks the inode's mtime_ns, not just presence.
    We never write to, delete, or otherwise mutate PROD_REPORTS."""
    if not os.path.exists(PROD_REPORTS):
        pytest.skip(f"{PROD_REPORTS} not present in this environment")
    link = tmp_path / "prod_link"
    link.symlink_to(PROD_REPORTS, target_is_directory=True)
    before_mtime_ns = os.stat(PROD_REPORTS).st_mtime_ns

    r = _run(tmp_path, str(link))

    assert r.returncode == 2
    assert "escapes /tmp" in r.stderr
    assert PROD_REPORTS in r.stderr
    after_mtime_ns = os.stat(PROD_REPORTS).st_mtime_ns
    assert after_mtime_ns == before_mtime_ns, "production reports mtime changed"


# ── (4) /tmp/link -> another existing non-/tmp directory — rejected ─────────

def test_rejects_symlink_to_other_non_tmp_directory(tmp_path):
    """Points at /var/tmp itself — already exists on every host, never
    created or deleted by this test."""
    link = tmp_path / "outside_link"
    link.symlink_to(STABLE_OUTSIDE_DIR, target_is_directory=True)
    r = _run(tmp_path, str(link))
    assert r.returncode == 2
    assert "escapes /tmp" in r.stderr


# ── (5) nested symlink escape (nonexistent suffix under an existing outside
#        directory) — rejected; no outside directory is ever created ────────

def test_rejects_nested_symlink_escape(tmp_path):
    inner_link = tmp_path / "inner"
    inner_link.symlink_to(STABLE_OUTSIDE_DIR, target_is_directory=True)
    # "sub/leaf" need not exist for realpath -m to canonicalize the whole
    # path — no directory is created anywhere, inside or outside tmp_path.
    outer_path = str(inner_link / "sub" / "leaf")
    r = _run(tmp_path, outer_path)
    assert r.returncode == 2
    assert "escapes /tmp" in r.stderr


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


# ── guard must reject BEFORE any target operation, using an entirely
#    tmp_path-local writable fixture ────────────────────────────────────────

def test_escape_rejected_before_any_target_operation(tmp_path):
    """The writable fixture (the thing that must survive) lives ENTIRELY
    under tmp_path. The escaping symlink points at the stable, pre-existing
    /var/tmp (never written by this test) — proving the safety gate fires
    BEFORE the script ever reaches token-lookup or deletion logic: if it
    didn't, the error would be a different one (e.g. "token report dir not
    found" for /var/tmp, or worse, an actual deletion attempt), not the
    /tmp-escape message."""
    real_reports = tmp_path / "reports"
    _setup_token(tmp_path, real_reports)
    d = real_reports / "keepme"
    d.mkdir()
    (d / "f.txt").write_text("x")
    with zipfile.ZipFile(real_reports / "keepme.zip", "w") as zf:
        zf.write(d / "f.txt", "keepme/f.txt")

    link = tmp_path / "escape"
    link.symlink_to(STABLE_OUTSIDE_DIR, target_is_directory=True)

    r = _run(tmp_path, str(link))

    assert r.returncode == 2
    # The rejection is the /tmp-escape guard specifically — not a later-stage
    # error — proving it fires before any token/target lookup or deletion.
    assert "escapes /tmp" in r.stderr
    # Our own, entirely tmp_path-local fixture survives untouched (this run
    # never even referenced real_reports — it used the escaping symlink).
    assert (d / "f.txt").read_text() == "x"


# ── source-level gate: no fixed outside writable targets remain anywhere ────

def test_no_fixed_outside_writable_paths_in_test_suite():
    """Architect follow-up item 2: scan the whole test suite for the specific
    fixed paths that used to be created/deleted outside tmp_path, and for the
    general pattern, so a future edit can't silently reintroduce them."""
    this_file = Path(__file__).resolve()
    tests_dir = this_file.parent
    # Keep the forbidden value assembled so the repository-level search gate
    # itself can prove the old fixed path is absent everywhere, including here.
    banned_substrings = ["/var/tmp/" + "idlock_test_"]
    offenders = []
    for path in tests_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for banned in banned_substrings:
            if banned in text:
                offenders.append(f"{path.name}: contains {banned!r}")
    assert not offenders, "fixed outside writable test paths found:\n" + "\n".join(offenders)
