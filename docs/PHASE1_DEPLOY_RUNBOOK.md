# Phase 1 Deploy Runbook — Stability Foundation

> **Documentation only. Do NOT execute during implementation or review.** This is
> the future deployment procedure for when PR #16 is merged and the maintainer
> chooses to deploy. Every step is a manual, gated operation on the DevOps host
> (`devops.shahrzad.ai`) run as root. Nothing here runs Codex or changes
> production during the coding/review phase.

Context you must know before starting:
- The **live** bot script is `/opt/shahrzad-devops/scripts/claude-telegram-bot.py`
  and is a **separate copy** from any repo `bot.py`. Deploy = copy a **verified,
  immutable** `bot.py` → live + restart.
- **Never** deploy from the dirty local `main` checkout (it carries uncommitted,
  possibly stale changes). Deploy only from a clean detached worktree checked out
  at the exact merged commit. Never `pull`/`checkout`/`reset`/`stash`/`clean` the
  dirty main worktree.
- The service unit is `claude-telegram-bot.service`.

Set these once at the top of your shell:
```bash
REPO=/opt/shahrzad-devops/repos/ClaudeCodeTelegramBot
FINAL_PHASE1_COMMIT=<the reviewed final commit on claude/phase1-stability-foundation>
```

## 0. Establish the immutable deployment source (Section 5)
```bash
cd "$REPO"
git fetch origin
MERGED_SHA=$(git rev-parse origin/main)
echo "origin/main = $MERGED_SHA"

# Gate: the reviewed Phase 1 commit MUST be an ancestor of merged main.
git merge-base --is-ancestor "$FINAL_PHASE1_COMMIT" "$MERGED_SHA" \
  || { echo "ABORT: $FINAL_PHASE1_COMMIT is not in origin/main"; exit 1; }

SHORT=$(git rev-parse --short "$MERGED_SHA")
DEPLOY_WT=/tmp/claude-bot-phase1-deploy-$SHORT
# Clean, DETACHED worktree at the exact merged SHA (never touches main worktree).
git worktree add --detach "$DEPLOY_WT" "$MERGED_SHA"
git -C "$DEPLOY_WT" rev-parse HEAD    # must equal $MERGED_SHA
```
All subsequent install/copy steps read only from `$DEPLOY_WT`.

## 1. Validate inside the clean deployment worktree (Section 11)
```bash
cd "$DEPLOY_WT"
python3 -m pytest -q                      # expect: all pass
python3 -m py_compile bot.py
bash -n scripts/cleanup-reports.sh
```

## 2. Back up live script, env, cron, and cleanup definitions
```bash
B=/root/phase1-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$B"; echo "backup at $B"
cp -a /opt/shahrzad-devops/scripts/claude-telegram-bot.py "$B/claude-telegram-bot.py.bak"
cp -a /opt/shahrzad-devops/.env                           "$B/.env.bak"
cp -a /etc/cron.d/cleanup-reports                         "$B/" 2>/dev/null || true
cp -a /etc/cron.d/nightwatch-reports-cleanup             "$B/" 2>/dev/null || true
cp -a /opt/shahrzad-devops/scripts/nightly-cleanup.sh    "$B/" 2>/dev/null || true
cp -a /opt/shahrzad-devops/scripts/cleanup-reports.sh    "$B/cleanup-reports.sh.bak"  2>/dev/null || true
cp -a /opt/shahrzad-devops/configs/reports-cleanup.env   "$B/reports-cleanup.env.bak" 2>/dev/null || true
systemctl cat claude-telegram-bot.service > "$B/claude-telegram-bot.service.txt" 2>/dev/null || true
```
> Retain `$B` through the entire production observation window (see step 12) —
> do not delete backups on the same day.

## 3. Set env values (edit `/opt/shahrzad-devops/.env`)
```env
BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4     # restores the live-only VPC listener (was drift)
BOT_MAX_SESSIONS=9                      # was 6
BOT_MAX_RUNNING_AGENTS=2
```
> `BOT_NIGHTWATCH_IPC_BIND2` **must** be set before replacing the live script, or
> the second (VPC) listener won't bind (repo default is empty).

## 4. Dedicated non-secret cleanup config + retention migration
Create the dedicated config (the cleanup service loads **only** this — never the
full `.env`; see Section 7 and `REPORT_RETENTION_MIGRATION.md`):
```bash
cat > /opt/shahrzad-devops/configs/reports-cleanup.env <<'EOF'
BOT_REPORT_RETENTION_DAYS=15
EOF
chmod 0644 /opt/shahrzad-devops/configs/reports-cleanup.env
```
Install the cleanup script to the **stable live path** from `$DEPLOY_WT`, install
the units, and disable the three conflicting jobs — full steps in
`REPORT_RETENTION_MIGRATION.md`:
```bash
install -m 0755 "$DEPLOY_WT/scripts/cleanup-reports.sh" /opt/shahrzad-devops/scripts/cleanup-reports.sh
cp "$DEPLOY_WT/systemd/reports-cleanup.service" /etc/systemd/system/
cp "$DEPLOY_WT/systemd/reports-cleanup.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now reports-cleanup.timer
# Gate: the unit resolves the STABLE live script (not a repo checkout):
systemctl show reports-cleanup.service -p ExecStart | grep -q /opt/shahrzad-devops/scripts/cleanup-reports.sh \
  && echo "ExecStart OK"
rm -f /etc/cron.d/cleanup-reports /etc/cron.d/nightwatch-reports-cleanup
# hand-edit nightly-cleanup.sh to drop ONLY the two report find-lines
/opt/shahrzad-devops/scripts/cleanup-reports.sh --dry-run
```

## 5. Copy the merged bot.py from the immutable worktree to the live path (Section 5/6)
```bash
cp "$DEPLOY_WT/bot.py" /opt/shahrzad-devops/scripts/claude-telegram-bot.py
# Gate: live hash MUST equal the deployment-worktree source.
test "$(sha256sum "$DEPLOY_WT/bot.py" | cut -d' ' -f1)" \
   = "$(sha256sum /opt/shahrzad-devops/scripts/claude-telegram-bot.py | cut -d' ' -f1)" \
   && echo "deployed hash matches \$DEPLOY_WT/bot.py"
```

## 6. Syntax + config verification WITHOUT sourcing the full .env (Section 11)
Do **not** `source`/`.` the full `.env` into your shell (it would export every
secret). Parse it with a non-executing parser and inject only into the check:
```bash
python3 -m py_compile /opt/shahrzad-devops/scripts/claude-telegram-bot.py
python3 - <<'PY'
import os, ast, importlib.util
# non-executing dotenv parse (KEY=VALUE lines only; no shell eval)
env = {}
with open("/opt/shahrzad-devops/.env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
os.environ.update(env)
spec = importlib.util.spec_from_file_location(
    "livebot", "/opt/shahrzad-devops/scripts/claude-telegram-bot.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print("import OK; MAX_SESSIONS=", m.MAX_SESSIONS, "MAX_RUNNING_AGENTS=", m.MAX_RUNNING_AGENTS)
assert m.MAX_SESSIONS == 9 and m.MAX_RUNNING_AGENTS == 2
PY
```

## 7. Pre-restart gates (Section 11)
```bash
# Gate: no session currently running or queued (avoid killing live work).
journalctl -u claude-telegram-bot.service -n 200 --no-pager | tail -5
# Inspect the live state file; abort if any session is running/queued:
python3 - <<'PY'
import json
s = json.load(open("/opt/shahrzad-devops/configs/bot-state.json"))
busy = [k for k,v in s.get("sessions",{}).items() if v.get("status") in ("running","queued")]
print("busy sessions:", busy)
raise SystemExit(1 if busy else 0)
PY
```
> **Explicit owner confirmation required here.** Do not proceed to restart until
> the maintainer confirms in-band ("go") immediately before the next step.

## 8. Controlled restart
```bash
systemctl restart claude-telegram-bot.service
systemctl status claude-telegram-bot.service --no-pager | head -20
```

## 9. Journal + cgroup verification
```bash
journalctl -u claude-telegram-bot.service -n 40 --no-pager | grep -E 'limits.effective|nightwatch_ipc.listening|Bot v4 ready'
# Expect: limits.effective max_sessions=9 max_running_agents=2
# Expect: nightwatch_ipc.listening binds=['127.0.0.1', '10.108.0.4']:9091
systemctl show claude-telegram-bot.service -p MemoryCurrent -p MemoryMax -p TasksCurrent
cat /sys/fs/cgroup/system.slice/claude-telegram-bot.service/memory.events   # oom_kill should stay 0
```

## 10. Telegram smoke tests (from the owner chat)
1. `/sessions` — list restored sessions.
2. Create sessions up to 9; attempt a 10th → expect the "Max 9 sessions" rejection.
3. Send three harmless tasks across three sessions → verify only **two** run at
   once; the third shows `queued — waiting for a free slot (2/2)`.
4. Two quick tasks to the **same** session → verify they serialize (FIFO).
5. Kill a session with a queued task → verify the queued task never fires.
6. Long-output task → **Save Report** → open Summary, Browse, ZIP (all 200; ZIP opens).
7. `/nightwatch_ping` → healthz OK; from prod (10.108.0.2) confirm the VPC
   listener answers on `10.108.0.4:9091`.

## 11. Success cleanup
```bash
# Remove the deployment worktree ONLY after successful deploy (or completed rollback).
git -C "$REPO" worktree remove "$DEPLOY_WT"
git -C "$REPO" worktree prune
```
Keep `$B` (backups) through the observation window (step 12).

## 12. Observation window & rollback
Observe for at least one busy period (watch `memory.events`/swap; 9 sessions ×
2 concurrent on 6G `MemoryMax`). **Retain all backups in `$B` until the window
closes.** If anything regresses:
```bash
# a) restore the live script from the immutable backup
cp -a "$B/claude-telegram-bot.py.bak" /opt/shahrzad-devops/scripts/claude-telegram-bot.py
# b) restore env
cp -a "$B/.env.bak" /opt/shahrzad-devops/.env
# c) restore cron / nightly cleanup
cp -a "$B/cleanup-reports"             /etc/cron.d/ 2>/dev/null || true
cp -a "$B/nightwatch-reports-cleanup"  /etc/cron.d/ 2>/dev/null || true
cp -a "$B/nightly-cleanup.sh"          /opt/shahrzad-devops/scripts/ 2>/dev/null || true
# d) restore/remove the installed cleanup script + dedicated config
if [ -f "$B/cleanup-reports.sh.bak" ]; then cp -a "$B/cleanup-reports.sh.bak" /opt/shahrzad-devops/scripts/cleanup-reports.sh; else rm -f /opt/shahrzad-devops/scripts/cleanup-reports.sh; fi
if [ -f "$B/reports-cleanup.env.bak" ]; then cp -a "$B/reports-cleanup.env.bak" /opt/shahrzad-devops/configs/reports-cleanup.env; else rm -f /opt/shahrzad-devops/configs/reports-cleanup.env; fi
systemctl disable --now reports-cleanup.timer 2>/dev/null || true
rm -f /etc/systemd/system/reports-cleanup.timer /etc/systemd/system/reports-cleanup.service
# e) reload + restart + verify
systemctl daemon-reload
systemctl restart claude-telegram-bot.service
systemctl status claude-telegram-bot.service --no-pager | head -15
```

## Not in this phase
Codex CLI, OpenRouter Chat mode, engine selection, cross-agent coordination,
persistent branch checkpoints, memory migration, Graphify integration, and the
RAM/disk message footer are explicitly **out of scope** and come in later phases.
