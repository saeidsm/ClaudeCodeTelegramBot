# MCP bridge — activation & rollback

The MCP surface is a thin JSON-RPC layer **over the existing Telegram bot
engine**, not a second engine. Every MCP tool call routes through the same
`_sessapi_*` console handlers the Telegram console uses (`_FakeReq`), so
session management, the execution queue, `MAX_RUNNING_AGENTS`, nonce
dedup, rate limit, label/project validation and the audit log are the
same objects — there is no parallel SessionManager and no second queue.

## What ships

- `mcp_bridge.py` — protocol layer + tools + worktree capture
- `bot.py` — `wire()` call, `/mcp/rpc` route, Bearer auth fallback,
  `starting_branch` persistence, `preserve_on_exit`, capture dir in ZIP
- `tests/test_mcp_bridge.py` — 17 offline tests (fake engines only)

## Tools

| tool | notes |
|---|---|
| `projects_list` / `models_list` | read-only |
| `sessions_list` | same sessions as Telegram |
| `session_new` | engine must be `claude`; project must be in `BOT_MCP_PROJECTS`; model alias recorded as requested; `claude_gateway.configured_models()` decides the effective model — unknown stays unknown |
| `send` | body rebuilt from whitelisted keys; `chat_id` never forwarded; POLICY_DENIED outside pilot projects |
| `status` | alias/effective model reported separately |
| `cancel` | queued-only. A running turn answers `cancel_unavailable` (409): TERM requires proven identity+ownership+process-end, which the bridge does not claim |
| `report` | same report pipeline as Telegram; the ZIP now carries `capture/` (status, diff, untracked files, sha256 manifest) |
| `ask` | Q&A (owner item 2/7): the question goes to an existing in-scope session, or one is created (identical gates to `session_new`) and the question is submitted to it — both through the same `_sessapi_send`/`_sessapi_control` seams. Answer arrives in the session output: poll `status`, build `report`. Caller nonce optional (generated clock/HWM-tracked when absent; a supplied nonce dedups an exact replay) |

Scope gate: **every** label-resolving tool (`send`/`status`/`cancel`/`report`)
refuses sessions whose project is outside `BOT_MCP_PROJECTS`
(POLICY_DENIED) **or whose engine is not `claude`** (ENGINE_UNAVAILABLE) —
a Codex session created via Telegram is visible but not drivable from MCP
(`run_codex` has no capture; driving it from MCP would re-open the exact
no-capture rm-rf this pilot closes).

`send` with the same `nonce` never runs twice — same `_console_duplicate`
high-water mark as the console. The bridge's own nonces (for `session_new`
and `ask`'s create step) track the wall clock AND the console HWM, so a
client that sent its own epoch-ms nonce can neither poison nor brick later
generated ones; the `nonce` schema says epoch-ms.

## Auth

No new key. `X-Session-API-Token` as today, plus standard
`Authorization: Bearer <same secret>`. `hmac.compare_digest` in both
cases; the Bearer parse never falls back to a prefix match. The session
API still binds to `172.17.0.1:9092`. Unauthorized callers get 403 before
any wiring/enablement state is observable; report summaries are scrubbed
with the same `_scrub_secrets` as `/output`.

## Activation (explicit, never automatic)

```sh
# systemd override for claude-telegram-bot / nikbot service:
Environment=BOT_MCP_ENABLED=1
# BOT_MCP_PROJECTS defaults to "Rag4Engineers" (comma-separated, ∩ registered)
```

`BOT_MCP_ENABLED` unset/0 → the route is not registered and Telegram
behaves exactly as before. Activation is a deploy decision, not a code
default.

## Rollback

```sh
# remove the override (or set BOT_MCP_ENABLED=0) and restart the bot service
```

Telegram paths are untouched by the bridge; a disabled bridge is
invisible to the Telegram engine. Captures live under
`$BOT_DATA_ROOT/captures/<safe-sid>/<stamp>/` and reports keep the
existing 15-day retention.

## Capture-before-cleanup

A dirty worktree is never `rm -rf`'d without a proven capture, on **both**
deletion paths:

- **exit** (`WorktreeSession._exit_sync`): `TurnCapture` runs
  `capture_worktree_delta` (status, diff, untracked copies, sha256 manifest)
  before removal; a capture that cannot be proven complete (error, timeout,
  unparsable path) sets `preserve_on_exit` — the tree stays on disk and the
  miss is logged (`worktree.preserved_dirty`).
- **stale re-enter** (`_enter_sync`, the idempotent cleanup of a crashed
  run's tree): the leftover tree is captured first. A proven/clean capture
  removes it as before; an unproven one **quarantines** it — moved to
  `captures/<sid>/stale-<stamp>/` (never deleted), `git worktree prune`
  clears the registration, and a failed quarantine raises instead of ever
  taking the destructive path.

Parsing is machine-readable (`status --porcelain=v1 -z`, `core.quotepath
off`, `ls-files -z`): quoted Persian paths and rename records parse into
real paths, and any record that cannot be saved (dead path, read error,
oversize untracked file) downgrades the capture to unproven, which triggers
preserve/quarantine — a broken record is never silently "clean".

Secret handling: a secret-looking name (`token.env`, `secrets.json`,
`credentials.yml`, `*.pem/*.key/*.age`, `.env*`, ssh keys…) is never copied,
and when one appears in the delta the whole `diff.patch` is skipped —
`git diff` output is a second door for tracked secret content, closing
after review. Captures keep a 15-day retention (same as reports), pruned
lazily per capture.

## Known limits (v1)

- Q&A is session-scoped: `ask` routes the question through a session (new
  or existing) — there is no stateless one-shot path in the bot, and none
  was invented for the pilot. The answer comes back via `status`/`report`,
  like any turn.
- Cancel of a *running* turn is unavailable by design (no TERM without
  proven process ownership).
- Resume binds to the same owner/session/branch/model via the existing
  `--resume` path; `starting_branch` is persisted so a restart cannot
  re-bind resume to a different branch.