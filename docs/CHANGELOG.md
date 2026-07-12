# Changelog

All notable changes to the Shahrzad DevOps Telegram Bot.

## [Unreleased] — 2026-07-12 — Phase 1: Stability Foundation

Production remains **Claude-only** after this phase. Codex, OpenRouter Chat mode,
engine selection, cross-agent coordination, persistent checkpoints, memory
migration, Graphify integration, and resource footers are deferred to later phases.

### Added
- **Bounded execution concurrency.** Two distinct limits:
  `BOT_MAX_SESSIONS` (logical/restorable sessions per chat, default now 9 in prod)
  vs `BOT_MAX_RUNNING_AGENTS` (simultaneous heavy Claude runs bot-wide, default 2).
  A process-wide semaphore caps concurrent executions; the rest show **`queued`**
  with a "waiting for a free slot (N/max)" progress line.
- **Per-session serialization.** One async lock per session — two turns for the
  same session never run concurrently (protects the worktree and `--resume`
  state); different sessions still overlap up to the global limit.
- **Cancellable queued work.** `/kill` (and graceful shutdown) now cancel a
  session's queued/in-flight execution tasks — a queued turn for a killed session
  never starts later. Locks/permits always release on success, error, timeout, or
  cancellation.
- **`BOT_NIGHTWATCH_IPC_BIND2`** — optional, validated second NightWatch IPC
  listener (loopback/private only; wildcard/public/multicast/invalid rejected).
  Empty by default. Backports a live-only prod capability into Git.
- **Canonical 15-day report retention** — `scripts/cleanup-reports.sh` +
  `systemd/reports-cleanup.{service,timer}`, replacing three conflicting cleanup
  jobs. See `docs/REPORT_RETENTION_MIGRATION.md`. Override via
  `BOT_REPORT_RETENTION_DAYS`.
- Focused test suites for reports, concurrency, NightWatch bind, session config,
  the cleanup script, and non-blocking behavior.

### Changed
- **Report generation** rewritten: Python `zipfile` (no unchecked `zip`
  subprocess), UTF-8 summary, collision-safe slug (`name-timestamp-rand`), ZIP
  built to a temp path and atomically renamed only after it verifies non-empty +
  openable + containing `summary.txt`. Never returns a phantom ZIP link. `fmt_links`
  now renders deliberate clickable HTML anchors (with the raw URL as fallback).
- **Event-loop unblocking**: report archiving, health/log scripts, deploy scripts
  (incl. the 15-min ZK4 path), remote-branch listing, worktree create/fetch/remove,
  and attachment copy now run off-loop (`asyncio.to_thread`), so one session's I/O
  no longer freezes the whole bot. Timeouts, capture, return codes, and
  process-tree cleanup are preserved.
- Added a process-local async deploy lock (prod-side `deploy.sh` flock remains
  authoritative). Deploy auth/confirmation behavior unchanged.
- Stale "max 3" help text now derives from `MAX_SESSIONS`.
- Config parsing for the limits is defensive (invalid/non-positive → safe default;
  effective values logged at startup).

## [Unreleased] — 2026-05-06 — Phase B + Multi-bot + Model selection

### Added
- **`/video`** command (and `/assets`, `/upload_music`, `/renders`):
  Telegram-driven Remotion render pipeline producing brand promo videos with
  optional ElevenLabs Persian narration. Two pre-built templates
  (ProductPromo, ServicePromo) × two aspect ratios (9:16, 16:9). Per-brand
  assets under `/tmp/remotion-assets/<Brand>/`.
  See `docs/VIDEO_BOT.md` and `docs/VIDEO_BOT_SETUP.md`.
- Per-session ephemeral git worktrees (eliminates cross-session contamination
  on shared projects)
- `BOT_DEFAULT_CLAUDE_MODEL` env var for per-bot default model
- Per-session `claude_model` field; `/model` UI redesigned with two sections
  (Claude Code primary | Fallback chain)
- Multi-bot support: env-driven paths (`BOT_DATA_ROOT`, `BOT_REPOS`,
  `BOT_REPORTS`, `BOT_LOGS`, `BOT_CONFIGS_DIR`, `BOT_UPLOADS`,
  `BOT_WORKTREE_ROOT`) and feature flags (`BOT_DEPLOY_ENABLED`,
  `BOT_NIGHTWATCH_IPC_ENABLED`, `BOT_MAX_SESSIONS`) so a second bot instance
  can run isolated on the same host
- `SETUP.md` at repo root: single-bot installation, multi-bot deployment,
  configuration reference, operational tasks, troubleshooting,
  architecture notes

### Changed
- `/cont` menu description tightened to `Continue session` (was
  `Continue a session`); icon changed to ↩️
- `/model` menu description updated to `Model settings` (was
  `Fallback model`) to reflect the new two-section UI
- `README.md` now points to `SETUP.md` at the repo root as the canonical
  setup guide

## [Unreleased] — 2026-04-27 — NightWatch IPC integration

Added optional localhost HTTP endpoint for external monitoring services to
deliver formatted messages and inline buttons to the bot's admin chat.
Designed for nightly digest pipelines (e.g., Sentry summarizers) that want
to ride the bot's existing Telegram delivery rather than talking to the
Telegram API themselves.

Highlights:
- HMAC-authenticated `POST /inject` + unauthenticated `GET /healthz` on
  `127.0.0.1:9091`.
- Three new commands: `/nightwatch_ping`, `/nightwatch_run`, `/nightwatch_last`.
- `set_my_commands` now registers the new commands at startup so they
  appear in Telegram's `/`-menu.
- Protocol docs at [`docs/NIGHTWATCH_IPC.md`](NIGHTWATCH_IPC.md) — full
  request/response schema, signing rules, security notes.
- Bot remains fully functional if `BOT_NIGHTWATCH_HMAC_SECRET` is unset
  (the IPC server simply does not start; a startup warning is logged).

Implementation: aiohttp server runs on the same asyncio loop as
python-telegram-bot's polling, with graceful shutdown integrated so SIGTERM
cleanly drains both surfaces.

Migration: none. Existing bot behavior is unchanged when the new env vars
are not set.

Also in this release — small hygiene scrub of three previously-hardcoded
identifiers in `bot.py`, all moved to env vars with safe localhost defaults
so the bot is deployable by external users without further patching:

- `REPORT_URL` now reads `REPORTS_BASE_URL` (default `http://localhost:8080/reports`).
- OpenRouter `HTTP-Referer` and `X-Title` headers now read `APP_REFERER_URL`
  and `APP_TITLE`.
- `/logs` button now reads `PROD_HOST` (default `127.0.0.1`).

## [Unreleased] — 2026-04-26 — Merge of fix-session-resume-and-routing

Brought into main:
- `70fe758` — HTML chunk-splitter safety (closes `<pre>`/`<code>` at chunk
  boundaries; `BadRequest` fallback to plain text; surfaced exceptions in
  `_flush_buffer`)
- `fd7c656` — Multi-turn session fix (`--session-id` on first call, `--resume`
  after; `SessionManager.create` sets `ACTIVE_SESSION`; state persistence
  to `/opt/shahrzad-devops/configs/bot-state.json` with 10 s autosave
  and `post_init` restore)

This brings main into parity with what has been running on production
since 2026-04-21. Future deploys will once again happen from main.

## [Unreleased] — 2026-04-20 — Fix silent report loss on long Claude outputs

### Fixed — critical

**Reports from long Claude sessions were silently dropped.** Users experienced
this as "Claude announced a new session but I never got the report from the
previous one / context was lost." Root cause was **not** memory loss — the
report was generated and sent correctly from Claude, but the Telegram bot
failed to deliver it.

- **`send_long` HTML chunk splitting (bot.py:841)** — when a reply longer than
  4000 characters contained a `<pre>...</pre>` or `<code>...</code>` block
  that spanned a chunk boundary, the first chunk was sent without its closing
  tag. Telegram rejected the message with
  `BadRequest: Can't parse entities: can't find end tag corresponding to
  start tag "pre"` and the entire chunk was dropped silently. The bot had no
  retry path and the user saw nothing.

  Fix: new helper `_split_html_chunks` closes unclosed `<pre>`/`<code>` tags
  at each chunk boundary and reopens them in the next chunk, keeping every
  chunk independently valid HTML.

- **`send_long` no fallback on HTML parse error (bot.py:853)** — even with
  balanced tags, rare HTML encoding issues (malformed entities, unescaped
  `<`/`>` in user data) could still trigger `BadRequest`. The exception was
  not caught and the chunk was lost.

  Fix: catch `telegram.error.BadRequest` explicitly; on "parse entities" /
  "end tag" errors, retry the same chunk with `parse_mode=None` after
  stripping HTML tags — the user sees plain text instead of nothing.

- **`_flush_buffer` unhandled exception dropped user messages (bot.py:2261)**
  — this coroutine runs as a detached asyncio task scheduled via
  `call_later → ensure_future`. Any exception bubbled up to the task and
  surfaced as `ERROR: Task exception was never retrieved` in the journal
  while the user saw complete silence. Observed failure modes in the past
  week: `telegram.error.BadRequest` (see above), httpx transport errors.

  Fix: wrap the entire `_flush_buffer` body in try/except. On error log with
  `exc_info=True` and best-effort notify the user with a short error
  message pointing to `journalctl`.

### Observed incidents (evidence for the fix)

Dates and sessions where silent drops were observed in
`journalctl -u claude-telegram-bot`:

| Date (UTC)          | Session       | Error                                    |
| ------------------- | ------------- | ---------------------------------------- |
| 2026-04-18 16:47:22 | —             | `BadRequest: end tag "pre"`              |
| 2026-04-18 17:27:44 | —             | `BadRequest: end tag "pre"`              |
| 2026-04-18 18:04:40 | —             | `BadRequest: end tag "pre"`              |
| 2026-04-18 18:43:53 | —             | `BadRequest: end tag "pre"`              |
| 2026-04-20 08:39:02 | —             | `BadRequest: end tag "pre"`              |
| 2026-04-20 12:14:42 | —             | `BadRequest: end tag "pre"`              |

### Verification

A standalone test file (`/tmp/test_chunk_splitter.py` at development time)
covers seven cases for the new splitter:

1. Short text unchanged (single chunk)
2. Long plain text splits on newlines, preserves words
3. **`<pre>` block spanning boundary** — the regression case above
4. Multiple `<pre>` blocks in one message
5. Long `<code>` block
6. Sequential `<pre>` then `<code>`, both long
7. Realistic long report with embedded log block

All seven pass. The critical invariant — every produced chunk has balanced
`<pre>/<code>` tags — holds across all cases.

### Unchanged

- No behavior change for messages under 4000 chars or without block tags.
- No new dependencies (`re` and `telegram.error.BadRequest` were already
  available / used elsewhere).
- No breaking changes to any public function signature.

### Operational context

This fix is part of a broader VPS hygiene pass on 2026-04-20. Companion
changes on the host (outside this repo):

- Root filesystem reduced from 94% to ~76% full via `docker image prune -a`
  (9 GB reclaimed).
- New `/opt/shahrzad-devops/scripts/nightly-cleanup.sh` scheduled at
  00:30 UTC (04:00 Asia/Tehran) via `/etc/cron.d/shahrzad-nightly`.
- New `/opt/shahrzad-devops/scripts/session-reaper.sh` for manual cleanup
  of idle `claude --print` subprocesses older than 2 hours.
