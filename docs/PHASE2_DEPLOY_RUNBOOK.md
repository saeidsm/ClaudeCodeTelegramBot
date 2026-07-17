# Phase 2 Deploy Runbook — Engines (Claude / Codex / Chat)

**LOCKED.** Do not deploy until the owner sends the exact gate message:

```
GO PHASE2 DEPLOY
REVIEWED_PR=<number>
MERGED_SHA=<full 40-char SHA>
```

and `MERGED_SHA == origin/main`, the reviewed head tree equals the merge tree,
and the implementation-report gates passed. Otherwise **stop before any mutation**.

Deployment must install **every** new runtime module — not only `bot.py`.

## Runtime artifact manifest (must all be copied to the live script dir)

Live entrypoint dir: `/opt/shahrzad-devops/scripts/` (deployed as `claude-telegram-bot.py`).
The engines package + new modules must sit **next to** the entrypoint so
`import engines...`, `import coordination`, `import resource_footer` resolve.

| Source (repo) | Live destination |
|---|---|
| `bot.py` | `/opt/shahrzad-devops/scripts/claude-telegram-bot.py` |
| `engines/__init__.py` | `/opt/shahrzad-devops/scripts/engines/__init__.py` |
| `engines/base.py` | `.../scripts/engines/base.py` |
| `engines/claude_adapter.py` | `.../scripts/engines/claude_adapter.py` |
| `engines/codex_adapter.py` | `.../scripts/engines/codex_adapter.py` |
| `engines/openrouter_chat.py` | `.../scripts/engines/openrouter_chat.py` |
| `coordination.py` | `.../scripts/coordination.py` |
| `resource_footer.py` | `.../scripts/resource_footer.py` |
| `log_filters.py` (unchanged) | already live — verify hash |
| `video_module/` (unchanged) | already live — verify hash |

> `engines/__init__.py` must exist (empty is fine) so the package imports.
> If the live layout imports `bot` as a module elsewhere, the sibling modules
> and the `engines/` package must be on the same `sys.path` entry as `bot.py`.

## New writable paths (create least-privilege, root-owned)

| Path | Purpose | Default |
|---|---|---|
| `BOT_CHAT_SESSIONS_DIR` | bounded per-session Chat history | `/opt/shahrzad-devops/chat-sessions` |
| `BOT_COORDINATION_FILE` (+`.lock`) | shared Claude/Codex coordination store | `/opt/shahrzad-devops/configs/coordination.json` |
| OpenRouter catalog cache | last-known-good models | `<configs>/openrouter-catalog.json` |

## Env keys to add (non-secret; edit only reviewed keys, never expose secrets)

- `OPENROUTER_API_KEY` — **must already exist** in the live `.env`; do NOT modify it.
- Optional tunables (all have safe defaults; only set if reviewed):
  `BOT_CHAT_CONCURRENCY`, `BOT_CHAT_CATALOG_TTL`, `BOT_CHAT_FAVORITES`,
  `BOT_CHAT_HISTORY_MAX_TURNS`, `BOT_CHAT_HISTORY_MAX_CHARS`,
  `BOT_CODEX_DEFAULT_MODEL`, `BOT_CODEX_SANDBOX`, `BOT_CLAUDE_MODELS`,
  `BOT_CODEX_MODELS`, `BOT_CHAT_SESSIONS_DIR`, `BOT_COORDINATION_FILE`.

## Prerequisites verified at deploy time (booleans / catalogs, no secrets printed)

1. `codex login status` → "Logged in using ChatGPT" (Codex engine usable).
2. `codex debug models` returns Sol/Terra/Luna slugs.
3. `GET /api/v1/models` reachable with the existing key (favorites resolvable).
4. `claude models` catalog visible.
5. basic-memory / Graphify are **optional** — absence is non-fatal (adapters degrade).

## Validation before restart (immutable worktree at merged SHA)

```
python3 -m py_compile bot.py engines/*.py coordination.py resource_footer.py
python3 -m pytest -q                      # full suite green
git diff --check
```

Then a non-executing import smoke with the **live dir on sys.path**, temp state/logs,
verifying: three engines register; migration of legacy state → engine=claude;
engine catalogs load; OpenRouter catalog fetch (or last-known-good); no secret in logs.

## Restart + observation

Restart the bot **once**. Within 120s require: stable new PID, no fatal traceback,
`limits.effective max_sessions=9 ... max_running_agents=2`, dual NightWatch (if enabled),
`Bot v4 ready`, legacy sessions migrated to `engine=claude`, healthy registry/catalogs,
no OOM increase. Observe ≥90s.

## Rollback (on any failure)

Restore from the unique root-only backup created before mutation:

```
# 1. stop new bot
systemctl stop claude-telegram-bot
# 2. restore entrypoint + modules + .env + configs from backup manifest
cp -a <backup>/claude-telegram-bot.py /opt/shahrzad-devops/scripts/
cp -a <backup>/engines /opt/shahrzad-devops/scripts/
cp -a <backup>/coordination.py <backup>/resource_footer.py /opt/shahrzad-devops/scripts/
cp -a <backup>/.env /opt/shahrzad-devops/.env
# 3. remove Phase-2-created paths if they did not PRESENT_BEFORE
#    (chat-sessions/, coordination.json, openrouter-catalog.json)
# 4. restart old bot once and verify PID/ready
systemctl start claude-telegram-bot
```

Record `DEPLOYED` / `ROLLED BACK` / `STOPPED BEFORE MUTATION` in
`/tmp/phase2-autonomous-deploy-<shortsha>.md`. Retain backups + worktree.

## basic-memory MCP (optional, transactional)

Only if reviewed: back up Claude/Codex CLI configs, add the recon-verified
streamable-HTTP MCP entry transactionally, preserve auth/unrelated config, then
validate the handshake. Never write secrets/transcripts into memory.
