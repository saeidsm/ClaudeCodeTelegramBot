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
scripts/deploy-phase1-phase2.sh --execute  --reviewed-pr 19 --merged-sha <40-hex> --source <detached-wt>
```

The script performs, in order, exactly:

1. **Read-only preflight (both modes), writing only under `/tmp` and NEVER
   mutating Git metadata** — no lock, no mutation, no `__pycache__`/`.pytest_cache`
   in the source (bytecode disabled, caches + basetemp under a `/tmp` scratch
   dir), `GIT_OPTIONAL_LOCKS=0` so `git status` never writes the index:
   - **source gate:** the source is a **detached** worktree (no branch), fully
     clean (no staged/unstaged/untracked), its HEAD equals the 40-hex merged
     SHA, and a **read-only** `git ls-remote origin refs/heads/main` (never a
     `fetch` — no local ref/object update) returns exactly the merged SHA
     (fail-closed on stale/missing/failed ls-remote);
   - **suite gate:** `py_compile` all runtime modules, `bash -n` all shell
     scripts, and the complete offline suite from the immutable source
     (`pytest -p no:cacheprovider`, temp cache/basetemp); optional documented total;
   - **built-in readiness gates (records, never prints secrets):** bot service
     active (capture unit/user/PID); **zero** running/queued sessions from the
     resolved state file; **capacity** — disk/RAM thresholds + cgroup
     `memory.events` `oom_kill==0`; **listeners** — NightWatch primary + `BIND2`
     from the non-executingly parsed env; **OpenRouter** — an *authenticated*
     read-only key check (HTTP 200; nonempty alone is insufficient; key never
     printed); **service-user** — the exact systemd `User`, running Claude
     `models`, `codex login status` / `debug models` / `exec --help` / `exec
     resume --help` **as that user**, plus PTB/aiohttp/dotenv + module imports;
     the report-cleanup **dry-run** (no `|| true`). Each gate has a real built-in;
     a test seam may replace it ONLY under `--test-root`. `basic-memory`/Graphify
     are inspected and reported as **optional follow-ups (not integrated)**.
   - In production mode (no `--test-root`) every test-seam variable is **refused**.
2. **Path resolution:** the exact state / chat-history / coordination(+`.lock`) /
   OpenRouter-cache / reports-token+config paths the running bot uses are resolved
   from the **non-executed** live env (`BOT_DATA_ROOT`/`BOT_CONFIGS_DIR`/
   `BOT_CHAT_SESSIONS_DIR`/`BOT_COORDINATION_FILE`/`BOT_CHAT_CATALOG_CACHE`),
   validated to stay inside the live root (fail-closed on escape/symlink).
3. **Execute-only, after all read-only gates:** require `--reviewed-pr 19`, then
   acquire the single deploy **flock**.
4. **Manifest + backup** of every artifact and the RESOLVED live data
   (state/type/owner/group/mode/hash; recursive tree hash for package dirs):
   runtime artifacts/package, `.env`, resolved `bot-state.json`, coordination
   store + `.lock`, OpenRouter cache, chat-history root, cleanup config/**both**
   systemd units, **both** conflicting crons, `nightly-cleanup.sh`, and the
   timer's prior enabled/active state.
5. **Install** from the detached source: files hash-verified, `engines/` and
   `video_module/` **recursive tree-hash** verified source-vs-installed; writable
   runtime dirs `chown`ed to the **service user** and writability tested **as
   that user** (fail on any `chown`/`chmod` error — no `|| true`); executable
   helpers kept root/service-owned for `_valid_coord_publisher`.
6. **`.env`** binary-safe, atomic, fsync-backed allow-list edit: `BOT_MAX_SESSIONS=9`,
   `BOT_MAX_RUNNING_AGENTS=2`, `BOT_NIGHTWATCH_IPC_BIND2=10.108.0.4`, resolved
   Phase-2 paths (chat sessions, coordination store, catalog cache, absolute
   coordination publisher). Changes only the exact records; preserves every other
   byte, newline style (incl. CRLF), comments, quoting and final-newline state;
   **fails closed on a duplicate allow-listed key**.
7. **All three retention migrations:** remove `/etc/cron.d/cleanup-reports` and
   `/etc/cron.d/nightwatch-reports-cleanup`, and **surgically** (byte-preserving,
   Python) comment only the report-deletion command(s) inside `nightly-cleanup.sh`
   (fail closed if a deletion is entangled with `&&`/`||`/`;`/continuation);
   install the canonical cleanup config and require its **dry-run** — no real
   deletion runs during the transaction.
8. **Verify** the live directory: compile/import closure + real offline
   `FooterBot`/`Application` construction with the live dir on `sys.path`.
9. **Restart exactly once** (after re-checking the busy-session gate; a journal
   cursor is captured first). **Health uses ONLY the new process's evidence**
   (`journalctl --after-cursor` from the restart window): stable **new** PID, no
   fatal traceback, `Bot v4 ready`, effective limits 9/2, exactly three engines
   from the real `engines.registered names=…` line, both listeners, writable
   paths **as the service user**, stable cgroup/OOM. **`observe`** repeats the
   full health assertion and requires the SAME new PID (no churn).
10. **Only at the final commit boundary, after observation,** enable + start
    `reports-cleanup.timer`; verify enabled + active + a **nonempty** exact stable
    `ExecStart` (no empty-output bypass).
11. Any post-mutation failure → an ordered, **verified** rollback: stop any newly
    activated timer, restore files/content/metadata + remove absent-before,
    `daemon-reload`, restore the prior timer enabled/active state, restart the old
    bot and re-check it is active with healthy listeners. Every rollback error is
    accumulated: `ROLLED_BACK` only if ALL restores/verifications pass, else
    `ROLLBACK_FAILED` (with the failed steps + backup path). The original forward
    exit code is preserved. Success → `DEPLOYED`; pre-mutation stop →
    `STOPPED_BEFORE_MUTATION`.

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
