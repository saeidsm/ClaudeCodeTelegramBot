"""Round-5 production-safety matrix for scripts/deploy-phase1-phase2.sh.

Fully offline: a fake root under /tmp with PATH-shadowed fakes (git, systemctl,
journalctl, ss, curl, df, free, id, sudo, claude, codex) exercising the REAL
production branches. No test touches /opt, /etc, real services or the network,
and the deploy script is never run against real paths.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "deploy-phase1-phase2.sh"
_REAL_GIT = shutil.which("git") or "git"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_RHEAD = "1111111111111111111111111111111111111111"   # owner-authorized reviewed PR head
_RTREE = "2222222222222222222222222222222222222222"   # owner-authorized reviewed tree
_RPR = "19"                                            # owner-authorized reviewed PR number
_RREF = "refs/heads/some-reviewed-branch"               # owner-authorized reviewed PR ref
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

_CODEX_IDS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


def _mock_bin(d: Path) -> Path:
    b = d / "bin"; b.mkdir()

    (b / "git").write_text(f"""#!/usr/bin/env bash
case "$*" in
  *"symbolic-ref -q HEAD"*) [ "${{GIT_ON_BRANCH:-0}}" = 1 ] && {{ echo refs/heads/x; exit 0; }} || exit 1 ;;
  *"status --porcelain"*)   [ "${{GIT_DIRTY:-0}}" = 1 ] && echo " M f"; exit 0 ;;
  *"check-ref-format"*)     exec "{_REAL_GIT}" "$@" ;;
  *"^{{tree}}"*)             echo "${{GIT_MERGED_TREE:-{_RTREE}}}"; exit 0 ;;
  *"rev-parse HEAD"*)        echo "${{GIT_HEAD_SHA:-{_SHA}}}"; exit 0 ;;
  *"ls-remote"*"refs/heads/main"*)
    [ "${{GIT_LSREMOTE_FAIL:-0}}" = 1 ] && exit 0
    printf '%s\\trefs/heads/main\\n' "${{GIT_LSREMOTE_SHA:-{_SHA}}}"; exit 0 ;;
  *"ls-remote"*)
    # generic reviewed-ref lookup: reply with the queried ref (no hard-coded branch)
    [ "${{GIT_PRBRANCH_DELETED:-0}}" = 1 ] && exit 0
    printf '%s\\t%s\\n' "${{GIT_PRBRANCH_SHA:-{_RHEAD}}}" "${{@: -1}}"; exit 0 ;;
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
    elif [ "$2" = "claude-telegram-bot" ]; then
      # prove the service stayed quiesced for the whole mutation window
      [ -f "$S/svc_down" ] && echo "start-from-quiesced" >> "$SC_LOG"
      rm -f "$S/svc_down"
      echo $(( $(cat "$S/pid") + 1000 )) > "$S/pid"; echo 0 > "$S/pidreads"
    fi; exit 0 ;;
  stop)
    if [ "$2" = "claude-telegram-bot" ]; then
      [ "${SC_STOP_FAIL:-0}" = 1 ] && exit 1
      # optionally simulate a state change/turn admitted during shutdown
      if [ -n "${SC_ON_STOP_INJECT_CONTENT:-}" ]; then
        printf '%s' "$SC_ON_STOP_INJECT_CONTENT" > "$DEPLOY_STATE_FILE"
      fi
      touch "$S/svc_down"
    else
      rm -f "$S/timer_active"
    fi; exit 0 ;;
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
  [ "${JOURNAL_QUERY_FAIL:-0}" = 1 ] && exit 1
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
[ -n "${SS_EXTRA_ENDPOINT:-}" ] && \
  echo "LISTEN 0 128 ${SS_EXTRA_ENDPOINT}:9091 0.0.0.0:* users:((\"python3\",pid=$pid,fd=9))"
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

    (b / "claude").write_text("""#!/usr/bin/env bash
# ANY `models` invocation is recorded — the gate must NEVER call it (it is a
# prompt, not a subcommand; the reply is nondeterministic LLM text)
case "$*" in
  *models*)
    touch "$SC_STATE/claude_models_called"
    # even an answer containing every id must not be able to pass the gate
    echo "Here are the models: claude-fable-5 claude-opus-4-8 claude-sonnet-5 claude-haiku-4-5-20251001"
    exit 0 ;;
  "--version")
    case "${CLAUDE_MODE:-ok}" in
      noversion)   exit 1 ;;
      badversion)  echo "not-a-version"; exit 0 ;;
      emptyversion) exit 0 ;;
      embeddedversion) echo "Claude Code version 2.1.214 build 9"; exit 0 ;;
      multilineversion) echo "2.1.214 (Claude Code)"; echo "extra line"; exit 0 ;;
      bareversion) echo "2.1.214"; exit 0 ;;
      *)           echo "2.1.214 (Claude Code)"; exit 0 ;;
    esac ;;
  "--help")
    case "${CLAUDE_MODE:-ok}" in
      nomodel) echo "Usage: claude [options]"; echo "  --print   run and exit"; exit 0 ;;
      noprint) echo "Usage: claude [options]"; echo "  --model <model>  choose model"; exit 0 ;;
      *) echo "Usage: claude [options] [command] [prompt]"
         echo "  --model <model>   Model for the current session"
         echo "  -p, --print       Print response and exit"
         exit 0 ;;
    esac ;;
  "auth status --json")
    case "${CLAUDE_MODE:-ok}" in
      authfail)    exit 1 ;;
      noauth)      printf '{"loggedIn": false}'; exit 0 ;;
      authbadjson) printf 'not json {'; exit 0 ;;
      authprefix)  printf 'note: {"loggedIn": true}'; exit 0 ;;
      authsuffix)  printf '{"loggedIn": true} trailing'; exit 0 ;;
      authnested)  printf '{"auth": {"loggedIn": true}}'; exit 0 ;;
      authstring)  printf '{"loggedIn": "true"}'; exit 0 ;;
      authnumeric) printf '{"loggedIn": 1}'; exit 0 ;;
      authmissing) printf '{"authMethod": "claude.ai"}'; exit 0 ;;
      autharray)   printf '[{"loggedIn": true}]'; exit 0 ;;
      *)           printf '{"loggedIn": true, "authMethod": "claude.ai"}'; exit 0 ;;
    esac ;;
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
  linkowner) chown -h 12345:12345 "$RB_TARGET" ;;
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
              "JOURNAL_CURSOR_EMPTY", "DF_AVAIL_KB", "FREE_MEM_KB", "FREE_SWAP_KB",
              "GIT_MERGED_TREE", "GIT_PRBRANCH_SHA", "GIT_PRBRANCH_DELETED",
              "SC_STOP_FAIL", "SC_ON_STOP_INJECT_CONTENT", "JOURNAL_QUERY_FAIL",
              "SS_EXTRA_ENDPOINT"):
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


def _execute(fs, env_extra=None, reviewed=True):
    args = ["--execute", "--reviewed-pr", _RPR, "--merged-sha", _SHA]
    if reviewed:
        args += ["--reviewed-head", _RHEAD, "--reviewed-tree", _RTREE, "--reviewed-ref", _RREF]
    return _run(fs, *args, env_extra=env_extra)


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
    ({"CLAUDE_MODE": "noversion"}, "claude --version failed"),
    ({"CLAUDE_MODE": "badversion"}, "not a version"),
    ({"CLAUDE_MODE": "emptyversion"}, "not a version"),
    ({"CLAUDE_MODE": "embeddedversion"}, "not a version"),
    ({"CLAUDE_MODE": "multilineversion"}, "not a version"),
    ({"CLAUDE_MODE": "nomodel"}, "lacks --model"),
    ({"CLAUDE_MODE": "noprint"}, "lacks --print"),
    ({"CLAUDE_MODE": "noauth"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "authfail"}, "claude auth status failed"),
    ({"CLAUDE_MODE": "authbadjson"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "authprefix"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "authsuffix"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "authnested"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "authstring"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "authnumeric"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "authmissing"}, "claude not authenticated"),
    ({"CLAUDE_MODE": "autharray"}, "claude not authenticated"),
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
    # Gap 2: exactly ONE controlled old->new transition (one stop, one start),
    # the service provably stayed quiesced across the whole mutation window,
    # and the timer activates only after the start (commit boundary)
    lines = log.splitlines()
    stops = [i for i, l in enumerate(lines) if l.strip() == "stop claude-telegram-bot"]
    starts = [i for i, l in enumerate(lines) if l.strip() == "start claude-telegram-bot"]
    assert len(stops) == 1 and len(starts) == 1
    assert stops[0] < starts[0]
    assert "start-from-quiesced" in log
    e_idx = min(i for i, l in enumerate(lines) if l.startswith("enable reports-cleanup.timer"))
    assert e_idx > starts[0]
    assert _field(fs, "QUIESCE_STOPS") == "1"
    # Gap 1: reviewed identities recorded in the transaction report
    assert _field(fs, "REVIEWED_HEAD") == _RHEAD
    assert _field(fs, "REVIEWED_TREE") == _RTREE
    assert _field(fs, "MERGED_TREE") == _RTREE
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
    "start_new", "health_check", "observe", "enable_timer",
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


# ═══ Round 6 — Gap 1: reviewed-tree authorization gate ══════════════════════
def test_reviewed_tree_mismatch_fails_before_mutation(fs):
    live0 = _digest(fs["live"])
    r = _execute(fs, env_extra={"GIT_MERGED_TREE": "3" * 40})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "reviewed tree" in r.stderr
    assert _digest(fs["live"]) == live0


def test_reviewed_identity_missing_fails(fs):
    r = _execute(fs, reviewed=False)                      # no --reviewed-head/tree
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "40 hex" in r.stderr


def test_reviewed_identity_malformed_fails(fs):
    r = _run(fs, "--execute", "--reviewed-pr", _RPR, "--merged-sha", _SHA,
             "--reviewed-head", "abc", "--reviewed-tree", _RTREE)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "40 hex" in r.stderr


def test_squash_commit_different_sha_matching_tree_succeeds(fs):
    # squash merge: merged commit != reviewed head, PR branch deleted post-merge
    # -> the reviewed-TREE equality is the decisive documented gate
    r = _execute(fs, env_extra={"GIT_PRBRANCH_DELETED": "1"})
    assert _status(fs) == "DEPLOYED", r.stderr[-2000:]
    assert "PR branch deleted post-merge" in r.stderr
    assert _field(fs, "MERGED_TREE") == _RTREE


def test_stale_live_pr_branch_fails(fs):
    r = _execute(fs, env_extra={"GIT_PRBRANCH_SHA": "4" * 40})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "stale PR branch" in r.stderr


# ═══ Hotfix — dynamic reviewed PR/ref identity (no hard-coded PR/branch) ════
def test_reviewed_ref_dynamic_branch_succeeds(fs):
    # a DIFFERENT PR number and a DIFFERENT branch than any other test's
    # default prove nothing in the gate is hard-coded
    other_ref = "refs/heads/totally-different-reviewed-branch"
    r = _run(fs, "--execute", "--reviewed-pr", "777", "--merged-sha", _SHA,
             "--reviewed-head", _RHEAD, "--reviewed-tree", _RTREE,
             "--reviewed-ref", other_ref)
    assert _status(fs) == "DEPLOYED", r.stderr[-2000:]
    assert _field(fs, "REVIEWED_PR") == "777"
    assert _field(fs, "REVIEWED_REF") == other_ref


@pytest.mark.parametrize("pr", ["", "0", "-1", "abc", "1.5", "01", " 19", "19 ", "19\n"])
def test_reviewed_pr_invalid_rejected(fs, pr):
    live0 = _digest(fs["live"])
    r = _run(fs, "--execute", "--reviewed-pr", pr, "--merged-sha", _SHA,
             "--reviewed-head", _RHEAD, "--reviewed-tree", _RTREE,
             "--reviewed-ref", _RREF)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "positive decimal integer" in r.stderr
    assert _digest(fs["live"]) == live0


def test_reviewed_ref_missing_fails(fs):
    live0 = _digest(fs["live"])
    r = _run(fs, "--execute", "--reviewed-pr", _RPR, "--merged-sha", _SHA,
             "--reviewed-head", _RHEAD, "--reviewed-tree", _RTREE)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "refs/heads/" in r.stderr
    assert _digest(fs["live"]) == live0


@pytest.mark.parametrize("ref,expect", [
    ("claude/fix-something", "refs/heads/"),           # missing refs/heads/ prefix
    ("refs/tags/v1", "refs/heads/"),                   # wrong ref namespace
    ("--reviewed-pr", "refs/heads/"),                  # option-like value
    ("refs/heads/", "invalid"),                        # empty name after prefix
    ("refs/heads/foo;rm -rf /", "invalid"),            # shell metacharacter (;)
    ("refs/heads/foo|bar", "invalid"),                 # shell metacharacter (|)
    ("refs/heads/$(whoami)", "invalid"),                # command substitution
    ("refs/heads/foo bar", "invalid"),                 # embedded whitespace
    ("refs/heads/foo\tbar", "invalid"),                # embedded control char
    ("refs/heads/../etc/passwd", "check-ref-format"),  # ref traversal
])
def test_reviewed_ref_malformed_rejected(fs, ref, expect):
    live0 = _digest(fs["live"])
    r = _run(fs, "--execute", "--reviewed-pr", _RPR, "--merged-sha", _SHA,
             "--reviewed-head", _RHEAD, "--reviewed-tree", _RTREE,
             "--reviewed-ref", ref)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr
    assert _digest(fs["live"]) == live0


# ═══ Round 6 — Gap 2: fail-closed state + quiescence boundary ═══════════════
@pytest.mark.parametrize("content,expect", [
    ("not-json{", "invalid JSON"),
    ('{"sessions": {"1:a": {"status": "weird"}}}', "unknown session status"),
    ('["not-an-object"]', "top-level is not an object"),
    ('{"nosessions": true}', "missing 'sessions'"),
])
def test_state_fail_closed_variants(fs, content, expect):
    (fs["live"] / "configs" / "bot-state.json").write_text(content)
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert expect in r.stderr


def test_state_missing_fails_closed(fs):
    (fs["live"] / "configs" / "bot-state.json").unlink()
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "state file missing" in r.stderr


def test_turn_admitted_during_quiesce_aborts_without_mutation(fs):
    injected = '{"sessions": {"9:late": {"status": "queued"}}}'
    env0 = (fs["live"] / ".env").read_text(); cron0 = _digest(fs["cron"])
    r = _execute(fs, env_extra={"SC_ON_STOP_INJECT_CONTENT": injected})
    assert r.returncode != 0
    assert _status(fs) == "STOPPED_BEFORE_MUTATION"       # no file mutation happened
    assert "turn" in r.stderr.lower() or "quiesce (authoritative post-stop)" in r.stderr
    # the admitted turn's state is PRESERVED (not lost, not overwritten)
    assert (fs["live"] / "configs" / "bot-state.json").read_text() == injected
    assert (fs["live"] / ".env").read_text() == env0 and _digest(fs["cron"]) == cron0
    # availability restored: old bot started again
    assert _field(fs, "QUIESCE_RESTORE") == "ok"
    assert "start claude-telegram-bot" in fs["log"].read_text()
    assert not (fs["live"] / "scripts" / "claude-telegram-bot.py").exists()


def test_quiesce_stop_failure_restores_availability(fs):
    live0 = _digest(fs["live"])
    r = _execute(fs, env_extra={"SC_STOP_FAIL": "1"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "quiesce" in r.stderr.lower()
    assert _digest(fs["live"]) == live0                    # live files untouched
    assert _field(fs, "QUIESCE_RESTORE") == "ok"           # availability restored


def test_latest_state_change_preserved_by_rollback(fs):
    # the bot writes fresh state during shutdown (0 active) -> the backup is the
    # AUTHORITATIVE post-quiesce state; rollback must restore exactly it
    latest = '{"sessions": {}, "marker": "LATEST-AT-SHUTDOWN"}'
    r = _execute(fs, env_extra={"SC_ON_STOP_INJECT_CONTENT": latest,
                                "DEPLOY_FAIL_AT": "health_check"})
    assert _status(fs) == "ROLLED_BACK", r.stderr[-1500:]
    assert (fs["live"] / "configs" / "bot-state.json").read_text() == latest


# ═══ Round 6 — Gap 3: rollback uses ORIGINAL listener expectations ══════════
def test_rollback_listeners_original_bind2_absent(fs):
    # original env has NO BIND2; forward expects 10.108.0.4 (we write it);
    # ss offers only the primary endpoint -> forward health fails, rollback
    # must verify the ORIGINAL endpoints (primary only) and succeed
    (fs["live"] / ".env").write_text(_ENV_ORIG.replace(
        "BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4\n", ""))
    r = _execute(fs, env_extra={"SS_MODE": "nobind2"})
    assert r.returncode != 0
    assert "10.108.0.4" in r.stderr                        # forward expectation failed
    assert _status(fs) == "ROLLED_BACK"                    # original profile passed


def test_rollback_listeners_original_different_bind2(fs):
    (fs["live"] / ".env").write_text(_ENV_ORIG.replace(
        "BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4", "BOT_NIGHTWATCH_IPC_BIND2=10.99.0.7"))
    r = _execute(fs, env_extra={"SS_MODE": "nobind2", "SS_EXTRA_ENDPOINT": "10.99.0.7"})
    assert r.returncode != 0
    assert "10.108.0.4" in r.stderr                        # forward failed on new BIND2
    assert _status(fs) == "ROLLED_BACK"                    # original (10.99.0.7) verified


def test_rollback_listeners_original_disabled(fs):
    (fs["live"] / ".env").write_text(
        _ENV_ORIG + "BOT_NIGHTWATCH_IPC_ENABLED=false\n")
    # no listeners exist at all; failure comes from stale journal; rollback must
    # SKIP listener verification because the ORIGINAL config disabled it
    r = _execute(fs, env_extra={"SS_MODE": "none", "JOURNAL_MODE": "stale"})
    assert r.returncode != 0
    assert _status(fs) == "ROLLED_BACK", r.stderr[-1500:]


# ═══ Round 6 — hardening: partial write / journal failure / link metadata ═══
def test_editor_survives_partial_writes(fs, tmp_path):
    # usercustomize monkeypatches os.write to write at most 3 bytes per call;
    # the editor's complete-write loop must still produce the exact result
    hookdir = tmp_path / "hook"; hookdir.mkdir()
    (hookdir / "usercustomize.py").write_text(
        "import os\n_real = os.write\n"
        "os.write = lambda fd, b: _real(fd, bytes(b)[:3])\n")
    envf = tmp_path / "p.env"
    envf.write_bytes(b'A=1\r\nBOT_MAX_SESSIONS=6\r\nB="x"\r\n')
    env = dict(fs["env"]); env["PYTHONPATH"] = str(hookdir)
    env["DEPLOY_REPORT"] = str(tmp_path / "rep.md")
    r = subprocess.run(["bash", str(_SCRIPT), "--set-env-key", str(envf),
                        "BOT_MAX_SESSIONS", "9"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    assert envf.read_bytes() == b'A=1\r\nBOT_MAX_SESSIONS=9\r\nB="x"\r\n'


def test_persistent_journal_failure_distinct_fail_closed(fs):
    r = _execute(fs, env_extra={"JOURNAL_QUERY_FAIL": "1"})
    assert r.returncode != 0 and _status(fs) == "ROLLED_BACK"
    assert "journal retrieval persistently failing" in r.stderr


def test_restored_symlink_owner_corruption_rollback_failed(fs, tmp_path):
    outside = tmp_path / "outside.sh"; outside.write_text(_NIGHTLY_OK)
    fs["nightly"].unlink(); fs["nightly"].symlink_to(outside)
    # migrate_retention refuses the symlink -> rollback restores the symlink;
    # the injector then chown -h's it, so link owner/group verification must fail
    r = _execute(fs, env_extra={
        "DEPLOY_RB_CORRUPT_CMD": str(fs["binp"] / "rbcorrupt"),
        "RB_CORRUPT_MODE": "linkowner", "RB_TARGET": str(fs["nightly"]),
    })
    assert r.returncode != 0
    assert _status(fs) == "ROLLBACK_FAILED"
    assert "symlink owner mismatch" in r.stderr or "symlink group mismatch" in r.stderr


# ═══ Hotfix — deterministic Claude CLI gate (no LLM text in readiness) ══════
def test_claude_models_is_never_invoked(fs):
    # success run: the gate must pass WITHOUT ever calling `claude models`
    r = _execute(fs)
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]
    assert not (fs["sc"] / "claude_models_called").exists()


def test_llm_text_cannot_satisfy_the_gate(fs):
    # `claude models` (if it were called) returns text containing EVERY adapter
    # id — yet with auth failing, the gate must still fail closed, proving the
    # LLM answer has no path into the readiness decision
    r = _execute(fs, env_extra={"CLAUDE_MODE": "noauth"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "claude not authenticated" in r.stderr
    assert not (fs["sc"] / "claude_models_called").exists()


def test_claude_bare_version_output_accepted(fs):
    # `2.1.214` with no suffix is a valid contract answer (default-mode runs
    # already prove `2.1.214 (Claude Code)` passes)
    r = _execute(fs, env_extra={"CLAUDE_MODE": "bareversion"})
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]


def test_claude_auth_response_never_printed(fs):
    # strict-JSON rejection must not leak the auth payload into any output
    r = _execute(fs, env_extra={"CLAUDE_MODE": "authnested"})
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "loggedIn" not in r.stdout + r.stderr


def test_bot_claude_models_override_validated(fs):
    # well-formed override passes; malformed (empty id) fails closed
    (fs["live"] / ".env").write_text(
        _ENV_ORIG + "BOT_CLAUDE_MODELS=claude-opus-4-8:Opus,claude-sonnet-5:Sonnet\n")
    r = _execute(fs)
    assert _status(fs) == "DEPLOYED", r.stderr[-1500:]

    (fs["live"] / ".env").write_text(
        _ENV_ORIG + "BOT_CLAUDE_MODELS=:NoId,claude-sonnet-5:Sonnet\n")
    r = _execute(fs)
    assert r.returncode != 0 and _status(fs) == "STOPPED_BEFORE_MUTATION"
    assert "catalog validation failed" in r.stderr
