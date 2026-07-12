# Phase 1 Deploy Runbook — Stability Foundation

> **Do NOT execute during Phase 1 implementation.** This is the future
> deployment procedure for when the PR is merged and the maintainer chooses to
> deploy. Every step is a manual, gated operation on the DevOps host
> (`devops.shahrzad.ai`) run as root. Nothing here runs Codex or changes
> production during the coding phase.

Context you must know before starting:
- The **live** bot script is `/opt/shahrzad-devops/scripts/claude-telegram-bot.py`
  and is a **separate copy** from the repo `bot.py`. Deploy = copy repo → live +
  restart. Before Phase 1 the live copy had drifted (a second NightWatch listener
  that existed on no branch); Phase 1 backports that capability behind
  `BOT_NIGHTWATCH_IPC_BIND2`, so the live drift is now representable in Git.
- The service unit is `claude-telegram-bot.service`.

## 0. Pre-flight
```bash
cd /opt/shahrzad-devops/repos/ClaudeCodeTelegramBot
git fetch origin
git log --oneline -1 origin/main          # confirm the merged Phase 1 commit
python3 -m pytest -q                        # expect: all pass
python3 -m py_compile bot.py
```

## 1. Back up the live bot script
```bash
cp -a /opt/shahrzad-devops/scripts/claude-telegram-bot.py \
      /opt/shahrzad-devops/scripts/claude-telegram-bot.py.bak.$(date +%Y%m%d-%H%M%S)
```

## 2. Back up env, cron, and cleanup definitions
```bash
B=/root/phase1-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$B"
cp -a /opt/shahrzad-devops/.env                              "$B/.env.bak"
cp -a /etc/cron.d/cleanup-reports                            "$B/" 2>/dev/null || true
cp -a /etc/cron.d/nightwatch-reports-cleanup                "$B/" 2>/dev/null || true
cp -a /opt/shahrzad-devops/scripts/nightly-cleanup.sh       "$B/" 2>/dev/null || true
systemctl cat claude-telegram-bot.service > "$B/claude-telegram-bot.service.txt" 2>/dev/null || true
echo "backup at $B"
```

## 3. Set the second NightWatch listener bind (restores the prod capability)
Edit `/opt/shahrzad-devops/.env` and set:
```env
BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4
```
> This MUST be set **before** copying the merged `bot.py` over the live script,
> because the repo default is empty — without it, the second (VPC) listener that
> prod relies on would not come up.

## 4. Set the logical session limit to 9
Edit `/opt/shahrzad-devops/.env`:
```env
BOT_MAX_SESSIONS=9
```
(was 6)

## 5. Set the concurrent-agent limit to 2
Edit `/opt/shahrzad-devops/.env`:
```env
BOT_MAX_RUNNING_AGENTS=2
```

## 6. Install the canonical report-cleanup service + timer
```bash
cp /opt/shahrzad-devops/repos/ClaudeCodeTelegramBot/systemd/reports-cleanup.service /etc/systemd/system/
cp /opt/shahrzad-devops/repos/ClaudeCodeTelegramBot/systemd/reports-cleanup.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now reports-cleanup.timer
systemctl list-timers reports-cleanup.timer --no-pager
```

## 7. Disable the three conflicting report-retention paths
> See `docs/REPORT_RETENTION_MIGRATION.md` for the full rationale.
```bash
rm -f /etc/cron.d/cleanup-reports
rm -f /etc/cron.d/nightwatch-reports-cleanup
# Edit /opt/shahrzad-devops/scripts/nightly-cleanup.sh and delete ONLY the two
# report `find … reports …` lines in step [6/6]; leave the rest of the script.
```
Validate the retention script first (dry-run, then a real run):
```bash
/opt/shahrzad-devops/repos/ClaudeCodeTelegramBot/scripts/cleanup-reports.sh --dry-run
systemctl start reports-cleanup.service
journalctl -u reports-cleanup.service -n 20 --no-pager
```

## 8. Copy the merged bot.py to the live script path
```bash
cp /opt/shahrzad-devops/repos/ClaudeCodeTelegramBot/bot.py \
   /opt/shahrzad-devops/scripts/claude-telegram-bot.py
```

## 9. Syntax + import checks BEFORE restart
```bash
python3 -m py_compile /opt/shahrzad-devops/scripts/claude-telegram-bot.py
# Import smoke (loads env; confirms no import-time error):
set -a; . /opt/shahrzad-devops/.env; set +a
python3 -c "import importlib.util,sys; \
spec=importlib.util.spec_from_file_location('livebot','/opt/shahrzad-devops/scripts/claude-telegram-bot.py'); \
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); \
print('import OK; MAX_SESSIONS=',m.MAX_SESSIONS,'MAX_RUNNING_AGENTS=',m.MAX_RUNNING_AGENTS)"
```
Expect `MAX_SESSIONS= 9 MAX_RUNNING_AGENTS= 2`.

## 10. Controlled restart
```bash
systemctl restart claude-telegram-bot.service
systemctl status claude-telegram-bot.service --no-pager | head -20
```

## 11. Journal + cgroup verification
```bash
journalctl -u claude-telegram-bot.service -n 40 --no-pager | grep -E 'limits.effective|nightwatch_ipc.listening|Bot v4 ready'
# Expect: limits.effective max_sessions=9 max_running_agents=2
# Expect: nightwatch_ipc.listening binds=['127.0.0.1', '10.108.0.4']:9091
systemctl show claude-telegram-bot.service -p MemoryCurrent -p MemoryMax -p TasksCurrent
cat /sys/fs/cgroup/system.slice/claude-telegram-bot.service/memory.events   # oom_kill should stay 0
```
> If `MemoryMax` (6G) looks tight for 9 sessions, remember only 2 run at once —
> but watch `memory.events`/swap after the first busy period.

## 12. Telegram smoke tests (from the owner chat)
1. `/sessions` — list the existing six sessions (restored from state).
2. Create sessions 7, 8, 9 via `/new`.
3. Attempt session 10 → expect the "Max 9 sessions" rejection.
4. Send three harmless tasks (e.g. "echo hello") across three sessions → verify
   only **two** show `running` at once and the third shows `queued — waiting for
   a free slot (2/2)` until a slot frees.
5. Send two quick tasks to the **same** session → verify they run one-at-a-time
   (the second waits for the first).
6. Kill a session that has a queued task (`/kill <name>`) → verify the queued task
   never fires afterward.
7. Trigger a task that produces long output → tap **Save Report** → open the
   Summary, Browse, and ZIP links (all 200; ZIP downloads and opens).
8. `/nightwatch_ping` → healthz OK; from prod (10.108.0.2) confirm the VPC
   listener answers on `10.108.0.4:9091` (an authorized `/inject` still delivers).

## 13. Rollback (if any gate fails)
```bash
B=/root/phase1-backup-<TIMESTAMP>          # the dir from step 2
# a) restore the live script
cp -a /opt/shahrzad-devops/scripts/claude-telegram-bot.py.bak.<TS> \
      /opt/shahrzad-devops/scripts/claude-telegram-bot.py
# b) restore env
cp -a "$B/.env.bak" /opt/shahrzad-devops/.env
# c) restore cron / cleanup definitions and remove the new timer
cp -a "$B/cleanup-reports"             /etc/cron.d/ 2>/dev/null || true
cp -a "$B/nightwatch-reports-cleanup"  /etc/cron.d/ 2>/dev/null || true
cp -a "$B/nightly-cleanup.sh"          /opt/shahrzad-devops/scripts/ 2>/dev/null || true
systemctl disable --now reports-cleanup.timer
rm -f /etc/systemd/system/reports-cleanup.timer /etc/systemd/system/reports-cleanup.service
# d) reload + restart + verify
systemctl daemon-reload
systemctl restart claude-telegram-bot.service
systemctl status claude-telegram-bot.service --no-pager | head -15
journalctl -u claude-telegram-bot.service -n 20 --no-pager
```

## Not in this phase
Codex CLI, OpenRouter Chat mode, engine selection, cross-agent coordination,
persistent branch checkpoints, memory migration, Graphify integration, and the
RAM/disk message footer are explicitly **out of scope** and come in later phases,
after this foundation is merged and deployed.
