"""Blocking finding 6 — the deploy transaction is executable, gated, and rolls
back deterministically. Offline: a fake root/tmp filesystem with mocked
git/systemctl; the real script under scripts/deploy-phase1-phase2.sh. Never
touches production.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "deploy-phase1-phase2.sh"
_SHA = "0123456789abcdef0123456789abcdef01234567"   # 40 hex

_ENV_ORIG = (
    "# live env\n"
    "TELEGRAM_BOT_TOKEN=123:SECRET-TOKEN-KEEP\n"
    "OPENROUTER_API_KEY=sk-or-SECRET-KEEP\n"
    "BOT_MAX_SESSIONS=6\n"
    "BOT_DATA_ROOT=/opt/shahrzad-devops\n"
)
_CRON_A = "0 * * * * root find /opt/.../reports -mtime +1 -delete\n"
_CRON_B = "0 0 * * * root find /opt/.../reports/T -mtime +15 -delete\n"
_CRON_UNRELATED = "@daily root /usr/bin/apt-get clean\n"


def _mock_bin(d: Path) -> Path:
    b = d / "bin"; b.mkdir()
    git = b / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do :; done\n'
        'case "$*" in\n'
        '  *"rev-parse HEAD"*) echo "%s" ;;\n'
        '  *"rev-parse origin/main"*) echo "%s" ;;\n'
        '  *fetch*) exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n' % (_SHA, _SHA))
    git.chmod(0o755)
    sysctl = b / "systemctl"
    sysctl.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$DEPLOY_SYSCTL_LOG"\n'
        'case "$*" in *"is-active"*) exit 0 ;; esac\n'
        'exit 0\n')
    sysctl.chmod(0o755)
    return b


@pytest.fixture
def fs(tmp_path):
    live = tmp_path / "live"
    (live / "scripts").mkdir(parents=True)
    (live / "configs").mkdir()
    (live / "reports").mkdir()
    (live / ".env").write_text(_ENV_ORIG)
    cron = tmp_path / "cron"; cron.mkdir()
    (cron / "cleanup-reports").write_text(_CRON_A)
    (cron / "nightwatch-reports-cleanup").write_text(_CRON_B)
    (cron / "unrelated-job").write_text(_CRON_UNRELATED)
    systemd = tmp_path / "systemd"; systemd.mkdir()
    binp = _mock_bin(tmp_path)
    sysctl_log = tmp_path / "systemctl.log"; sysctl_log.write_text("")
    env = dict(os.environ)
    env.update({
        "PATH": f"{binp}:{env['PATH']}",
        "GIT": str(binp / "git"),
        "SYSTEMCTL": str(binp / "systemctl"),
        "DEPLOY_SYSCTL_LOG": str(sysctl_log),
        "DEPLOY_SOURCE": str(_REPO),
        "DEPLOY_CRON_DIR": str(cron),
        "DEPLOY_SYSTEMD_DIR": str(systemd),
        "DEPLOY_BACKUP_ROOT": str(tmp_path / "backups"),
        "DEPLOY_LOCK": str(tmp_path / "deploy.lock"),
        "DEPLOY_REPORT": str(tmp_path / "report.md"),
        "PYTHON": sys.executable,
        "DEPLOY_PYTHON": sys.executable,
    })
    return dict(tmp=tmp_path, live=live, cron=cron, systemd=systemd,
               report=tmp_path / "report.md", sysctl_log=sysctl_log, env=env)


def _run(fs, *args, env_extra=None):
    env = dict(fs["env"])
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(_SCRIPT), "--test-root", str(fs["live"]), *args],
        env=env, capture_output=True, text=True, timeout=120)


def _status(fs):
    return next((l.split(":", 1)[1].strip()
                 for l in fs["report"].read_text().splitlines()
                 if l.startswith("STATUS:")), None)


# ── preflight: no mutation ──────────────────────────────────────────────────
def test_preflight_performs_no_mutation(fs):
    r = _run(fs, "--preflight")
    assert r.returncode == 0, r.stderr
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    # nothing changed
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG
    assert (fs["cron"] / "cleanup-reports").exists()
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()
    assert not (fs["tmp"] / "backups").exists()
    assert fs["sysctl_log"].read_text() == ""       # no restart


def test_execute_requires_correct_gate(fs):
    # wrong PR
    r = _run(fs, "--execute", "--reviewed-pr", "18", "--merged-sha", _SHA)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    # mismatched sha
    r = _run(fs, "--execute", "--reviewed-pr", "19", "--merged-sha", "f" * 40)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()


def test_protected_root_cannot_be_retargeted(fs):
    # --test-root outside /tmp is refused (production root not freely redefinable)
    r = subprocess.run(
        ["bash", str(_SCRIPT), "--preflight", "--test-root", "/var/tmp/evil"],
        env=fs["env"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "under /tmp" in r.stderr


# ── execute: full success ───────────────────────────────────────────────────
def test_execute_success_deploys_and_migrates(fs):
    r = _run(fs, "--execute", "--reviewed-pr", "19", "--merged-sha", _SHA)
    assert r.returncode == 0, r.stderr + r.stdout
    assert _status(fs) == "DEPLOYED"
    sc = fs["live"] / "scripts"
    # Phase-2 artifacts installed
    for f in ("claude-telegram-bot.py", "coordination.py", "resource_footer.py",
              "coord_publish.py", "log_filters.py"):
        assert (sc / f).exists(), f
    assert (sc / "engines" / "openrouter_chat.py").exists()
    assert (sc / "video_module" / "handlers.py").exists()
    assert (sc / "cleanup-reports.sh").exists()
    # systemd units installed
    assert (fs["systemd"] / "reports-cleanup.timer").exists()
    # retention conflicts removed, unrelated cron untouched
    assert not (fs["cron"] / "cleanup-reports").exists()
    assert not (fs["cron"] / "nightwatch-reports-cleanup").exists()
    assert (fs["cron"] / "unrelated-job").read_text() == _CRON_UNRELATED
    assert (fs["live"] / "configs" / "reports-cleanup.env").exists()
    # env: allow-listed key changed, SECRETS preserved byte-for-byte
    env_now = (fs["live"] / ".env").read_text()
    assert "BOT_MAX_SESSIONS=9" in env_now
    assert "TELEGRAM_BOT_TOKEN=123:SECRET-TOKEN-KEEP" in env_now
    assert "OPENROUTER_API_KEY=sk-or-SECRET-KEEP" in env_now
    # exactly one forward restart
    assert fs["sysctl_log"].read_text().count("restart claude-telegram-bot") == 1


# ── execute: rollback at each stage ─────────────────────────────────────────
@pytest.mark.parametrize("stage", [
    "install_artifacts", "migrate_retention", "edit_env",
    "import_smoke", "restart_once", "verify_live",
])
def test_rollback_at_each_stage(fs, stage):
    r = _run(fs, "--execute", "--reviewed-pr", "19", "--merged-sha", _SHA,
             env_extra={"DEPLOY_FAIL_AT": stage})
    assert r.returncode != 0, r.stdout
    assert _status(fs) == "ROLLED_BACK"
    # present-before artifacts restored byte-for-byte
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG
    assert (fs["cron"] / "cleanup-reports").read_text() == _CRON_A
    assert (fs["cron"] / "nightwatch-reports-cleanup").read_text() == _CRON_B
    assert (fs["cron"] / "unrelated-job").read_text() == _CRON_UNRELATED
    # absent-before artifacts removed again
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()
    assert not (fs["live"] / "scripts" / "engines").exists()
    assert not (fs["live"] / "scripts" / "video_module").exists()
    assert not (fs["systemd"] / "reports-cleanup.timer").exists()
    assert not (fs["live"] / "configs" / "reports-cleanup.env").exists()
