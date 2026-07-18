# Combined Phase-1 + Phase-2 Deployment Runbook (Engines: Claude / Codex / Chat)

**DOCUMENTATION ONLY. Do NOT execute in this task.** Deployment is locked behind
the architect review + merged-SHA gate. This runbook is written as one
transactional, autonomous procedure that installs **both** Phase 1 (if the live
box was rolled back) and Phase 2 in a single backup-protected transaction.

> Why combined: the earlier Phase-1 deploy was **rolled back**, so production may
> still be running the *old* bot with `BOT_MAX_SESSIONS=6` and the three
> conflicting report-retention cron paths. A Phase-2-only runbook that assumes
> Phase 1 is live is unsafe. Audit first; install what is missing.

---

## Executable procedure (do not hand-type these steps)

Every step in this runbook is implemented as ONE transaction by the
repository-owned, offline-tested script — so deployment is an exact procedure,
not commands invented at deploy time (the cause of the prior deploy loops):

```
scripts/deploy-phase1-phase2.sh          # THE procedure — run this, do not improvise
tests/test_deploy_script.py              # offline proof of every gate + rollback
```

Invoke:

```
scripts/deploy-phase1-phase2.sh --preflight --merged-sha <40-hex> --source <detached-wt>
scripts/deploy-phase1-phase2.sh --execute --reviewed-pr 19 \
    --reviewed-head <owner-authorized PR-head 40-hex> \
    --reviewed-tree <owner-authorized tree 40-hex> \
    --merged-sha <40-hex> --source <detached-wt>
```

The reviewed head/tree come from the owner's review record of PR #19
(`git rev-parse <reviewed-head>` / `git rev-parse <reviewed-head>^{tree}` at
review time) — they are the authorization, not something derived at deploy time.

The script performs, in order, exactly:

1. **One fail-closed, non-executing `.env` parse into `/tmp` scratch** — never
   `source`/eval. A missing `python-dotenv`, unreadable/symlinked/malformed file,
   or a **duplicate safety-critical key** (`export KEY=` and `KEY=` count as the
   SAME key) stops before any mutation with an explicit reason. Every later
   consumer (path resolution, listeners, health, rollback) reuses this single
   validated result.
2. **Read-only preflight (both modes), writing only under `/tmp` and NEVER
   mutating Git metadata** — no lock, no mutation, no `__pycache__`/`.pytest_cache`
   in the source (bytecode disabled, caches + basetemp under a `/tmp` scratch
   dir), `GIT_OPTIONAL_LOCKS=0` so `git status` never writes the index:
   - **source gate:** detached clean worktree at the exact merged SHA; a
     **read-only** `git ls-remote origin refs/heads/main` (never `fetch`) must
     return exactly the merged SHA (fail-closed on stale/missing/failure);
   - **suite gate:** `py_compile`, `bash -n`, and the complete offline suite from
     the immutable source (temp cache/basetemp); optional documented total;
   - **built-in readiness gates (never print secrets):**
     * service active; **zero** running/queued sessions from the resolved state;
     * **capacity** — disk **and RAM and swap** against **hard-coded production
       minima** (500/150/64 MB — `DEPLOY_MIN_*` overrides are refused outside the
       test harness), plus a **mandatory** cgroup gate: an unresolvable
       `ControlGroup` or unreadable `memory.events` fails closed; `oom_kill` must
       be exactly 0;
     * **service user** — the systemd `User` must EXIST (`id` lookup; **no UID-0
       fallback**); root probes run directly, non-root probes require proven
       noninteractive `sudo -u`;
     * **capability contracts (content, not just exit status):** Claude `models`
       must list the adapter's selectable ids; `codex login status` must say
       logged-in; `codex debug models` must contain Sol/Terra/Luna (or the
       configured override ids); `codex exec --help` must prove `--json`;
       `codex exec resume --help` must prove resume-by-session-id;
     * **listeners** — the EXACT `bind:port` endpoints from the validated env
       (wildcard/wrong-IP/missing endpoints rejected), socket evidence bound to
       the expected service PID where the platform reports it, plus the bot's
       own `/healthz` contract (`"ok": true`) so an unrelated listener can never
       satisfy readiness;
     * **OpenRouter** — authenticated HTTP 200 **and** a minimally valid catalog
       payload (`data` non-empty); the key/headers/bodies are never printed;
     * the report-cleanup **dry-run** (no `|| true`); `basic-memory`/Graphify
       reported as optional follow-ups (NOT integrated).
   - In production mode (no `--test-root`) every test-seam variable is refused.
3. **Path resolution** from the validated parse (`BOT_DATA_ROOT`/`BOT_CONFIGS_DIR`/
   `BOT_CHAT_SESSIONS_DIR`/`BOT_COORDINATION_FILE`/`BOT_CHAT_CATALOG_CACHE`),
   contained inside the live root (fail-closed on escape/symlink).
4. **Execute-only — reviewed-tree AUTHORIZATION gate (squash/merge-safe):**
   require `--reviewed-pr 19` **and** the owner-authorized `--reviewed-head` +
   `--reviewed-tree` (40-hex each). The merged source tree
   (`git rev-parse <MERGED_SHA>^{tree}`) must equal the reviewed TREE exactly —
   commit-SHA equality is insufficient under squash merge. If the PR head branch
   still exists remotely, a read-only `ls-remote` must show it equal to the
   reviewed head (stale branch fails); if the branch was deleted after merge,
   the reviewed-tree equality is the decisive immutable gate (documented case).
   The reviewed head/tree and merged tree are recorded in the transaction
   report. Then acquire the single deploy **flock**.
4b. **Quiescence boundary (before ANY file mutation):** re-verify zero
   running/queued sessions from the STRICT state read (a missing/unreadable/
   malformed state file or unknown status now fails closed — never "0"), then
   `systemctl stop` the bot under the already-installed recovery trap, verify
   the unit is inactive, and re-read the authoritative state — a turn admitted
   during shutdown aborts before any mutation with its state preserved. Only
   then is the state backup captured, so rollback can never restore a stale
   copy over newer session state. The bot is down for the whole mutation
   window, so no turn can be admitted or execute under mixed code; success
   performs exactly ONE old→new transition (`start`, not restart); a
   pre-mutation failure restarts the old bot (availability restore, files
   untouched); no restart loops.
5. **Manifest + backup** with full identity per entry — state, type, owner,
   group, mode, and content hash / **full tree manifest** (path+type+bytes+
   symlink target+mode+empty dirs) / link target — for every artifact and the
   resolved live data, plus the timer's prior enabled/active state.
6. **Install** from the detached source: file hashes and **full tree manifests**
   verified source-vs-installed (extra/missing/type/mode/link-target changes all
   fail; escaping symlinks rejected); the retention **service unit is RENDERED**
   to the resolved `EnvironmentFile`/`ExecStart`/`REPORTS_ROOT_GUARD` paths and
   verified; the installed timer must be **non-persistent** (a `Persistent=true`
   catch-up could fire real cleanup inside the transaction); runtime dirs AND
   every existing mutable artifact (coordination store/lock, catalog cache,
   state) are `chown`ed to the service user — any `chown`/`chmod` failure aborts.
7. **`.env` secure atomic edit** (allow-list only): unpredictable temp file with
   `O_CREAT|O_EXCL|O_NOFOLLOW` in the destination directory, owner/group/mode
   preserved or the edit fails, fsync of file and directory, symlinked targets
   refused, duplicates (incl. `export` form) refused. Preserves every unrelated
   byte and newline style (incl. CRLF); the ONLY documented structural change is
   that appending a key to a file lacking a final newline first terminates the
   last line. The nightly-cleanup surgical editor follows the same contract.
8. **All three retention migrations** (both cron files + surgical nightly edit,
   fail-closed on entangled deletions); canonical config written to the SAME
   resolved path the rendered unit reads; cleanup runs **only** `--dry-run`.
9. **Verify** the live dir (import closure + offline `FooterBot`/`Application`
   construction) and probe **read/write/ATOMIC-REPLACE as the service user** in
   the chat-history, coordination, and catalog-cache directories; existing
   mutable files must be service-replaceable.
10. **Start the new bot exactly once** (the single transition begun by the
    quiescence stop). Capturing the pre-start journal cursor is
    MANDATORY (empty cursor = pre-restart failure; no stale-journal fallback).
    **Health polls** for a bounded window (60 s production, 2 s interval) and
    reads ONLY `journalctl -u <unit> _PID=<new MainPID> --after-cursor=<cursor>`:
    ready marker, 9/2 limits, **exactly** `chat,claude,codex`, exact listeners
    (new PID), stable cgroup, service-user-writable paths. A stale ready line,
    old traceback, missing/extra engine, or PID churn cannot pass. **`observe`**
    (90 s production) repeats the full assertion with the SAME PID.
11. **Only at the final commit boundary, after observation,** verify the rendered
    unit paths + installed script hash + root guard + non-persistent timer, then
    enable + start `reports-cleanup.timer` and require enabled + active + a
    **nonempty** exact `ExecStart`.
12. Any post-mutation failure → an ordered, **VERIFIED** rollback: stop any newly
    activated timer; restore all entries; **verify every entry** against its
    recorded type/hash/tree/target/mode/owner/group (symlinks: target AND
    owner/group) and every absent-before removal; assert no editor temp files
    remain; `daemon-reload`; restore the prior timer state; restart the old bot
    and require it active with a MainPID and the **ORIGINAL pre-deploy listener
    endpoints** (an immutable snapshot taken at the single env parse — never the
    forward values the deploy wrote, including absent/different/disabled BIND2).
    All errors accumulate: `ROLLED_BACK` only when every restore and
    verification passes, otherwise `ROLLBACK_FAILED` (with the failed steps and
    backup path). The forward exit code is preserved.

The remaining sections document acceptance evidence to confirm afterwards.

---

## Gate 0 — do not mutate until all true

```
GO PHASE2 DEPLOY
REVIEWED_PR=19
MERGED_SHA=<full 40-char SHA>
```

- PR #19 is **merged**; `MERGED_SHA == origin/main` (fetch read-only to confirm).
- The reviewed head tree equals the merge tree (supports **squash** merge: compare
  `git rev-parse <MERGED_SHA>^{tree}` against the reviewed tree, not the commit).
- The implementation-report gates passed.

If any is false → **STOP BEFORE MUTATION**, write `STOPPED BEFORE MUTATION`.

## Gate 1 — immutable deployment worktree

Create a detached worktree at the merged SHA; never deploy from the dirty main
checkout:

```
git worktree add --detach /tmp/phase2-deploy-<shortsha> <MERGED_SHA>
```

Run all gates here: `py_compile bot.py engines/*.py coordination.py resource_footer.py`,
full `pytest` green, `git diff --check`, `bash -n scripts/*.sh`, isolated import
smoke, secret scan. Abort on any failure.

## Gate 2 — audit live Phase-1 state (read-only)

Determine the **service user** from systemd and record, without printing secrets:

```
systemctl show claude-telegram-bot -p MainPID -p ActiveEnterTimestamp -p User -p ExecStart
```

Audit and record `PRESENT_BEFORE` (present/absent + hash) for each of:

- Live entrypoint `/opt/shahrzad-devops/scripts/claude-telegram-bot.py`.
- Phase-2 modules/package: `engines/` (+`__init__.py`), `coordination.py`,
  `resource_footer.py`, `scripts/coord_publish.py`.
- `/opt/shahrzad-devops/.env`; bot state `configs/bot-state.json`.
- Chat data dir (`chat-sessions/`), coordination store (`configs/coordination.json`
  + `.lock`), OpenRouter catalog cache (`configs/openrouter-catalog.json`).
- Report retention: `scripts/cleanup-reports.sh`, its config, the systemd
  `reports-cleanup.service` + `.timer`, **both** report cron files, and the
  nightly cleanup — the **three conflicting retention paths** noted in project
  memory. Record each so rollback can restore the exact prior state.
- Service unit file + drop-ins; owners/modes/hashes of everything above.

If Phase 1 is absent/partial, the SAME transaction must also install the Phase-1
env keys and retention fix:

```
BOT_MAX_SESSIONS=9
BOT_MAX_RUNNING_AGENTS=2
BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4     # VPC private second listener
```
…plus the single authoritative report cleanup script/config/systemd timer, and
**removal of exactly the three conflicting retention paths** — all inside the
one backup-protected transaction.

## Gate 3 — capability verification as the service user (no secrets printed)

Run as the exact service user determined in Gate 2:

- `codex login status` → must be logged in (ChatGPT). `codex debug models` → Sol/
  Terra/Luna present. `codex exec --help` / `codex exec resume --help` → the
  `--json` + `resume <SESSION_ID>` contract still holds.
- `claude models` → catalog visible.
- OpenRouter reachable (`GET /api/v1/models`) with the **existing** key.
  **A missing/invalid `OPENROUTER_API_KEY` is a pre-mutation NO-GO** — never
  invent or edit the secret.
- basic-memory / Graphify are OPTIONAL — absence is non-fatal.

## Gate 4 — pre-mutation safety

- Parse `.env` **non-executingly**; modify only explicitly allow-listed non-secret
  keys, preserving every unrelated line and secret byte-for-byte.
- Require **zero running/queued sessions**, capacity/cgroup headroom, both
  NightWatch listeners healthy (if enabled), a clean test run, and a report
  retention **dry-run** (no real deletion) before any mutation.
- Acquire a single deployment flock.

## Backup + manifest (before ANY write)

Create a unique root-only backup dir; copy every artifact from Gate 2 with its
`PRESENT_BEFORE` state, owners, modes, hashes, and the exact rollback commands.
Snapshot the service unit + `MainPID`/timestamp.

## Install (from the immutable worktree only)

Copy every runtime dependency to stable live paths and verify each:

| Artifact | Live destination |
|---|---|
| `bot.py` | `/opt/shahrzad-devops/scripts/claude-telegram-bot.py` |
| `engines/` (incl. `__init__.py`) | `.../scripts/engines/` |
| `coordination.py`, `resource_footer.py` | `.../scripts/` |
| `scripts/coord_publish.py` | `.../scripts/coord_publish.py` |

Verify: hashes match source, owner/mode correct, **import closure** resolves with
the **live dir on `sys.path`**, `aiohttp` importable, and the `engines` package
resolves next to `bot.py`. Create `chat-sessions/`, `configs/coordination.json`
parent, and catalog-cache dir least-privilege (service-user owned, `0700`/`0600`).
Configure basic-memory only with verified syntax + backup + handshake (optional).

## Restart exactly once, then verify (≤120s)

Restart the service **once** after all pre-restart gates. Require:

- stable new PID (old PID unchanged until this final step); no loop/fatal traceback;
- `limits.effective max_sessions=9 ... max_running_agents=2`;
- three engine registrations (claude/codex/chat);
- legacy sessions migrated to `engine=claude`; Claude UUID/resume preserved;
- Codex thread capture/resume readiness; Chat catalog/favorites loaded; Chat
  history containment intact; independent Chat cap; coordination store writable;
- both NightWatch listeners; report triplet (Summary/Browse/ZIP) working; footer
  present on a test reply; cgroup/OOM stable; retention timer active.
- Observe ≥ 90 seconds. **No real report cleanup runs during the transaction.**

## Rollback (any post-mutation failure)

Automatically, per the manifest: restore/remove each artifact to its
`PRESENT_BEFORE` state (entrypoint, Phase-2 modules/package, `.env`, state, Chat +
coordination data, catalog cache, retention script/config/units, **both** report
cron files, nightly cleanup, service unit), `systemctl daemon-reload`, restart the
**old** bot once, and prove old hashes/listeners/sessions/retention state are
healthy. Newly-created paths that were `PRESENT_BEFORE=absent` (chat-sessions/,
coordination.json, openrouter-catalog.json) are removed. Retain the backup + worktree.

## Final report

Write exactly one of `DEPLOYED` / `ROLLED BACK` / `STOPPED BEFORE MUTATION` to
`/tmp/phase2-autonomous-deploy-<shortsha>.md` with evidence, hashes, backup path,
and rollback commands.

## New runtime env keys (non-secret; all have safe defaults)

`BOT_CHAT_SESSIONS_DIR`, `BOT_COORDINATION_FILE`, `BOT_CHAT_CATALOG_TTL`,
`BOT_CHAT_CONCURRENCY`, `BOT_CHAT_FAVORITES`, `BOT_CHAT_HISTORY_MAX_TURNS`,
`BOT_CHAT_HISTORY_MAX_CHARS`, `BOT_CODEX_DEFAULT_MODEL`, `BOT_CODEX_SANDBOX`,
`BOT_CLAUDE_MODELS`, `BOT_CODEX_MODELS`. `OPENROUTER_API_KEY` must already exist
and is never modified.
