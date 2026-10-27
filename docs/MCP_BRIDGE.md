# MCP bridge — activation & rollback

The MCP surface is a thin JSON-RPC layer **over the existing Telegram bot
engine**, not a second engine. Every MCP tool call routes through the same
`_sessapi_*` console handlers the Telegram console uses (`_FakeReq`), so
session management, the execution queue, `MAX_RUNNING_AGENTS`, nonce
dedup, rate limit, label/project validation and the audit log are the
same objects — there is no parallel SessionManager and no second queue.

## What ships

- `mcp_bridge.py` — protocol layer + tools + worktree capture + quarantine
- `bot.py` — `wire()` call, `/mcp/rpc` route, Bearer auth fallback,
  `starting_branch` persistence, persistent per-session worktree,
  `preserve_on_exit`, capture dir in ZIP
- `tests/test_mcp_bridge.py` — 36 offline tests (fake engines only)

## Tools

| tool | notes |
|---|---|
| `projects_list` / `models_list` | read-only; tested through the real `dispatch_rpc` |
| `sessions_list` | same sessions as Telegram |
| `session_new` | engine must be `claude`; project must be in `BOT_MCP_PROJECTS`; model alias recorded as requested; `claude_gateway.configured_models()` decides the effective model — unknown stays unknown |
| `send` | body rebuilt from whitelisted keys; `chat_id` never forwarded; POLICY_DENIED outside pilot projects |
| `status` | alias/effective model reported separately; lists `pending_questions` (the awaited question ids of the session) |
| `cancel` | queued-only. A running turn answers `cancel_unavailable` (409): TERM requires proven identity+ownership+process-end, which the bridge does not claim |
| `report` | same report pipeline as Telegram; the ZIP now carries `capture/` (status, diff, untracked files, sha256 manifest) |
| `ask` | Q&A (owner items 2/7 + 6): the question goes to an existing in-scope session, or one is created (identical gates to `session_new`) and the question is submitted to it — both through the same `_sessapi_send`/`_sessapi_control` seams. **Caller nonce required** — with no session yet it dedups the whole ask (create consumes it, the send leg uses a generated nonce strictly above the HWM it raised, so an internal nonce can never invalidate a valid request); with a session it dedups the send. A successful ask returns a `question_id`. The real 3-second console window applies verbatim (an honest 429, never a bypass) |
| `answer` | the runner's ANSWER to an awaited question: bound to the SAME `question_id` and submitted into the SAME session (that is how the session continues); one answer per question; a question of session A cannot be answered via session B's label (`unknown_question`); the binding is in-memory, so a bot restart refuses old ids (`unknown_question`) while the session itself still resumes by label. A plain `send` is NOT a substitute — it never answers a question |

Scope gate: **every** label-resolving tool (`send`/`status`/`cancel`/`report`/`answer`)
refuses sessions whose project is outside `BOT_MCP_PROJECTS`
(POLICY_DENIED) **or whose engine is not `claude`** (ENGINE_UNAVAILABLE) —
a Codex session created via Telegram is visible but not drivable from MCP
(`run_codex` has no capture; driving it from MCP would re-open the exact
no-capture rm-rf this pilot closes).

`send` with the same `nonce` never runs twice — same `_console_duplicate`
high-water mark as the console. The bridge's own nonces (for `session_new`
and `ask`'s create step) track the wall clock AND the console HWM, and
every generated value — the FIRST included — is STRICTLY above both: a
first value landing exactly on the floor was refused as a duplicate of the
caller's own nonce (owner-found bug, fixed + regression-tested).

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
# BOT_WORKTREE_REUSE=0 restores fresh-tree-per-spawn (delete on exit)
```

Telegram paths are untouched by the bridge; a disabled bridge is
invisible to the Telegram engine. Captures live under
`$BOT_DATA_ROOT/captures/<safe-sid>/<stamp>/`, quarantines under
`$BOT_DATA_ROOT/quarantine/<safe-sid>/stale-<stamp>/` (own root — see
below). Capture auto-deletion is **OFF by default**
(`BOT_MCP_CAPTURE_RETENTION_DAYS`, 0 = keep everything); reports keep the
existing 15-day retention.

## Worktrees: persistent per session (owner R-revisit item 4)

A session's worktree at `WORKTREE_ROOT/<session_id>` now **persists across
that session's turns**: a resume (`send`/`ask`/`answer` on the same label)
re-enters the SAME tree with the SAME commit and the SAME uncommitted
changes — no fetch, no rebuild from `origin/<branch>`, and **no fallback
to main** (a session bound to a branch that vanished from origin fails
loud). Only the first spawn of a session builds from origin; a DIFFERENT
session still picks work up via commit+push as before.
`BOT_WORKTREE_REUSE=0` restores the old fresh-tree-per-spawn behavior.

Deletion now happens only where it always was guarded:

- **stale re-enter** (`_enter_sync`, a crashed run's leftover tree that
  cannot be reused): capture first. A proven/clean capture removes it; an
  unproven one **quarantines** it (see below).
- **`claude-worktree-gc`** (8-day orphan sweep): a dirty tree is quarantined
  (with its git index), never `rm -rf`'d; a tree that cannot be proven clean
  or quarantined is skipped.

## Capture-before-cleanup

A dirty worktree is never `rm -rf`'d without a proven capture, on every
deletion path that still exists:

- **exit** (`WorktreeSession._exit_sync`): `TurnCapture` runs
  `capture_worktree_delta` (status, diff, untracked copies, sha256 manifest)
  before removal; a capture that cannot be proven complete (error, timeout,
  unparsable path) sets `preserve_on_exit` — the tree stays on disk and the
  miss is logged (`worktree.preserved_dirty`). With reuse on (default) the
  tree is kept for the next turn anyway and nothing is deleted.
- **stale re-enter**: the leftover tree is captured first. A proven/clean
  capture removes it as before; an unproven one **quarantines** it — moved
  to `quarantine/<sid>/stale-<stamp>/` (never deleted), `git worktree prune`
  clears the registration, and a failed quarantine raises instead of ever
  taking the destructive path.

**Quarantine ≠ capture ≠ report (owner R-revisit item 2).** The quarantine
root is deliberately OUTSIDE the capture tree, and the report ZIP's only
capture source (`latest_capture_dir`) accepts capture-STAMP dirs only — a
quarantined tree can never ride into a report. The quarantine also carries
`git-meta/` — a copy of `<project>/.git/worktrees/<name>/` taken BEFORE the
prune deletes it — so the git **index** survives beside the dirty files
(item 1); it is staged to a sibling dir first because `copytree` into the
destination would create it and turn the move into a nest.

Parsing is machine-readable (`status --porcelain=v1 -z`, `core.quotepath
off`, `ls-files -z`): quoted Persian paths and rename records parse into
real paths, and any record that cannot be saved (dead path, read error,
oversize untracked file) downgrades the capture to unproven, which triggers
preserve/quarantine — a broken record is never silently "clean".

Secret handling: a secret-looking name (`token.env`, `secrets.json`,
`credentials.yml`, `*.pem/*.key/*.age`, `.env*`, ssh keys…) is never copied,
and when one appears in the delta the whole `diff.patch` is skipped —
`git diff` output is a second door for tracked secret content, closing
after review.

## Known limits (v1)

- Q&A is session-scoped: `ask` routes the question through a session (new
  or existing) — there is no stateless one-shot path in the bot, and none
  was invented for the pilot. The runner's answer comes back via `answer`
  (bound to the question id), and everything lands in `status`/`report`
  like any turn.
- Cancel of a *running* turn is unavailable by design (no TERM without
  proven process ownership).
- Resume binds to the same owner/session/branch/model via the existing
  `--resume` path; `starting_branch` is persisted so a restart cannot
  re-bind resume to a different branch.
- The `answer`→question binding is in-memory: a bot restart drops it (the
  session and its transcript still resume by label); `ask` after a restart
  issues a fresh question id.