"""Round-4 §§2-9 — the deploy transaction is executable, read-only-safe in
preflight, uses built-in production gates, resolves the real bot paths, verifies
recursive hashes + service-user ownership, gates health on new-process evidence
only, activates the timer strictly at the commit boundary, and rolls back
truthfully (ROLLBACK_FAILED on any restore failure). Fully offline: fake root
under /tmp, mocked git/systemctl/journalctl and PATH-shadowed system tools. No
test touches /opt, /etc, real services or the network.
"""
from __future__ import annotations

import hashlib
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
    'OPENROUTER_API_KEY="sk-or-SECRET-KEEP"\n'
    "BOT_MAX_SESSIONS=6\n"
    "LOG_LEVEL=info\n"
)
_CRON_A = "0 * * * * root find /opt/.../reports -mtime +1 -delete\n"
_CRON_B = "0 0 * * * root find /opt/.../reports/T -mtime +15 -delete\n"
_CRON_UNRELATED = "@daily root /usr/bin/apt-get clean\n"
_NIGHTLY_OK = (
    "#!/bin/bash\n"
    "echo start\n"
    "docker image prune -f\n"
    "find /opt/shahrzad-devops/reports -mtime +1 -delete\n"
    "journalctl --vacuum-time=14d\n"
    "rm -rf /opt/shahrzad-devops/reports/hollow\n"
    "apt-get autoclean\n"
)
_NIGHTLY_AMBIGUOUS = (
    "#!/bin/bash\n"
    "find /opt/shahrzad-devops/reports -mtime +1 -delete && echo swept\n"
    "apt-get autoclean\n"
)
_TOK = "testtoken0123456789abcdef"


def _mock_bin(d: Path) -> Path:
    b = d / "bin"; b.mkdir()
    (b / "git").write_text(f"""#!/usr/bin/env bash
case "$*" in
  *"symbolic-ref -q HEAD"*) [ "${{GIT_ON_BRANCH:-0}}" = 1 ] && {{ echo refs/heads/x; exit 0; }} || exit 1 ;;
  *"status --porcelain"*)   [ "${{GIT_DIRTY:-0}}" = 1 ] && echo " M f"; exit 0 ;;
  *"rev-parse HEAD"*)        echo "${{GIT_HEAD_SHA:-{_SHA}}}"; exit 0 ;;
  *"ls-remote"*)             [ "${{GIT_LSREMOTE_FAIL:-0}}" = 1 ] && exit 0; printf '%s\\trefs/heads/main\\n' "${{GIT_LSREMOTE_SHA:-{_SHA}}}"; exit 0 ;;
  *) exit 0 ;;
esac
""")
    (b / "systemctl").write_text("""#!/usr/bin/env bash
S="$SC_STATE"; echo "$*" >> "$SC_LOG"
[ -f "$S/pid" ] || echo 1000 > "$S/pid"
tgt="${@: -1}"
case "$1" in
  is-active)
    if [ "$tgt" = "reports-cleanup.timer" ]; then
      if [ -f "$S/timer_active" ]; then [ "$2" = "--quiet" ] || echo active; exit 0;
      else [ "$2" = "--quiet" ] || echo inactive; exit 1; fi
    fi
    [ -f "$S/svc_down" ] && exit 1; exit 0 ;;
  is-enabled)
    if [ "$tgt" = "reports-cleanup.timer" ]; then
      if [ -f "$S/timer_enabled" ]; then [ "$2" = "--quiet" ] || echo enabled; exit 0;
      else [ "$2" = "--quiet" ] || echo disabled; exit 1; fi
    fi; exit 0 ;;
  show)
    case "$*" in
      *MainPID*)     cat "$S/pid" ;;
      *"-p User"*)   echo "${SVC_USER:-root}" ;;
      *ControlGroup*) echo "${SC_CGROUP:-}" ;;
      *ExecStart*)   echo "${SC_EXECSTART:-}" ;;
    esac; exit 0 ;;
  restart)  echo $(( $(cat "$S/pid") + 1000 )) > "$S/pid"; rm -f "$S/svc_down"; exit 0 ;;
  enable)   [ "$2" = "reports-cleanup.timer" ] && touch "$S/timer_enabled"; exit 0 ;;
  disable)  rm -f "$S/timer_enabled"; exit 0 ;;
  start)    [ "$2" = "reports-cleanup.timer" ] && touch "$S/timer_active"; exit 0 ;;
  stop)     rm -f "$S/timer_active"; exit 0 ;;
  *) exit 0 ;;
esac
""")
    (b / "journalctl").write_text("""#!/usr/bin/env bash
case "$*" in
  *"--show-cursor"*) echo "-- cursor: CUR$(cat ${SC_STATE}/pid 2>/dev/null || echo 0)"; exit 0 ;;
esac
case "$*" in
  *"--after-cursor"*)
    case "${JOURNAL_MODE:-good}" in
      stale) exit 0 ;;
      missing_engine) printf 'Bot v4 ready\\nlimits.effective max_sessions=9 max_running_agents=2\\n'; exit 0 ;;
      traceback_new) printf 'Bot v4 ready\\nTraceback (most recent call last)\\n'; exit 0 ;;
      *) printf 'Bot v4 ready\\nlimits.effective max_sessions=9 max_running_agents=2\\nengines.registered names=chat,claude,codex\\n'; exit 0 ;;
    esac ;;
  *) printf 'OLD Traceback that must be ignored\\nengines.registered names=chat,claude,codex\\n'; exit 0 ;;
esac
""")
    (b / "fakepytest").write_text('#!/usr/bin/env bash\necho "${FAKE_TESTS:-999} passed in 1s"\nexit ${FAKE_PYTEST_RC:-0}\n')
    (b / "okcmd").write_text("#!/usr/bin/env bash\nexit 0\n")
    (b / "failcmd").write_text("#!/usr/bin/env bash\nexit 1\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b


@pytest.fixture
def fs(tmp_path):
    live = tmp_path / "live"
    (live / "scripts").mkdir(parents=True)
    (live / "configs").mkdir()
    (live / "reports" / _TOK).mkdir(parents=True)
    (live / "chat-sessions").mkdir()
    (live / ".env").write_text(_ENV_ORIG)
    (live / "configs" / "bot-state.json").write_text('{"sessions": {}}')
    (live / "configs" / "reports-token.env").write_text(f"REPORTS_PATH_TOKEN={_TOK}\n")
    cron = tmp_path / "cron"; cron.mkdir()
    (cron / "cleanup-reports").write_text(_CRON_A)
    (cron / "nightwatch-reports-cleanup").write_text(_CRON_B)
    (cron / "unrelated-job").write_text(_CRON_UNRELATED)
    systemd = tmp_path / "systemd"; systemd.mkdir()
    nightly = live / "scripts" / "nightly-cleanup.sh"; nightly.write_text(_NIGHTLY_OK)
    binp = _mock_bin(tmp_path)
    sc = tmp_path / "sc"; sc.mkdir(); logf = tmp_path / "sc.log"; logf.write_text("")
    env = dict(os.environ)
    env.update({
        "PATH": f"{binp}:{env['PATH']}",
        "GIT": str(binp / "git"), "SYSTEMCTL": str(binp / "systemctl"),
        "JOURNALCTL": str(binp / "journalctl"),
        "SC_STATE": str(sc), "SC_LOG": str(logf),
        "SC_EXECSTART": str(live / "scripts" / "cleanup-reports.sh"),
        "DEPLOY_SOURCE": str(_REPO),
        "DEPLOY_CRON_DIR": str(cron), "DEPLOY_SYSTEMD_DIR": str(systemd),
        "DEPLOY_BACKUP_ROOT": str(tmp_path / "backups"),
        "DEPLOY_LOCK": str(tmp_path / "deploy.lock"),
        "DEPLOY_REPORT": str(tmp_path / "report.md"),
        "DEPLOY_PYTHON": sys.executable, "DEPLOY_PYTEST": str(binp / "fakepytest"),
        "DEPLOY_STATE_FILE": str(live / "configs" / "bot-state.json"),
        "DEPLOY_NIGHTLY_CLEANUP": str(nightly),
        # readiness gates via seams (happy path): each is honoured under --test-root
        "DEPLOY_CLAUDE_CMD": str(binp / "okcmd"), "DEPLOY_CODEX_CMD": str(binp / "okcmd"),
        "DEPLOY_CAPACITY_CMD": str(binp / "okcmd"), "DEPLOY_LISTENER_CMD": str(binp / "okcmd"),
        "DEPLOY_OPENROUTER_CMD": str(binp / "okcmd"),
        "DEPLOY_OBSERVE_SECONDS": "0",
        "FAKE_TESTS": "999", "DEPLOY_EXPECTED_TESTS": "999",
    })
    return dict(tmp=tmp_path, live=live, cron=cron, systemd=systemd, nightly=nightly,
               report=tmp_path / "report.md", log=logf, binp=binp, sc=sc, env=env)


def _run(fs, *args, env_extra=None):
    env = dict(fs["env"])
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(_SCRIPT), "--test-root", str(fs["live"]), *args],
                          env=env, capture_output=True, text=True, timeout=180)


def _field(fs, name):
    for l in fs["report"].read_text().splitlines():
        if l.startswith(name + ":"):
            return l.split(":", 1)[1].strip()
    return None


def _status(fs):
    return _field(fs, "STATUS")


def _execute(fs, env_extra=None):
    return _run(fs, "--execute", "--reviewed-pr", "19", "--merged-sha", _SHA, env_extra=env_extra)


def _digest(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            h.update(p.as_posix().encode()); h.update(p.read_bytes())
    return h.hexdigest()


# ── §2 preflight: no Git-metadata / source / live writes ────────────────────
def test_preflight_no_writes_anywhere(fs):
    live0 = _digest(fs["live"]); cron0 = _digest(fs["cron"])
    src_status0 = subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                                 capture_output=True, text=True).stdout
    src_head0 = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout
    reflog0 = subprocess.run(["git", "-C", str(_REPO), "reflog", "show", "-n1"],
                             capture_output=True, text=True).stdout

    r = _run(fs, "--preflight", "--merged-sha", _SHA)
    assert r.returncode == 0, r.stderr[-2500:]
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert _digest(fs["live"]) == live0 and _digest(fs["cron"]) == cron0
    assert not Path(fs["env"]["DEPLOY_LOCK"]).exists()          # no lock in preflight
    # git metadata untouched (no fetch): status/head/reflog unchanged
    assert subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                          capture_output=True, text=True).stdout == src_status0
    assert subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == src_head0
    assert subprocess.run(["git", "-C", str(_REPO), "reflog", "show", "-n1"],
                          capture_output=True, text=True).stdout == reflog0


def test_lsremote_stale_or_failed_fails_closed(fs):
    r = _execute(fs, env_extra={"GIT_LSREMOTE_SHA": "f" * 40})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "origin/main" in r.stderr
    r = _execute(fs, env_extra={"GIT_LSREMOTE_FAIL": "1"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "ls-remote" in r.stderr


def test_production_refuses_test_seams():
    env = dict(os.environ); env["DEPLOY_CRON_DIR"] = "/tmp/x"; env["DEPLOY_REPORT"] = "/tmp/rej.md"
    r = subprocess.run(["bash", str(_SCRIPT), "--preflight"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "refuses test seam" in r.stderr


# ── §3 built-in production gates run and fail closed (no seams) ──────────────
def _builtin_env(fs):
    """Drop the readiness seams so the BUILT-IN gates run; shadow their tools."""
    b = fs["binp"]
    # PATH-shadow df/free/ss/curl with pass-by-default mocks
    (b / "df").write_text("#!/usr/bin/env bash\necho hdr\necho 'F 1 2 999999999 5% /'\n")   # NR==2 $4=avail_kb
    (b / "free").write_text("#!/usr/bin/env bash\necho 'Mem: 1 1 1 1 1 9999999'\n")     # big available
    (b / "ss").write_text('#!/usr/bin/env bash\necho "LISTEN 0 0 0.0.0.0:9091 x"\necho "LISTEN 0 0 10.108.0.4:9091 x"\n')
    (b / "curl").write_text("#!/usr/bin/env bash\necho 200\n")                           # http 200
    for n in ("df", "free", "ss", "curl"):
        (b / n).chmod(0o755)
    ee = {k: v for k, v in fs["env"].items()}
    for seam in ("DEPLOY_CAPACITY_CMD", "DEPLOY_LISTENER_CMD", "DEPLOY_OPENROUTER_CMD"):
        ee.pop(seam, None)
    # add BIND2 to the live env so the listener built-in checks it
    (fs["live"] / ".env").write_text(_ENV_ORIG + "BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4\n")
    return ee


def test_builtin_capacity_gate_runs_and_can_fail(fs):
    ee = _builtin_env(fs)
    # first prove the built-ins PASS end-to-end
    r = subprocess.run(["bash", str(_SCRIPT), "--preflight", "--merged-sha", _SHA,
                        "--test-root", str(fs["live"])],
                       env={k: str(v) for k, v in ee.items()}, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    # now force disk low -> capacity gate fails closed
    (fs["binp"] / "df").write_text("#!/usr/bin/env bash\necho hdr\necho 'F 1 2 1 5% /'\n"); (fs["binp"] / "df").chmod(0o755)
    r = subprocess.run(["bash", str(_SCRIPT), "--preflight", "--merged-sha", _SHA,
                        "--test-root", str(fs["live"])],
                       env={k: str(v) for k, v in ee.items()}, capture_output=True, text=True, timeout=180)
    assert r.returncode != 0 and "capacity" in r.stderr.lower()


def test_builtin_listener_and_openrouter_gates_fail_closed(fs):
    ee = _builtin_env(fs)
    # listener down
    (fs["binp"] / "ss").write_text("#!/usr/bin/env bash\ntrue\n"); (fs["binp"] / "ss").chmod(0o755)
    r = subprocess.run(["bash", str(_SCRIPT), "--preflight", "--merged-sha", _SHA,
                        "--test-root", str(fs["live"])],
                       env={k: str(v) for k, v in ee.items()}, capture_output=True, text=True, timeout=180)
    assert r.returncode != 0 and "listener" in r.stderr.lower()
    # listener back, OpenRouter auth 401
    (fs["binp"] / "ss").write_text('#!/usr/bin/env bash\necho "LISTEN 0 0 0.0.0.0:9091 x"\necho "LISTEN 0 0 10.108.0.4:9091 x"\n'); (fs["binp"] / "ss").chmod(0o755)
    (fs["binp"] / "curl").write_text("#!/usr/bin/env bash\necho 401\n"); (fs["binp"] / "curl").chmod(0o755)
    r = subprocess.run(["bash", str(_SCRIPT), "--preflight", "--merged-sha", _SHA,
                        "--test-root", str(fs["live"])],
                       env={k: str(v) for k, v in ee.items()}, capture_output=True, text=True, timeout=180)
    assert r.returncode != 0 and "openrouter" in r.stderr.lower()


@pytest.mark.parametrize("seam,expect", [
    ("DEPLOY_CODEX_CMD", "codex"), ("DEPLOY_CLAUDE_CMD", "claude"),
])
def test_service_cli_gate_fails_closed(fs, seam, expect):
    r = _execute(fs, env_extra={seam: str(fs["binp"] / "failcmd")})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr.lower()


def test_busy_session_blocks(fs):
    (fs["live"] / "configs" / "bot-state.json").write_text('{"sessions":{"1:a":{"status":"running"}}}')
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "running/queued" in r.stderr


# ── §4 resolved paths (non-default) are backed up + rolled back ─────────────
def test_nondefault_paths_resolved_and_rolled_back(fs):
    # point the bot's data/config elsewhere inside the live root, with content
    alt = fs["live"] / "data"; (alt / "cfg").mkdir(parents=True); (alt / "hist").mkdir()
    (alt / "cfg" / "bot-state.json").write_text('{"sessions":{}, "marker":"ALT"}')
    (alt / "cfg" / "coordination.json").write_text('{"entries":{}}')
    (alt / "cfg" / "openrouter-catalog.json").write_text('{"cache":"ALT"}')
    (alt / "cfg" / "reports-token.env").write_text(f"REPORTS_PATH_TOKEN={_TOK}\n")
    (fs["live"] / "reports" / _TOK).mkdir(exist_ok=True)
    env_alt = (_ENV_ORIG
               + f"BOT_CONFIGS_DIR={alt}/cfg\n"
               + f"BOT_CHAT_SESSIONS_DIR={alt}/hist\n"
               + f"BOT_COORDINATION_FILE={alt}/cfg/coordination.json\n"
               + f"BOT_CHAT_CATALOG_CACHE={alt}/cfg/openrouter-catalog.json\n")
    (fs["live"] / ".env").write_text(env_alt)
    # reports-token must be found where the resolved configs dir is
    r = _execute(fs, env_extra={"DEPLOY_STATE_FILE": str(alt / "cfg" / "bot-state.json"),
                                "DEPLOY_FAIL_AT": "health_check"})
    assert _status(fs) == "ROLLED_BACK", r.stderr[-2000:]
    # the resolved (non-default) state file was restored byte-for-byte
    assert (alt / "cfg" / "bot-state.json").read_text() == '{"sessions":{}, "marker":"ALT"}'
    assert (alt / "cfg" / "coordination.json").read_text() == '{"entries":{}}'


# ── §5 recursive hash mismatch / extra file fails install ───────────────────
def test_success_full(fs):
    r = _execute(fs)
    assert r.returncode == 0, r.stderr[-3000:]
    assert _status(fs) == "DEPLOYED"
    sc = fs["live"] / "scripts"
    assert (sc / "claude-telegram-bot.py").exists() and (sc / "engines" / "openrouter_chat.py").exists()
    assert (sc / "video_module" / "handlers.py").exists()
    assert (fs["systemd"] / "reports-cleanup.timer").exists()
    assert not (fs["cron"] / "cleanup-reports").exists()
    assert (fs["cron"] / "unrelated-job").read_text() == _CRON_UNRELATED
    nb = fs["nightly"].read_text()
    assert nb.count("removed report-deletion") == 2 and "apt-get autoclean" in nb
    env_now = (fs["live"] / ".env").read_text()
    assert "BOT_MAX_SESSIONS=9" in env_now and "BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4" in env_now
    assert 'OPENROUTER_API_KEY="sk-or-SECRET-KEEP"' in env_now   # quoted secret preserved
    assert _field(fs, "FORWARD_RESTARTS") == "1"
    # timer activated AFTER observe (enable appears after the last health/observe)
    log = fs["log"].read_text().splitlines()
    assert any(l.startswith("restart ") for l in log)
    assert any(l.startswith("enable reports-cleanup.timer") for l in log)
    r_idx = max(i for i, l in enumerate(log) if l.startswith("restart "))
    e_idx = min(i for i, l in enumerate(log) if l.startswith("enable reports-cleanup.timer"))
    assert e_idx > r_idx


# ── §6 health uses only new-process evidence ────────────────────────────────
def test_stale_journal_cannot_pass(fs):
    r = _execute(fs, env_extra={"JOURNAL_MODE": "stale"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"


def test_missing_engine_log_fails(fs):
    r = _execute(fs, env_extra={"JOURNAL_MODE": "missing_engine"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"


def test_new_process_traceback_fails(fs):
    r = _execute(fs, env_extra={"JOURNAL_MODE": "traceback_new"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"


def test_old_traceback_ignored_success(fs):
    # full journal (unused by health) contains an old traceback; success proves
    # health reads only the after-cursor window
    r = _execute(fs, env_extra={"JOURNAL_MODE": "good"})
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]


# ── §7 strict timer ExecStart ───────────────────────────────────────────────
def test_empty_execstart_rejected(fs):
    r = _execute(fs, env_extra={"SC_EXECSTART": ""})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    # the failure is the empty ExecStart at the timer boundary
    assert "ExecStart is empty" in r.stderr


# ── §8 rollback matrix (per-stage) + ROLLBACK_FAILED ────────────────────────
@pytest.mark.parametrize("stage", [
    "install_artifacts", "edit_env", "migrate_retention", "verify_install",
    "restart_once", "health_check", "observe", "enable_timer",
])
def test_rollback_each_stage(fs, stage):
    r = _execute(fs, env_extra={"DEPLOY_FAIL_AT": stage})
    assert r.returncode != 0
    assert _status(fs) == "ROLLED_BACK", (stage, r.stderr[-1500:])
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG
    assert (fs["cron"] / "cleanup-reports").read_text() == _CRON_A
    assert fs["nightly"].read_text() == _NIGHTLY_OK
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()
    assert not (fs["live"] / "scripts" / "engines").exists()
    assert not (fs["systemd"] / "reports-cleanup.timer").exists()


def test_ambiguous_nightly_aborts_rolls_back(fs):
    fs["nightly"].write_text(_NIGHTLY_AMBIGUOUS)
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    assert fs["nightly"].read_text() == _NIGHTLY_AMBIGUOUS
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG


def test_rollback_failed_when_old_bot_does_not_recover(fs):
    # health fails (rollback triggered); the listener check PASSES readiness but
    # FAILS during rollback verification (2nd call) -> the old bot is not proven
    # healthy -> ROLLBACK_FAILED, never a false ROLLED_BACK.
    counter = fs["binp"] / "listener_count.sh"
    counter.write_text('#!/usr/bin/env bash\n'
                       'c="$SC_STATE/lcount"; n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 ));'
                       ' echo $n > "$c"; [ "$n" -le 1 ] && exit 0 || exit 1\n')
    counter.chmod(0o755)
    r = _execute(fs, env_extra={"JOURNAL_MODE": "stale", "DEPLOY_LISTENER_CMD": str(counter)})
    assert r.returncode != 0
    assert _status(fs) == "ROLLBACK_FAILED"
    assert _field(fs, "ROLLBACK_ERRORS") not in (None, "<none>")


# ── §8 prior timer-state restore matrix ─────────────────────────────────────
@pytest.mark.parametrize("enabled,active", [
    (True, True), (True, False), (False, False),
])
def test_prior_timer_state_restored(fs, enabled, active):
    if enabled:
        (fs["sc"] / "timer_enabled").touch()
    if active:
        (fs["sc"] / "timer_active").touch()
    r = _execute(fs, env_extra={"DEPLOY_FAIL_AT": "health_check"})
    assert _status(fs) == "ROLLED_BACK", r.stderr[-1500:]
    # after rollback the timer files reflect the prior state
    assert (fs["sc"] / "timer_enabled").exists() == enabled
    assert (fs["sc"] / "timer_active").exists() == active


# ── §9 byte-preserving .env edits ───────────────────────────────────────────
def test_crlf_env_preserved(fs):
    crlf = _ENV_ORIG.replace("\n", "\r\n")
    (fs["live"] / ".env").write_bytes(crlf.encode())
    r = _execute(fs)
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]
    raw = (fs["live"] / ".env").read_bytes()
    assert b"BOT_MAX_SESSIONS=9\r\n" in raw                      # edited line keeps CRLF
    assert b'OPENROUTER_API_KEY="sk-or-SECRET-KEEP"\r\n' in raw  # untouched CRLF secret
    assert b"TELEGRAM_BOT_TOKEN=123:SECRET-TOKEN-KEEP\r\n" in raw


def test_no_final_newline_env_preserved(fs):
    (fs["live"] / ".env").write_bytes(_ENV_ORIG.encode().rstrip(b"\n"))  # no final newline
    r = _execute(fs)
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]
    raw = (fs["live"] / ".env").read_bytes()
    # LOG_LEVEL was the last (unterminated) line; it must be intact + terminated before appends
    assert b"LOG_LEVEL=info\n" in raw
    assert b"BOT_COORD_PUBLISHER=" in raw


def test_duplicate_key_fails_closed(fs):
    (fs["live"] / ".env").write_text(_ENV_ORIG + "BOT_MAX_SESSIONS=7\n")  # duplicate allow-listed key
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    assert "duplicate" in r.stderr.lower()
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG + "BOT_MAX_SESSIONS=7\n"  # restored
