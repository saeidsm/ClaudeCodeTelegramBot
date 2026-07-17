"""Round-2 §3 — the deploy transaction is executable, gated, and rolls back.

Fully offline: a fake root under /tmp with mocked git/systemctl/journalctl,
service-user CLIs, pytest and health/listener commands. No test touches /opt,
/etc, real services or the network. The real script lives at
scripts/deploy-phase1-phase2.sh.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "deploy-phase1-phase2.sh"
_SHA = "0123456789abcdef0123456789abcdef01234567"

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
_NIGHTLY_OK = (
    "#!/bin/bash\n"
    "echo start\n"
    "docker image prune -f\n"
    "find /opt/shahrzad-devops/reports -mtime +1 -delete\n"   # report-deletion 1
    "journalctl --vacuum-time=14d\n"
    "rm -rf /opt/shahrzad-devops/reports/hollow\n"            # report-deletion 2
    "apt-get autoclean\n"
)
_NIGHTLY_AMBIGUOUS = (
    "#!/bin/bash\n"
    "echo start\n"
    "find /opt/shahrzad-devops/reports -mtime +1 -delete && echo swept\n"  # entangled
    "apt-get autoclean\n"
)


def _mock_bin(d: Path, sha=_SHA) -> Path:
    b = d / "bin"; b.mkdir()

    (b / "git").write_text(f"""#!/usr/bin/env bash
case "$*" in
  *"symbolic-ref -q HEAD"*) [ "${{GIT_ON_BRANCH:-0}}" = 1 ] && {{ echo refs/heads/x; exit 0; }} || exit 1 ;;
  *"status --porcelain"*)   [ "${{GIT_DIRTY:-0}}" = 1 ] && echo " M f"; exit 0 ;;
  *"rev-parse HEAD"*)        echo "${{GIT_HEAD_SHA:-{sha}}}"; exit 0 ;;
  *"rev-parse origin/main"*) echo "${{GIT_ORIGIN_SHA:-{sha}}}"; exit 0 ;;
  *fetch*)                   [ "${{GIT_FETCH_FAIL:-0}}" = 1 ] && exit 1 || exit 0 ;;
  *) exit 0 ;;
esac
""")
    (b / "systemctl").write_text("""#!/usr/bin/env bash
S="$SC_STATE"; echo "$*" >> "$SC_LOG"
[ -f "$S/pid" ] || echo 1000 > "$S/pid"
tgt="${@: -1}"
case "$1" in
  is-active)
    if [ "$tgt" = "reports-cleanup.timer" ]; then [ -f "$S/timer_active" ] && exit 0 || exit 1; fi
    exit 0 ;;
  is-enabled)
    if [ "$tgt" = "reports-cleanup.timer" ]; then
      if [ -f "$S/timer_enabled" ]; then [ "$2" = "--quiet" ] || echo enabled; exit 0;
      else [ "$2" = "--quiet" ] || echo disabled; exit 1; fi
    fi; exit 0 ;;
  show)
    case "$*" in
      *MainPID*)  cat "$S/pid" ;;
      *"-p User"*) echo "${SVC_USER:-root}" ;;
      *ExecStart*) echo "" ;;
    esac; exit 0 ;;
  restart)  echo $(( $(cat "$S/pid") + 1000 )) > "$S/pid"; exit 0 ;;
  enable)   [ "$2" = "reports-cleanup.timer" ] && touch "$S/timer_enabled"; exit 0 ;;
  disable)  rm -f "$S/timer_enabled"; exit 0 ;;
  start)    [ "$2" = "reports-cleanup.timer" ] && touch "$S/timer_active"; exit 0 ;;
  stop)     rm -f "$S/timer_active"; exit 0 ;;
  *) exit 0 ;;
esac
""")
    (b / "journalctl").write_text("""#!/usr/bin/env bash
cat <<'EOF'
Bot v4 ready
limits.effective max_sessions=9 max_running_agents=2
engines registered: claude, codex, chat
EOF
""")
    (b / "fakepytest").write_text("""#!/usr/bin/env bash
echo "${FAKE_TESTS:-999} passed in 1s"
exit ${FAKE_PYTEST_RC:-0}
""")
    for name in ("okcmd", "failcmd"):
        (b / name).write_text("#!/usr/bin/env bash\n"
                              + ("exit 0\n" if name == "okcmd" else "exit 1\n"))
    for f in b.iterdir():
        f.chmod(0o755)
    return b


@pytest.fixture
def fs(tmp_path):
    live = tmp_path / "live"
    (live / "scripts").mkdir(parents=True)
    (live / "configs").mkdir()
    (live / "reports").mkdir()
    _TOK = "testtoken0123456789abcdef"
    (live / "reports" / _TOK).mkdir()          # token report dir the cleanup expects
    (live / ".env").write_text(_ENV_ORIG)
    (live / "configs" / "bot-state.json").write_text('{"sessions": {}}')
    (live / "configs" / "reports-token.env").write_text(
        "REPORTS_PATH_TOKEN=testtoken0123456789abcdef\n")
    cron = tmp_path / "cron"; cron.mkdir()
    (cron / "cleanup-reports").write_text(_CRON_A)
    (cron / "nightwatch-reports-cleanup").write_text(_CRON_B)
    (cron / "unrelated-job").write_text(_CRON_UNRELATED)
    systemd = tmp_path / "systemd"; systemd.mkdir()
    nightly = live / "scripts" / "nightly-cleanup.sh"
    nightly.write_text(_NIGHTLY_OK)
    binp = _mock_bin(tmp_path)
    sc_state = tmp_path / "sc_state"; sc_state.mkdir()
    logf = tmp_path / "sysctl.log"; logf.write_text("")

    env = dict(os.environ)
    env.update({
        "PATH": f"{binp}:{env['PATH']}",
        "GIT": str(binp / "git"),
        "SYSTEMCTL": str(binp / "systemctl"),
        "JOURNALCTL": str(binp / "journalctl"),
        "SC_STATE": str(sc_state), "SC_LOG": str(logf),
        "DEPLOY_SOURCE": str(_REPO),
        "DEPLOY_CRON_DIR": str(cron),
        "DEPLOY_SYSTEMD_DIR": str(systemd),
        "DEPLOY_BACKUP_ROOT": str(tmp_path / "backups"),
        "DEPLOY_LOCK": str(tmp_path / "deploy.lock"),
        "DEPLOY_REPORT": str(tmp_path / "report.md"),
        "DEPLOY_PYTHON": sys.executable,
        "DEPLOY_PYTEST": str(binp / "fakepytest"),
        "DEPLOY_STATE_FILE": str(live / "configs" / "bot-state.json"),
        "DEPLOY_NIGHTLY_CLEANUP": str(nightly),
        "DEPLOY_CLAUDE_CMD": str(binp / "okcmd"),
        "DEPLOY_CODEX_CMD": str(binp / "okcmd"),
        "DEPLOY_OBSERVE_SECONDS": "0",
        "FAKE_TESTS": "999",
        "DEPLOY_EXPECTED_TESTS": "999",
    })
    return dict(tmp=tmp_path, live=live, cron=cron, systemd=systemd, nightly=nightly,
               report=tmp_path / "report.md", log=logf, binp=binp, env=env)


def _run(fs, *args, env_extra=None):
    env = dict(fs["env"])
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(_SCRIPT), "--test-root", str(fs["live"]), *args],
                          env=env, capture_output=True, text=True, timeout=180)


def _status(fs):
    return next((l.split(":", 1)[1].strip()
                 for l in fs["report"].read_text().splitlines()
                 if l.startswith("STATUS:")), None)


def _execute(fs, env_extra=None):
    return _run(fs, "--execute", "--reviewed-pr", "19", "--merged-sha", _SHA,
                env_extra=env_extra)


# ── preflight: byte-identical, no caches, no lock ───────────────────────────
def test_preflight_no_mutation_no_source_caches(fs):
    import hashlib
    def digest(root):
        h = hashlib.sha256()
        for p in sorted(Path(root).rglob("*")):
            if p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()
    live_before = digest(fs["live"]); cron_before = digest(fs["cron"])
    src_before = subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                                capture_output=True, text=True).stdout

    r = _run(fs, "--preflight", "--merged-sha", _SHA)
    assert r.returncode == 0, r.stderr[-2000:]
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert digest(fs["live"]) == live_before        # live root untouched
    assert digest(fs["cron"]) == cron_before
    # source worktree unchanged, no __pycache__/.pytest_cache written into it
    src_after = subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
    assert src_after == src_before
    assert not (fs["live"] / ".deploy.lock").exists()      # no lock in preflight
    assert not (Path(fs["env"]["DEPLOY_LOCK"])).exists()


# ── production refuses test seams ───────────────────────────────────────────
def test_production_mode_refuses_test_seams():
    # no --test-root but a seam set -> NO-GO before anything
    env = dict(os.environ); env["DEPLOY_CRON_DIR"] = "/tmp/x"
    env["DEPLOY_REPORT"] = "/tmp/rej.md"
    r = subprocess.run(["bash", str(_SCRIPT), "--preflight"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "refuses test seam" in r.stderr


# ── source gate: detached/clean/sha/fetch ───────────────────────────────────
@pytest.mark.parametrize("bad,expect", [
    ({"GIT_ON_BRANCH": "1"}, "detached"),
    ({"GIT_DIRTY": "1"}, "staged/unstaged/untracked"),
    ({"GIT_HEAD_SHA": "f" * 40}, "HEAD"),
    ({"GIT_ORIGIN_SHA": "f" * 40}, "origin/main"),
    ({"GIT_FETCH_FAIL": "1"}, "fetch"),
])
def test_source_gate_fails_closed(fs, bad, expect):
    r = _execute(fs, env_extra=bad)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr
    # no mutation happened
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()


# ── readiness gates fail closed before mutation ─────────────────────────────
@pytest.mark.parametrize("env_extra,expect", [
    ({"DEPLOY_CODEX_CMD": "FAILCMD"}, "Codex"),
    ({"DEPLOY_CAPACITY_CMD": "FAILCMD"}, "capacity"),
    ({"DEPLOY_LISTENER_CMD": "FAILCMD"}, "listener"),
])
def test_readiness_gates_fail_closed(fs, env_extra, expect):
    ee = {k: (str(fs["binp"] / "failcmd") if v == "FAILCMD" else v)
          for k, v in env_extra.items()}
    r = _execute(fs, env_extra=ee)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect.lower() in r.stderr.lower()
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()


def test_busy_session_blocks(fs):
    (fs["live"] / "configs" / "bot-state.json").write_text(
        '{"sessions": {"1:a": {"status": "running"}}}')
    r = _execute(fs)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "running/queued" in r.stderr


def test_missing_openrouter_key_blocks(fs):
    (fs["live"] / ".env").write_text("BOT_MAX_SESSIONS=6\nOPENROUTER_API_KEY=\n")
    r = _execute(fs)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"


# ── full success ────────────────────────────────────────────────────────────
def test_execute_success(fs):
    r = _execute(fs)
    assert r.returncode == 0, r.stderr[-3000:]
    assert _status(fs) == "DEPLOYED"
    sc = fs["live"] / "scripts"
    for f in ("claude-telegram-bot.py", "coordination.py", "resource_footer.py",
              "coord_publish.py", "log_filters.py", "cleanup-reports.sh"):
        assert (sc / f).exists(), f
    assert (sc / "engines" / "openrouter_chat.py").exists()
    assert (sc / "video_module" / "handlers.py").exists()
    assert (fs["systemd"] / "reports-cleanup.timer").exists()
    # all THREE retention conflicts migrated
    assert not (fs["cron"] / "cleanup-reports").exists()
    assert not (fs["cron"] / "nightwatch-reports-cleanup").exists()
    assert (fs["cron"] / "unrelated-job").read_text() == _CRON_UNRELATED
    nb = fs["nightly"].read_text()
    assert "# [phase1-2 retention migration: removed report-deletion] find /opt/shahrzad-devops/reports -mtime +1 -delete" in nb
    assert "# [phase1-2 retention migration: removed report-deletion] rm -rf /opt/shahrzad-devops/reports/hollow" in nb
    assert "docker image prune -f" in nb and "apt-get autoclean" in nb   # unrelated kept
    # no active report-deletion command remains
    active = [l for l in nb.splitlines()
              if "reports" in l and ("-delete" in l or "rm -rf" in l) and not l.lstrip().startswith("#")]
    assert active == []
    # env: 9/2/BIND2, secrets preserved
    env_now = (fs["live"] / ".env").read_text()
    assert "BOT_MAX_SESSIONS=9" in env_now
    assert "BOT_MAX_RUNNING_AGENTS=2" in env_now
    assert "BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4" in env_now
    assert "TELEGRAM_BOT_TOKEN=123:SECRET-TOKEN-KEEP" in env_now
    assert "OPENROUTER_API_KEY=sk-or-SECRET-KEEP" in env_now
    # exactly one forward restart
    fwd = next(l for l in fs["report"].read_text().splitlines()
               if l.startswith("FORWARD_RESTARTS:"))
    assert fwd.strip().endswith("1")
    # timer enabled+active AFTER health (enable appears after restart in the log)
    log = fs["log"].read_text().splitlines()
    r_idx = next(i for i, l in enumerate(log) if l.startswith("restart "))
    e_idx = next(i for i, l in enumerate(log) if l.startswith("enable reports-cleanup.timer"))
    assert e_idx > r_idx


def test_ambiguous_nightly_aborts_and_rolls_back(fs):
    fs["nightly"].write_text(_NIGHTLY_AMBIGUOUS)
    r = _execute(fs)
    assert r.returncode != 0
    assert _status(fs) == "ROLLED_BACK"
    assert "ambiguous" in r.stderr.lower()
    # nightly restored byte-for-byte; env restored
    assert fs["nightly"].read_text() == _NIGHTLY_AMBIGUOUS
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG


# ── rollback at each mutation/verification stage ────────────────────────────
@pytest.mark.parametrize("stage", [
    "install_artifacts", "edit_env", "migrate_retention", "verify_install",
    "restart_once", "health_check", "enable_timer", "observe",
])
def test_rollback_each_stage_restores(fs, stage):
    r = _execute(fs, env_extra={"DEPLOY_FAIL_AT": stage})
    assert r.returncode != 0
    assert _status(fs) == "ROLLED_BACK"
    # present-before restored byte-for-byte
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG
    assert (fs["cron"] / "cleanup-reports").read_text() == _CRON_A
    assert (fs["cron"] / "nightwatch-reports-cleanup").read_text() == _CRON_B
    assert fs["nightly"].read_text() == _NIGHTLY_OK
    assert (fs["cron"] / "unrelated-job").read_text() == _CRON_UNRELATED
    # absent-before removed again
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()
    assert not (fs["live"] / "scripts" / "engines").exists()
    assert not (fs["live"] / "scripts" / "video_module").exists()
    assert not (fs["systemd"] / "reports-cleanup.timer").exists()
    assert not (fs["live"] / "configs" / "reports-cleanup.env").exists()


def test_rollback_restores_startup_mutated_state(fs):
    # bot-state.json present before with content -> a post-mutation failure must
    # restore it byte-for-byte (startup may have migrated/saved it).
    orig_state = '{"sessions": {}, "marker": "ORIGINAL"}'
    (fs["live"] / "configs" / "bot-state.json").write_text(orig_state)
    r = _execute(fs, env_extra={"DEPLOY_FAIL_AT": "health_check"})
    assert _status(fs) == "ROLLED_BACK"
    assert (fs["live"] / "configs" / "bot-state.json").read_text() == orig_state
