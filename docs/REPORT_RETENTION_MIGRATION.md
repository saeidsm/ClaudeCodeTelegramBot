# Report Retention Migration — three conflicting jobs → one canonical timer

## Problem

Report links (Summary / Browse / ZIP) sent to Telegram live forever in chat
history, but the report files behind them were being erased far sooner by
**three conflicting, undocumented cleanup jobs** on the DevOps host. The result:
of 236 report directories observed during recon, only 21 still had a
`summary.txt`, 21 had a `.zip`, and **196 were completely empty** — so most
older links 404'd on Summary and ZIP while Browse showed a hollow directory.

## The three conflicting live jobs (as found on prod)

1. **`/etc/cron.d/cleanup-reports`** — hourly:
   ```cron
   0 * * * * root find /opt/shahrzad-devops/reports -type f -mmin +1440 -delete
   ```
   Deletes every **file** older than 24h **but leaves the directories** → the
   empty-dir / 404 pattern. This is the primary culprit.

2. **`/opt/shahrzad-devops/scripts/nightly-cleanup.sh`** (step `[6/6]`, invoked
   from the nightly cleanup workflow) — deletes report **dirs** older than 1 day
   and `*.zip` older than 1 day:
   ```bash
   find /opt/shahrzad-devops/reports -mindepth 1 -maxdepth 1 -type d -mtime +1 -exec rm -rf {} +
   find /opt/shahrzad-devops/reports -maxdepth 1 -name "*.zip" -mtime +1 -delete
   ```

3. **`/etc/cron.d/nightwatch-reports-cleanup`** — daily, deletes token-scoped
   report **dirs** older than 15 days:
   ```cron
   0 0 * * * root find /opt/shahrzad-devops/reports/<TOKEN> -mindepth 1 -maxdepth 1 -type d -mtime +15 -exec rm -rf {} +
   ```

Policies contradict each other (24h files vs 1-day dirs/zips vs 15-day dirs).

## Target policy

> A report's **Summary + Browse directory + ZIP stay available together for 15
> days**, then the whole triplet is removed as a unit. Already-hollow historical
> directories are swept regardless of age.

Implemented by the repository-owned, safety-guarded
[`scripts/cleanup-reports.sh`](../scripts/cleanup-reports.sh), run daily by
[`systemd/reports-cleanup.timer`](../systemd/reports-cleanup.timer) →
[`systemd/reports-cleanup.service`](../systemd/reports-cleanup.service).
Retention is overridable via `BOT_REPORT_RETENTION_DAYS` (default 15).

## Migration — exact future deployment steps

> Run on the DevOps host as root. **This migration is NOT performed by Phase 1;**
> it is executed at deploy time per the Phase 1 deploy runbook.

### 1. Back up the current definitions
```bash
mkdir -p /root/retention-backup-$(date +%Y%m%d)
cd /root/retention-backup-$(date +%Y%m%d)
cp -a /etc/cron.d/cleanup-reports            ./cleanup-reports.cron            2>/dev/null || true
cp -a /etc/cron.d/nightwatch-reports-cleanup ./nightwatch-reports-cleanup.cron 2>/dev/null || true
cp -a /opt/shahrzad-devops/scripts/nightly-cleanup.sh ./nightly-cleanup.sh.bak 2>/dev/null || true
# also back up any existing live cleanup script + dedicated config (if present)
cp -a /opt/shahrzad-devops/scripts/cleanup-reports.sh       ./cleanup-reports.sh.bak       2>/dev/null || true
cp -a /opt/shahrzad-devops/configs/reports-cleanup.env      ./reports-cleanup.env.bak      2>/dev/null || true
```

### 2. Disable/remove ONLY the report-related conflicting cron entries
```bash
rm -f /etc/cron.d/cleanup-reports
rm -f /etc/cron.d/nightwatch-reports-cleanup
```
(Leave every non-report cron entry untouched.)

### 3. Remove ONLY the report-deletion lines from the nightly cleanup workflow
Edit `/opt/shahrzad-devops/scripts/nightly-cleanup.sh` and delete just the two
report `find … reports …` lines in step `[6/6]` (leave the rest of the script —
apt cleanup, etc. — intact). Retention is now owned by the timer.

### 4. Create the dedicated non-secret cleanup config
The service loads only this file — **never** the full bot `.env`. It holds no
secrets (the path token is read by the script from `reports-token.env`).
```bash
cat > /opt/shahrzad-devops/configs/reports-cleanup.env <<'EOF'
BOT_REPORT_RETENTION_DAYS=15
EOF
chmod 0644 /opt/shahrzad-devops/configs/reports-cleanup.env
```

### 5. Install the script to the STABLE live path, then the timer
Install from the immutable deploy worktree (`$DEPLOY_WT`, see
`PHASE1_DEPLOY_RUNBOOK.md`) — **not** from the repo checkout, which may be stale.
The systemd unit executes the stable live path, so the running timer never
depends on the checkout.
```bash
install -m 0755 "$DEPLOY_WT/scripts/cleanup-reports.sh" \
                /opt/shahrzad-devops/scripts/cleanup-reports.sh
cp "$DEPLOY_WT/systemd/reports-cleanup.service" /etc/systemd/system/
cp "$DEPLOY_WT/systemd/reports-cleanup.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now reports-cleanup.timer
# Confirm the unit resolves the stable live script:
systemctl show reports-cleanup.service -p ExecStart | grep -q /opt/shahrzad-devops/scripts/cleanup-reports.sh \
  && echo "ExecStart OK (stable live path)"
```

### 6. Dry-run once
```bash
/opt/shahrzad-devops/scripts/cleanup-reports.sh --dry-run
```
Confirm the printed counts look sane (removed_* small, retained_intact > 0).

### 7. Run once for real
```bash
systemctl start reports-cleanup.service
journalctl -u reports-cleanup.service -n 20 --no-pager
```

### 8. Verify an intact recent Summary/Browse/ZIP triplet
Pick a report dir created in the last day and confirm all three URLs return 200:
```bash
TOKEN=$(grep -E '^REPORTS_PATH_TOKEN=' /opt/shahrzad-devops/configs/reports-token.env | cut -d= -f2)
SLUG=$(ls -t /opt/shahrzad-devops/reports/$TOKEN | grep -v '\.zip$' | head -1)
for p in "$SLUG/summary.txt" "$SLUG/" "$SLUG.zip"; do
  echo "$(curl -s -o /dev/null -w '%{http_code}' "https://devops.shahrzad.ai/reports/$TOKEN/$p")  $p"
done
```

### 9. Verify expired hollow directories are removed
```bash
find /opt/shahrzad-devops/reports/$TOKEN -mindepth 1 -maxdepth 1 -type d -empty | wc -l   # expect 0
```

### 10. Rollback if validation fails
```bash
systemctl disable --now reports-cleanup.timer
rm -f /etc/systemd/system/reports-cleanup.timer /etc/systemd/system/reports-cleanup.service
systemctl daemon-reload
# restore the backed-up definitions from step 1:
cp /root/retention-backup-*/cleanup-reports.cron            /etc/cron.d/cleanup-reports            2>/dev/null || true
cp /root/retention-backup-*/nightwatch-reports-cleanup.cron /etc/cron.d/nightwatch-reports-cleanup 2>/dev/null || true
cp /root/retention-backup-*/nightly-cleanup.sh.bak          /opt/shahrzad-devops/scripts/nightly-cleanup.sh 2>/dev/null || true
# restore/remove the installed live cleanup script + dedicated config:
if [ -f /root/retention-backup-*/cleanup-reports.sh.bak ]; then
  cp /root/retention-backup-*/cleanup-reports.sh.bak /opt/shahrzad-devops/scripts/cleanup-reports.sh
else
  rm -f /opt/shahrzad-devops/scripts/cleanup-reports.sh   # was newly installed
fi
if [ -f /root/retention-backup-*/reports-cleanup.env.bak ]; then
  cp /root/retention-backup-*/reports-cleanup.env.bak /opt/shahrzad-devops/configs/reports-cleanup.env
else
  rm -f /opt/shahrzad-devops/configs/reports-cleanup.env  # was newly created
fi
```

## Note
The bot's in-code report generator now writes a **verified** summary + ZIP with a
collision-safe slug (`<name>-<timestamp>-<rand>`), so within the TTL all three
links resolve. Retention is the only thing that removes them, and it now has a
single owner.
