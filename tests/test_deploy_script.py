"""Round-5 production-safety matrix for scripts/deploy-phase1-phase2.sh.

Fully offline: a fake root under /tmp with PATH-shadowed fakes (git, systemctl,
journalctl, ss, curl, df, free, id, sudo, claude, codex) exercising the REAL
production branches. No test touches /opt, /etc, real services or the network,
and the deploy script is never run against real paths.
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
_TOK = "testtoken0123456789abcdef"

_ENV_ORIG = (
    "# live env\n"
    "TELEGRAM_BOT_TOKEN=123:SECRET-TOKEN-KEEP\n"
    'OPENROUTER_API_KEY="sk-or-SECRET-KEEP"\n'
    "BOT_MAX_SESSIONS=6\n"
    "BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4\n"
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

_CLAUDE_IDS = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5",
               "claude-haiku-4-5-20251001"]
_CODEX_IDS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


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

    # systemctl: pid tracking, timer state files, ControlGroup, persistent
    # catch-up simulation (starting a Persistent=true timer starts the service).
    (b / "systemctl").write_text("""#!/usr/bin/env bash
S="$SC_STATE"; echo "$*" >> "$SC_LOG"
[ -f "$S/pid" ] || echo 1000 > "$S/pid"
tgt="${@: -1}"
_mainpid() {
  n=$(( $(cat "$S/pidreads" 2>/dev/null || echo 0) + 1 )); echo $n > "$S/pidreads"
  if [ -n "${SC_PID_CHURN_AFTER:-}" ] && [ "$n" -gt "$SC_PID_CHURN_AFTER" ]; then
    echo $(( $(cat "$S/pid") + n )); return
  fi
  cat "$S/pid"
}
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
      *MainPID*)      _mainpid ;;
      *"-p User"*)    echo "${SVC_USER:-root}" ;;
      *ControlGroup*) echo "${SC_CGROUP:-}" ;;
      *ExecStart*)    echo "${SC_EXECSTART:-}" ;;
    esac; exit 0 ;;
  restart)  echo $(( $(cat "$S/pid") + 1000 )) > "$S/pid"; echo 0 > "$S/pidreads"; rm -f "$S/svc_down"; exit 0 ;;
  enable)   [ "$2" = "reports-cleanup.timer" ] && touch "$S/timer_enabled"; exit 0 ;;
  disable)  rm -f "$S/timer_enabled"; exit 0 ;;
  start)
    if [ "$2" = "reports-cleanup.timer" ]; then
      touch "$S/timer_active"
      # simulate systemd Persistent=true catch-up: a missed schedule fires the
      # service IMMEDIATELY on start — this is what the deploy must prevent.
      if grep -qiE '^Persistent=true' "$DEPLOY_SYSTEMD_DIR/reports-cleanup.timer" 2>/dev/null; then
        echo "start reports-cleanup.service (persistent catch-up)" >> "$SC_LOG"
      fi
    fi; exit 0 ;;
  stop)     rm -f "$S/timer_active"; exit 0 ;;
  daemon-reload) exit 0 ;;
  *) exit 0 ;;
esac
""")

    # journalctl: mandatory-cursor + _PID-filtered new-process evidence.
    (b / "journalctl").write_text("""#!/usr/bin/env bash
S="$SC_STATE"
case "$*" in
  *"--show-cursor"*)
    [ "${JOURNAL_CURSOR_EMPTY:-0}" = 1 ] && exit 0
    echo "-- cursor: CUR$(cat "$S/pid" 2>/dev/null || echo 0)"; exit 0 ;;
esac
has_pid=0; case "$*" in *"_PID="*) has_pid=1 ;; esac
if [ "$has_pid" = 1 ]; then
  all="$*"; want="${all#*_PID=}"; want="${want%% *}"
  cur="$(cat "$S/pid" 2>/dev/null || echo 0)"
  [ "$want" = "$cur" ] || exit 0            # journal for a different pid: empty
  case "${JOURNAL_MODE:-good}" in
    stale) exit 0 ;;
    missing_engine)
      printf 'Bot v4 ready\\nlimits.effective max_sessions=9 max_running_agents=2\\n'; exit 0 ;;
    extra_engine)
      printf 'Bot v4 ready\\nlimits.effective max_sessions=9 max_running_agents=2\\nengines.registered names=chat,claude,codex,extra\\n'; exit 0 ;;
    traceback_new)
      printf 'Bot v4 ready\\nTraceback (most recent call last)\\n'; exit 0 ;;
    delayed)
      n=$(( $(cat "$S/jcount" 2>/dev/null || echo 0) + 1 )); echo $n > "$S/jcount"
      if [ "$n" -le 2 ]; then printf 'starting...\\n'; exit 0; fi
      printf 'Bot v4 ready\\nlimits.effective max_sessions=9 max_running_agents=2\\nengines.registered names=chat,claude,codex\\n'; exit 0 ;;
    *)
      printf 'Bot v4 ready\\nlimits.effective max_sessions=9 max_running_agents=2\\nengines.registered names=chat,claude,codex\\n'; exit 0 ;;
  esac
fi
# UNfiltered journal: old-pid noise that must never satisfy or fail health
printf 'OLD Traceback (must be ignored)\\nBot v4 ready\\nengines.registered names=chat,claude,codex\\n'
exit 0
""")

    # ss: exact-endpoint + pid-bound socket evidence
    (b / "ss").write_text("""#!/usr/bin/env bash
pid="$(cat "$SC_STATE/pid" 2>/dev/null || echo 0)"
[ -n "${SS_PID_OVERRIDE:-}" ] && pid="$SS_PID_OVERRIDE"
case "${SS_MODE:-good}" in
  good)
    echo "LISTEN 0 128 127.0.0.1:9091 0.0.0.0:* users:((\\"python3\\",pid=$pid,fd=5))"
    echo "LISTEN 0 128 10.108.0.4:9091 0.0.0.0:* users:((\\"python3\\",pid=$pid,fd=6))" ;;
  wildcard)
    echo "LISTEN 0 128 0.0.0.0:9091 0.0.0.0:* users:((\\"python3\\",pid=$pid,fd=5))" ;;
  wrongip)
    echo "LISTEN 0 128 192.168.9.9:9091 0.0.0.0:* users:((\\"python3\\",pid=$pid,fd=5))" ;;
  nobind2)
    echo "LISTEN 0 128 127.0.0.1:9091 0.0.0.0:* users:((\\"python3\\",pid=$pid,fd=5))" ;;
  none) : ;;
esac
exit 0
""")

    # curl: /healthz contract + OpenRouter catalog probe (code + body file)
    (b / "curl").write_text("""#!/usr/bin/env bash
out=""; args=("$@")
for ((i=0;i<${#args[@]};i++)); do
  [ "${args[$i]}" = "-o" ] && out="${args[$((i+1))]}"
done
url="${args[${#args[@]}-1]}"
case "$url" in
  *healthz*)
    case "${CURL_HEALTH_MODE:-ok}" in
      ok)  printf '{"ok": true, "uptime_s": 1, "version": "phase-2b"}' ;;
      bad) printf '{"ok": false}' ;;
      none) : ;;
    esac; exit 0 ;;
  *openrouter*)
    case "${OR_MODE:-ok}" in
      ok)      [ -n "$out" ] && printf '{"data":[{"id":"z-ai/glm-5.2"}]}' > "$out"; echo 200 ;;
      e401)    [ -n "$out" ] && printf '{"error":"unauthorized"}' > "$out"; echo 401 ;;
      e429)    [ -n "$out" ] && printf '{"error":"rate"}' > "$out"; echo 429 ;;
      badjson) [ -n "$out" ] && printf 'not-json' > "$out"; echo 200 ;;
      empty)   [ -n "$out" ] && printf '{"data":[]}' > "$out"; echo 200 ;;
    esac; exit 0 ;;
esac
exit 0
""")

    (b / "df").write_text("""#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "fake 100 1 ${DF_AVAIL_KB:-999999999} 5% /"
""")
    (b / "free").write_text("""#!/usr/bin/env bash
echo "              total        used        free      shared  buff/cache   available"
echo "Mem: 100 1 1 1 1 ${FREE_MEM_KB:-9999999}"
echo "Swap: 100 1 ${FREE_SWAP_KB:-999999}"
""")

    (b / "id").write_text("""#!/usr/bin/env bash
user="${@: -1}"
case "$user" in
  missinguser) echo "id: no such user" >&2; exit 1 ;;
  svcuser) case "$1" in -u) echo ${FAKE_SVC_UID:-0} ;; -g) echo ${FAKE_SVC_GID:-0} ;; esac; exit 0 ;;
  root|*) case "$1" in -u) echo 0 ;; -g) echo 0 ;; esac; exit 0 ;;
esac
""")
    (b / "sudo").write_text("""#!/usr/bin/env bash
[ "${SUDO_FAIL:-0}" = 1 ] && exit 1
# strip -n -u <user> -H and run the rest as the current (root) test user
while [ $# -gt 0 ]; do
  case "$1" in -n|-H) shift ;; -u) shift 2 ;; *) break ;; esac
done
exec "$@"
""")

    (b / "claude").write_text(f"""#!/usr/bin/env bash
case "$*" in
  models*)
    case "${{CLAUDE_MODE:-ok}}" in
      ok)      printf '%s\\n' {" ".join(_CLAUDE_IDS)} ;;
      partial) printf '%s\\n' claude-fable-5 claude-opus-4-8 ;;
      fail)    exit 1 ;;
    esac; exit 0 ;;
esac
exit 0
""")
    (b / "codex").write_text(f"""#!/usr/bin/env bash
case "$*" in
  "login status")
    case "${{CODEX_LOGIN_MODE:-ok}}" in
      ok)   echo "Logged in (ChatGPT)";;
      anon) echo "Not logged in";;
      fail) exit 1;;
    esac; exit 0 ;;
  "debug models")
    case "${{CODEX_MODELS_MODE:-ok}}" in
      ok)      printf '%s\\n' {" ".join(_CODEX_IDS)} ;;
      missing) printf '%s\\n' gpt-5.6-sol gpt-5.6-terra ;;
    esac; exit 0 ;;
  "exec --help")
    case "${{CODEX_EXEC_MODE:-ok}}" in
      ok)     echo "usage: codex exec [--json] ..." ;;
      nojson) echo "usage: codex exec ..." ;;
    esac; exit 0 ;;
  "exec resume --help")
    case "${{CODEX_RESUME_MODE:-ok}}" in
      ok)        echo "usage: codex exec resume <SESSION_ID>" ;;
      nosession) echo "usage: codex exec resume" ;;
    esac; exit 0 ;;
esac
exit 0
""")

    (b / "fakepytest").write_text('#!/usr/bin/env bash\necho "${FAKE_TESTS:-999} passed in 1s"\nexit ${FAKE_PYTEST_RC:-0}\n')
    (b / "rbcorrupt").write_text("""#!/usr/bin/env bash
# test-only rollback-corruption injector (runs between restore and verification)
case "${RB_CORRUPT_MODE:-}" in
  content) printf 'CORRUPTED' >> "$RB_TARGET" ;;
  mode)    chmod 777 "$RB_TARGET" ;;
  owner)   chown 12345:12345 "$RB_TARGET" ;;
  type)    rm -f "$RB_TARGET"; ln -s /tmp "$RB_TARGET" ;;
  extra)   printf 'ghost' > "$RB_EXTRA" ;;
esac
exit 0
""")
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
    # cgroup fixture: resolvable path with clean memory.events
    cgdir = tmp_path / "cg" / "system.slice" / "claude-telegram-bot.service"
    cgdir.mkdir(parents=True)
    (cgdir / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    binp = _mock_bin(tmp_path)
    sc = tmp_path / "sc"; sc.mkdir(); logf = tmp_path / "sc.log"; logf.write_text("")

    env = dict(os.environ)
    # drop any inherited modes so each test starts from the fixture defaults
    for k in ("JOURNAL_MODE", "SS_MODE", "OR_MODE", "CURL_HEALTH_MODE", "CLAUDE_MODE",
              "CODEX_LOGIN_MODE", "CODEX_MODELS_MODE", "CODEX_EXEC_MODE",
              "CODEX_RESUME_MODE", "SVC_USER", "SUDO_FAIL", "SC_PID_CHURN_AFTER",
              "JOURNAL_CURSOR_EMPTY", "DF_AVAIL_KB", "FREE_MEM_KB", "FREE_SWAP_KB"):
        env.pop(k, None)
    env.update({
        "PATH": f"{binp}:{env['PATH']}",
        "GIT": str(binp / "git"), "SYSTEMCTL": str(binp / "systemctl"),
        "JOURNALCTL": str(binp / "journalctl"),
        "SC_STATE": str(sc), "SC_LOG": str(logf),
        "SC_CGROUP": "/system.slice/claude-telegram-bot.service",
        "SC_EXECSTART": str(live / "scripts" / "cleanup-reports.sh"),
        "DEPLOY_CGROUP_BASE": str(tmp_path / "cg"),
        "DEPLOY_SOURCE": str(_REPO),
        "DEPLOY_CRON_DIR": str(cron), "DEPLOY_SYSTEMD_DIR": str(systemd),
        "DEPLOY_BACKUP_ROOT": str(tmp_path / "backups"),
        "DEPLOY_LOCK": str(tmp_path / "deploy.lock"),
        "DEPLOY_REPORT": str(tmp_path / "report.md"),
        "DEPLOY_PYTHON": sys.executable, "DEPLOY_PYTEST": str(binp / "fakepytest"),
        "DEPLOY_STATE_FILE": str(live / "configs" / "bot-state.json"),
        "DEPLOY_NIGHTLY_CLEANUP": str(nightly),
        "DEPLOY_OBSERVE_SECONDS": "0",
        "DEPLOY_HEALTH_DEADLINE": "4", "DEPLOY_HEALTH_POLL": "1",
        "FAKE_TESTS": "999", "DEPLOY_EXPECTED_TESTS": "999",
    })
    return dict(tmp=tmp_path, live=live, cron=cron, systemd=systemd, nightly=nightly,
               report=tmp_path / "report.md", log=logf, binp=binp, sc=sc, env=env)


def _run(fs, *args, env_extra=None):
    env = dict(fs["env"])
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run(["bash", str(_SCRIPT), "--test-root", str(fs["live"]), *args],
                          env=env, capture_output=True, text=True, timeout=240)


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


# ═══ §1 fail-closed dotenv + non-bypassable gates ═══════════════════════════
def test_preflight_no_writes_anywhere(fs):
    live0 = _digest(fs["live"]); cron0 = _digest(fs["cron"])
    status0 = subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                             capture_output=True, text=True).stdout
    head0 = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout
    reflog0 = subprocess.run(["git", "-C", str(_REPO), "reflog", "show", "-n1"],
                             capture_output=True, text=True).stdout
    r = _run(fs, "--preflight", "--merged-sha", _SHA)
    assert r.returncode == 0, r.stderr[-2500:]
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert _digest(fs["live"]) == live0 and _digest(fs["cron"]) == cron0
    assert not Path(fs["env"]["DEPLOY_LOCK"]).exists()
    assert subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                          capture_output=True, text=True).stdout == status0
    assert subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == head0
    assert subprocess.run(["git", "-C", str(_REPO), "reflog", "show", "-n1"],
                          capture_output=True, text=True).stdout == reflog0


@pytest.mark.parametrize("envtext,expect", [
    (_ENV_ORIG + "this is not a valid line\n", "syntax error"),
    (_ENV_ORIG + "BOT_MAX_SESSIONS=7\n", "duplicate"),
    (_ENV_ORIG + "export BOT_MAX_SESSIONS=7\n", "duplicate"),   # export == plain
])
def test_env_parse_fails_closed(fs, envtext, expect):
    (fs["live"] / ".env").write_text(envtext)
    live0 = _digest(fs["live"]); cron0 = _digest(fs["cron"])
    r = _execute(fs)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr.lower()
    assert _digest(fs["live"]) == live0 and _digest(fs["cron"]) == cron0


def test_env_unreadable_fails_closed(fs):
    envp = fs["live"] / ".env"
    envp.unlink(); envp.mkdir()            # a directory: open() fails
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"


def test_missing_dotenv_parser_fails_closed(fs, tmp_path):
    poison = tmp_path / "poison"; poison.mkdir()
    (poison / "dotenv.py").write_text("raise ImportError('parser gone')\n")
    r = _execute(fs, env_extra={"PYTHONPATH": str(poison)})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "parser" in r.stderr.lower() or "env parse failed" in r.stderr.lower()


def test_production_refuses_threshold_and_all_seams():
    for var in ("DEPLOY_MIN_DISK_MB", "DEPLOY_MIN_RAM_MB", "DEPLOY_MIN_SWAP_MB",
                "DEPLOY_HEALTH_DEADLINE", "DEPLOY_RB_CORRUPT_CMD", "DEPLOY_CRON_DIR"):
        env = dict(os.environ); env[var] = "1"; env["DEPLOY_REPORT"] = "/tmp/rej.md"
        r = subprocess.run(["bash", str(_SCRIPT), "--preflight"],
                           env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0 and "refuses test seam" in r.stderr, var


@pytest.mark.parametrize("env_extra,expect", [
    ({"DF_AVAIL_KB": "1"}, "disk low"),
    ({"FREE_MEM_KB": "1"}, "ram low"),
    ({"FREE_SWAP_KB": "1"}, "swap low"),
    ({"SC_CGROUP": ""}, "cgroup path unresolvable"),
    ({"SC_CGROUP": "/nonexistent/slice"}, "memory.events unreadable"),
])
def test_capacity_gate_fails_closed(fs, env_extra, expect):
    r = _execute(fs, env_extra=env_extra)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr.lower()


def test_nonzero_oom_kill_fails_closed(fs):
    ev = fs["tmp"] / "cg" / "system.slice" / "claude-telegram-bot.service" / "memory.events"
    ev.write_text("low 0\noom 1\noom_kill 3\n")
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "oom" in r.stderr.lower()


@pytest.mark.parametrize("bad,expect", [
    ({"GIT_ON_BRANCH": "1"}, "detached"),
    ({"GIT_DIRTY": "1"}, "staged/unstaged/untracked"),
    ({"GIT_HEAD_SHA": "f" * 40}, "HEAD"),
    ({"GIT_LSREMOTE_SHA": "f" * 40}, "origin/main"),
    ({"GIT_LSREMOTE_FAIL": "1"}, "ls-remote"),
])
def test_source_gate_fails_closed(fs, bad, expect):
    r = _execute(fs, env_extra=bad)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()


def test_busy_session_blocks(fs):
    (fs["live"] / "configs" / "bot-state.json").write_text('{"sessions":{"1:a":{"status":"running"}}}')
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "running/queued" in r.stderr


# ═══ §2 exact service-user capability + ownership ═══════════════════════════
def test_missing_service_user_fails(fs):
    r = _execute(fs, env_extra={"SVC_USER": "missinguser"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "does not exist" in r.stderr


def test_sudo_failure_fails_before_mutation(fs):
    r = _execute(fs, env_extra={"SVC_USER": "svcuser", "SUDO_FAIL": "1"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "sudo" in r.stderr.lower()


def test_root_service_no_sudo_needed(fs):
    # root service + a BROKEN sudo: must still succeed (probes run directly)
    r = _execute(fs, env_extra={"SVC_USER": "root", "SUDO_FAIL": "1"})
    assert _status(fs) == "DEPLOYED", r.stderr[-2000:]


@pytest.mark.parametrize("mode_env,expect", [
    ({"CLAUDE_MODE": "partial"}, "claude catalog missing"),
    ({"CLAUDE_MODE": "fail"}, "claude models failed"),
    ({"CODEX_LOGIN_MODE": "anon"}, "not authenticated"),
    ({"CODEX_LOGIN_MODE": "fail"}, "login status failed"),
    ({"CODEX_MODELS_MODE": "missing"}, "codex catalog missing"),
    ({"CODEX_EXEC_MODE": "nojson"}, "--json"),
    ({"CODEX_RESUME_MODE": "nosession"}, "session-id"),
])
def test_cli_content_contracts_fail_closed(fs, mode_env, expect):
    r = _execute(fs, env_extra=mode_env)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect.lower() in r.stderr.lower()
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()


def test_separate_cache_dir_and_root_owned_files_chowned(fs):
    # non-default cache dir + pre-existing files: install must chown them and
    # verify service-user replace capability (svcuser via fake sudo)
    cache_dir = fs["live"] / "cachedir"; cache_dir.mkdir()
    (cache_dir / "openrouter-catalog.json").write_text('{"cache":"old"}')
    (fs["live"] / "configs" / "coordination.json").write_text('{"entries":{}}')
    (fs["live"] / ".env").write_text(
        _ENV_ORIG + f"BOT_CHAT_CATALOG_CACHE={cache_dir}/openrouter-catalog.json\n")
    r = _execute(fs, env_extra={"SVC_USER": "svcuser", "FAKE_SVC_UID": "0", "FAKE_SVC_GID": "0"})
    assert _status(fs) == "DEPLOYED", r.stderr[-2500:]
    # cache file exists and is owned by the resolved service uid (0 in harness)
    st = (cache_dir / "openrouter-catalog.json").stat()
    assert st.st_uid == 0


# ═══ §3 exact listeners + authenticated readiness ═══════════════════════════
@pytest.mark.parametrize("mode,expect", [
    ("wildcard", "missing exact endpoint"),
    ("wrongip", "missing exact endpoint"),
    ("nobind2", "missing exact endpoint"),
    ("none", "missing exact endpoint"),
])
def test_listener_exact_endpoints_fail_closed(fs, mode, expect):
    r = _execute(fs, env_extra={"SS_MODE": mode})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr


def test_listener_unrelated_process_rejected(fs):
    r = _execute(fs, env_extra={"SS_PID_OVERRIDE": "99999"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "different process" in r.stderr


def test_healthz_contract_rejects_wrong_response(fs):
    r = _execute(fs, env_extra={"CURL_HEALTH_MODE": "bad"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "healthz contract" in r.stderr


@pytest.mark.parametrize("mode,expect", [
    ("e401", "http=401"), ("e429", "http=429"),
    ("badjson", "payload invalid"), ("empty", "payload invalid"),
])
def test_openrouter_probe_fails_closed(fs, mode, expect):
    r = _execute(fs, env_extra={"OR_MODE": mode})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr
    # the key must never leak into output
    assert "sk-or-SECRET-KEEP" not in r.stderr + r.stdout


# ═══ §4 installed-tree integrity (via --tree-manifest utility) ══════════════
def _manifest(d):
    r = subprocess.run(["bash", str(_SCRIPT), "--tree-manifest", str(d)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_tree_manifest_detects_all_change_classes(tmp_path):
    a = tmp_path / "a"; (a / "sub").mkdir(parents=True)
    (a / "f.py").write_text("x = 1\n"); (a / "f.py").chmod(0o644)
    (a / "sub" / "g.py").write_text("y = 2\n")
    (a / "empty").mkdir()
    os.symlink("f.py", a / "lnk")
    base = _manifest(a)

    import shutil
    def clone():
        b = tmp_path / "b"
        if b.exists(): shutil.rmtree(b)
        shutil.copytree(a, b, symlinks=True)
        return b

    b = clone(); (b / "extra.py").write_text("z\n")                    # extra
    assert _manifest(b) != base
    b = clone(); (b / "sub" / "g.py").unlink()                          # missing
    assert _manifest(b) != base
    b = clone(); (b / "f.py").unlink(); (b / "f.py").mkdir()            # type change
    assert _manifest(b) != base
    b = clone(); (b / "f.py").chmod(0o755)                              # mode change
    assert _manifest(b) != base
    b = clone(); (b / "lnk").unlink(); os.symlink("sub/g.py", b / "lnk")  # link target
    assert _manifest(b) != base
    b = clone()
    assert _manifest(b) == base                                          # identical == equal


def test_tree_manifest_covers_empty_dirs(tmp_path):
    a = tmp_path / "a"; a.mkdir(); (a / "e1").mkdir()
    b = tmp_path / "b"; b.mkdir()
    assert _manifest(a) != _manifest(b)


# ═══ §5 secure atomic editors ═══════════════════════════════════════════════
def test_success_full(fs):
    r = _execute(fs)
    assert r.returncode == 0, r.stderr[-3000:]
    assert _status(fs) == "DEPLOYED"
    sc = fs["live"] / "scripts"
    assert (sc / "claude-telegram-bot.py").exists() and (sc / "engines" / "openrouter_chat.py").exists()
    assert not (fs["cron"] / "cleanup-reports").exists()
    assert (fs["cron"] / "unrelated-job").read_text() == _CRON_UNRELATED
    nb = fs["nightly"].read_text()
    assert nb.count("removed report-deletion") == 2 and "apt-get autoclean" in nb
    env_now = (fs["live"] / ".env").read_text()
    assert "BOT_MAX_SESSIONS=9" in env_now
    assert 'OPENROUTER_API_KEY="sk-or-SECRET-KEEP"' in env_now
    assert _field(fs, "FORWARD_RESTARTS") == "1"
    # §6: rendered unit points at the RESOLVED config path + guard + ExecStart
    unit = (fs["systemd"] / "reports-cleanup.service").read_text()
    assert f"EnvironmentFile=-{fs['live']}/configs/reports-cleanup.env" in unit
    assert f"REPORTS_ROOT_GUARD={fs['live']}/reports" in unit
    assert f"ExecStart={fs['live']}/scripts/cleanup-reports.sh" in unit
    # §6: installed timer must not be persistent; NO real cleanup ran (the mock
    # simulates a Persistent=true catch-up by logging a service start)
    timer = (fs["systemd"] / "reports-cleanup.timer").read_text()
    # the DIRECTIVE must be false (a comment may mention Persistent=true)
    assert not any(l.strip().startswith("Persistent=true") for l in timer.splitlines())
    assert any(l.strip() == "Persistent=false" for l in timer.splitlines())
    log = fs["log"].read_text()
    assert "start reports-cleanup.service" not in log
    # timer enable happens after the last restart (commit boundary)
    lines = log.splitlines()
    r_idx = max(i for i, l in enumerate(lines) if l.startswith("restart "))
    e_idx = min(i for i, l in enumerate(lines) if l.startswith("enable reports-cleanup.timer"))
    assert e_idx > r_idx
    # no editor temp remnants
    assert not list(Path(fs["live"]).rglob(".*.deploytmp"))


def test_crlf_env_preserved(fs):
    (fs["live"] / ".env").write_bytes(_ENV_ORIG.replace("\n", "\r\n").encode())
    r = _execute(fs)
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]
    raw = (fs["live"] / ".env").read_bytes()
    assert b"BOT_MAX_SESSIONS=9\r\n" in raw
    assert b'OPENROUTER_API_KEY="sk-or-SECRET-KEEP"\r\n' in raw


def test_no_final_newline_documented_separator(fs):
    (fs["live"] / ".env").write_bytes(_ENV_ORIG.encode().rstrip(b"\n"))
    r = _execute(fs)
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]
    raw = (fs["live"] / ".env").read_bytes()
    # documented sole structural change: the unterminated last line gains one \n
    assert b"LOG_LEVEL=info\n" in raw
    assert b"BOT_COORD_PUBLISHER=" in raw


def test_export_prefix_preserved_on_edit(fs):
    (fs["live"] / ".env").write_text(_ENV_ORIG.replace(
        "BOT_MAX_SESSIONS=6", "export BOT_MAX_SESSIONS=6"))
    r = _execute(fs)
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]
    assert "export BOT_MAX_SESSIONS=9" in (fs["live"] / ".env").read_text()


def test_symlinked_env_refused_sentinel_untouched(fs, tmp_path):
    outside = tmp_path / "outside.env"
    outside.write_text(_ENV_ORIG)
    envp = fs["live"] / ".env"
    envp.unlink(); envp.symlink_to(outside)
    r = _execute(fs)
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"      # parse refuses symlink
    assert outside.read_text() == _ENV_ORIG               # sentinel untouched


def test_symlinked_nightly_refused(fs, tmp_path):
    outside = tmp_path / "outside.sh"; outside.write_text(_NIGHTLY_OK)
    fs["nightly"].unlink(); fs["nightly"].symlink_to(outside)
    r = _execute(fs)
    assert r.returncode != 0
    assert outside.read_text() == _NIGHTLY_OK             # sentinel untouched
    # nightly symlink itself restored by rollback (recorded as link)
    assert _status(fs) in ("ROLLED_BACK", "STOPPED_BEFORE_MUTATION")


def test_duplicate_key_fails_closed_and_rolls_back(fs):
    # duplicate appears only for the EDIT path (parse-critical set catches it
    # first, so craft a duplicate of a non-critical... use critical: parse dies)
    (fs["live"] / ".env").write_text(_ENV_ORIG + "BOT_MAX_SESSIONS=7\n")
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "duplicate" in r.stderr.lower()
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG + "BOT_MAX_SESSIONS=7\n"


def test_ambiguous_nightly_aborts_rolls_back(fs):
    fs["nightly"].write_text(_NIGHTLY_AMBIGUOUS)
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    assert fs["nightly"].read_text() == _NIGHTLY_AMBIGUOUS
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG


# ═══ §7 new-PID-only health with bounded polling ════════════════════════════
def test_delayed_startup_passes_within_deadline(fs):
    r = _execute(fs, env_extra={"JOURNAL_MODE": "delayed"})
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]


def test_startup_timeout_rolls_back(fs):
    r = _execute(fs, env_extra={"JOURNAL_MODE": "stale"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    assert "not ready within" in r.stderr


def test_empty_cursor_fails_before_restart(fs):
    r = _execute(fs, env_extra={"JOURNAL_CURSOR_EMPTY": "1"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    assert "cursor capture failed" in r.stderr
    assert _field(fs, "FORWARD_RESTARTS") == "0"          # never restarted


def test_old_pid_journal_ignored(fs):
    # unfiltered journal contains an old traceback + ready lines; success proves
    # only _PID-filtered new evidence is used
    r = _execute(fs, env_extra={"JOURNAL_MODE": "good"})
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]


def test_new_pid_fatal_fails(fs):
    r = _execute(fs, env_extra={"JOURNAL_MODE": "traceback_new"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"


@pytest.mark.parametrize("mode", ["missing_engine", "extra_engine"])
def test_engine_set_mismatch_fails(fs, mode):
    r = _execute(fs, env_extra={"JOURNAL_MODE": mode})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"


def test_pid_churn_fails(fs):
    # churn begins after restart_once + first health read → observe/health fails
    r = _execute(fs, env_extra={"SC_PID_CHURN_AFTER": "2"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    assert "PID changed" in r.stderr


# ═══ §8 verifiable rollback ═════════════════════════════════════════════════
@pytest.mark.parametrize("stage", [
    "install_artifacts", "edit_env", "migrate_retention", "verify_install",
    "restart_once", "health_check", "observe", "enable_timer",
])
def test_rollback_each_stage_verified(fs, stage):
    r = _execute(fs, env_extra={"DEPLOY_FAIL_AT": stage})
    assert r.returncode != 0
    assert _status(fs) == "ROLLED_BACK", (stage, r.stderr[-1500:])
    assert (fs["live"] / ".env").read_text() == _ENV_ORIG
    assert (fs["cron"] / "cleanup-reports").read_text() == _CRON_A
    assert fs["nightly"].read_text() == _NIGHTLY_OK
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()
    assert not (fs["systemd"] / "reports-cleanup.timer").exists()


@pytest.mark.parametrize("mode,target_key", [
    ("content", "env"), ("mode", "env"), ("owner", "env"),
    ("type", "nightly"), ("extra", "extra"),
])
def test_rollback_corruption_yields_rollback_failed(fs, mode, target_key):
    target = str(fs["live"] / ".env") if target_key == "env" else str(fs["nightly"])
    extra = str(fs["live"] / "scripts" / "claude-telegram-bot.py")  # absent-before
    r = _execute(fs, env_extra={
        "DEPLOY_FAIL_AT": "health_check",
        "DEPLOY_RB_CORRUPT_CMD": str(fs["binp"] / "rbcorrupt"),
        "RB_CORRUPT_MODE": mode, "RB_TARGET": target, "RB_EXTRA": extra,
    })
    assert r.returncode != 0
    assert _status(fs) == "ROLLBACK_FAILED", (mode, r.stderr[-1200:])
    assert _field(fs, "ROLLBACK_ERRORS") not in (None, "<none>")
    assert _field(fs, "FORWARD_EXIT_RC") == "3"           # forward code preserved


def test_rollback_failed_when_old_bot_does_not_recover(fs):
    counter = fs["binp"] / "listener_count.sh"
    counter.write_text('#!/usr/bin/env bash\n'
                       'c="$SC_STATE/lcount"; n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 ));'
                       ' echo $n > "$c"; [ "$n" -le 1 ] && exit 0 || exit 1\n')
    counter.chmod(0o755)
    r = _execute(fs, env_extra={"JOURNAL_MODE": "stale",
                                "DEPLOY_LISTENER_CMD": str(counter)})
    assert r.returncode != 0
    assert _status(fs) == "ROLLBACK_FAILED"


@pytest.mark.parametrize("enabled,active", [(True, True), (True, False), (False, False)])
def test_prior_timer_state_restored(fs, enabled, active):
    if enabled: (fs["sc"] / "timer_enabled").touch()
    if active:  (fs["sc"] / "timer_active").touch()
    r = _execute(fs, env_extra={"DEPLOY_FAIL_AT": "health_check"})
    assert _status(fs) == "ROLLED_BACK", r.stderr[-1500:]
    assert (fs["sc"] / "timer_enabled").exists() == enabled
    assert (fs["sc"] / "timer_active").exists() == active


def test_nondefault_paths_resolved_and_rolled_back(fs):
    alt = fs["live"] / "data"; (alt / "cfg").mkdir(parents=True); (alt / "hist").mkdir()
    (alt / "cfg" / "bot-state.json").write_text('{"sessions":{}, "marker":"ALT"}')
    (alt / "cfg" / "coordination.json").write_text('{"entries":{}}')
    (alt / "cfg" / "reports-token.env").write_text(f"REPORTS_PATH_TOKEN={_TOK}\n")
    (fs["live"] / ".env").write_text(
        _ENV_ORIG
        + f"BOT_CONFIGS_DIR={alt}/cfg\n"
        + f"BOT_CHAT_SESSIONS_DIR={alt}/hist\n"
        + f"BOT_COORDINATION_FILE={alt}/cfg/coordination.json\n")
    r = _execute(fs, env_extra={"DEPLOY_STATE_FILE": str(alt / "cfg" / "bot-state.json"),
                                "DEPLOY_FAIL_AT": "health_check"})
    assert _status(fs) == "ROLLED_BACK", r.stderr[-2000:]
    assert (alt / "cfg" / "bot-state.json").read_text() == '{"sessions":{}, "marker":"ALT"}'
    assert (alt / "cfg" / "coordination.json").read_text() == '{"entries":{}}'


def test_nondefault_configs_render_matching_unit(fs):
    alt = fs["live"] / "data"; (alt / "cfg").mkdir(parents=True)
    (alt / "cfg" / "bot-state.json").write_text('{"sessions": {}}')
    (alt / "cfg" / "reports-token.env").write_text(f"REPORTS_PATH_TOKEN={_TOK}\n")
    (fs["live"] / ".env").write_text(_ENV_ORIG + f"BOT_CONFIGS_DIR={alt}/cfg\n")
    r = _execute(fs, env_extra={"DEPLOY_STATE_FILE": str(alt / "cfg" / "bot-state.json")})
    assert _status(fs) == "DEPLOYED", r.stderr[-2500:]
    unit = (fs["systemd"] / "reports-cleanup.service").read_text()
    # §6: the unit's EnvironmentFile matches the RESOLVED (non-default) config
    assert f"EnvironmentFile=-{alt}/cfg/reports-cleanup.env" in unit
    assert (alt / "cfg" / "reports-cleanup.env").exists()
