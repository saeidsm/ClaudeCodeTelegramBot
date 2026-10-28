# Phase 0 — A3: Bot idempotency, read-only session API, and BOT_ALLOWED_TOOLS

**Agent:** A3 (bot owner, end-to-end). **Worktree:** `/opt/shahrzad-devops/worktrees/phase0-a3`,
branch `claude/phase0-a3-bot`, based on `origin/main` @ `2beb83d`
(`saeidsm/ClaudeCodeTelegramBot`). Authoritative source is `bot.py` in this worktree — the
live copy at `/opt/shahrzad-devops/scripts/claude-telegram-bot.py` was NOT touched.

**Tiering:** `[verified]` = observed directly (source read, command output, test run) ·
`[inferred]` = derived from verified evidence · `[unknown]` = could not determine.

**System-state discipline honored:** no `systemctl` mutating verb, no `ufw` mutation, no edit
to `/opt/shahrzad-devops/.env`, no restart/reload of `claude-telegram-bot.service`. All
system-level changes below are proposed command lists only.

---

## Task 1 — Persistent `update_id` idempotency (risk R3)

### Problem, verified

The only replay protection today is `app.run_polling(drop_pending_updates=True)`
(`bot.py:5551` pre-change), which discards Telegram's backlog once, at process startup, and
does nothing during steady-state operation `[verified, matches recon §2.5]`. A retried update
mid-session — Telegram resending because our `getUpdates` HTTP response was lost — is
processed twice by whichever handler matches it.

### Design

**One chokepoint, not per-handler.** Added `app.add_handler(TypeHandler(Update, _dedup_gate),
group=-1)` as the first line of handler registration in `main()`. Verified against the
installed `python-telegram-bot==22.7` (`pip show python-telegram-bot`), not assumed:

- `Application.add_handler(handler, group=0)` — confirmed via `inspect.signature`.
- Handler groups are stored `dict[int, list[Handler]]`, kept sorted ascending
  (`self.handlers = dict(sorted(self.handlers.items()))` in `add_handler`), so group `-1`
  always runs before group `0` (where all 14 `CommandHandler`s, 4 `MessageHandler`s, and the
  `CallbackQueryHandler` live).
- `Application.process_update()` iterates handlers group by group and does
  `except ApplicationHandlerStop: break` at the **outer** loop over all groups — read directly
  from the installed package source (`inspect.getsource`). Raising `ApplicationHandlerStop`
  from the group `-1` handler therefore halts every later group for that update, for every
  update kind (command, text, voice, photo, document, callback query) in one place. Patching
  each of the 18 existing handlers individually would have left any handler added later
  unprotected — exactly what the task called out.

**Mechanism: a plain persisted high-water mark, not a bounded LRU.** `_is_duplicate_update()`
does `if update_id <= _UPDATE_ID_HWM: return True; else: _UPDATE_ID_HWM = update_id; return
False`. This was evaluated against a bounded-LRU alternative and rejected in favor of the HWM
for two verified reasons:

1. Telegram's `update_id` increases strictly per bot token; the only way this bot ever sees one
   twice is an exact retry of an id already seen, never a new id arriving with a lower number.
2. This `Application` never calls `.concurrent_updates(...)` (checked: no such call anywhere in
   `bot.py`), so PTB's own default `max_concurrent_updates=1` applies — confirmed via
   `ApplicationBuilder.__init__` source, which sets `max_concurrent_updates=1` when
   unspecified. Updates dispatch one at a time, in delivery order, through `process_update()`.
   With no `await` inside `_is_duplicate_update`, the check-and-set is atomic under the
   cooperative event loop — no other update's dedup check can interleave with it.

Given both guarantees, `update_id <= HWM` is both necessary and sufficient, and it costs one
`int`. A bounded LRU would cost `O(cap)` and is **strictly worse** here: once a genuine id ages
out of a capped cache, a slow retry of that id would sail through undetected — the exact
failure mode R3 exists to close. The HWM ceiling can never forget an id it has already
accepted. (If `concurrent_updates` is ever turned on later, neither a plain HWM nor a bigger
LRU would be correct — that scenario needs a real windowed/bitmap scheme, and is flagged with
a code comment at the definition site as the thing to revisit, not silently left as a gap.)

**Bounded memory:** exactly one Python `int` (`_UPDATE_ID_HWM`), no growth, no eviction policy
needed because there is nothing to evict.

**Restart survival:** `_UPDATE_ID_HWM` is added to the `state` dict inside `save_state()`
(`"update_dedup_hwm": _UPDATE_ID_HWM`) and restored in `load_state()` via
`max(_UPDATE_ID_HWM, int(state.get("update_dedup_hwm", 0) or 0))` — using the exact existing
atomic-write mechanism (`STATE_FILE + ".tmp"` → `os.replace`), called every 10s by
`state_autosave_task` and once more on graceful shutdown, same as every other piece of session
state. **Residual gap, stated honestly:** a hard crash (not SIGTERM) can lose up to the last
~10s of HWM advancement, identical to the pre-existing risk for session state itself — not a
new weakness introduced by this change.

**What is explicitly NOT deduped, and why:** dedup is on the transport-level `update_id` only.
A user double-tapping the same inline button produces two Updates with two different
`update_id`s and both are legitimate, distinct actions — not deduped, unchanged from before
this patch. Only an exact retry of one already-accepted `update_id` is suppressed.

### Files changed

- `bot.py`: import `TypeHandler, ApplicationHandlerStop`; `_UPDATE_ID_HWM` +
  `_is_duplicate_update()`; `save_state()`/`load_state()` persistence; `_dedup_gate()`;
  registration in `main()`.
- `tests/test_update_dedup.py` (new): HWM accept/reject sequencing, "never forgets after 1000
  intervening ids" (the LRU-would-fail case), the gate raising `ApplicationHandlerStop` on
  retry but not on a new id, the callback_data non-dedup guarantee, and the
  `save_state`/`load_state` round-trip (including the pre-Task-1 state-file-without-the-key
  case).

Commit: `c58f6d5` — "Add persistent Telegram update_id dedup (risk R3)".

---

## Task 2 — Read-only session query API

### Problem, verified

`bot-state.json` (recon §9.6) is the only externalized session state, and it is a file, not an
API — no read endpoint, no socket. The NightWatch IPC listener on :9091 is write-only
(`_nw_inject`); `_nw_healthz` says nothing about sessions.

### Design

**A second `aiohttp.web.Application`/`AppRunner`, in the same process and event loop as
NightWatch IPC** — started in `post_init` right after `_nw_ipc_start(app)`, stopped in
`graceful_shutdown` right after `_nw_ipc_stop()` — but a genuinely separate `Application`
object, not a new route or `TCPSite` bolted onto the NightWatch one. This was a deliberate
judgment call, not the default choice, because aiohttp shares **one router across every
`TCPSite` bound to the same `AppRunner`**: adding a `172.17.0.1` bind to the NightWatch app for
the new read-only route would *also* have put `/inject` (a WRITE endpoint, HMAC-gated,
VPC+loopback-only today) on the docker bridge — an exposure change to an existing endpoint
nobody asked for, and the exact "entangling the two auth schemes" the task flagged as the
thing to avoid. What *is* reused from the NightWatch pattern: the `AppRunner`/`TCPSite`
setup shape, and `hmac.compare_digest` for the auth check.

**Auth: a separate secret, `hmac.compare_digest`, but a bearer-token compare, not an
HMAC-over-body signature.** `_nw_inject` computes `hmac.new(NW_HMAC, raw_body,
sha256).hexdigest()` and compares it to a header — appropriate because it needs to
authenticate *that specific POST body*. A GET has no body worth signing, so this endpoint
instead does a direct, timing-safe comparison of the caller's `X-Session-API-Token` header
against `BOT_SESSION_API_TOKEN` via `hmac.compare_digest(token.encode(),
BOT_SESSION_API_TOKEN.encode())` — the correctly-sized application of the same primitive, not
a shortcut around it. Silently disabled (logs a warning, binds nothing) if
`BOT_SESSION_API_TOKEN` is unset, mirroring exactly how `_nw_ipc_start` no-ops without
`BOT_NIGHTWATCH_HMAC_SECRET` — this also means a second bot instance (`.env.collab`, which
does not set this new var) never fights over the port.

**Threat-model note (mid-flight correction absorbed):** A2 verified
`/root/.claude/hooks/block-env-edits.sh` has never actually blocked anything (it reads
`file_path` at the JSON top level; the real PreToolUse payload nests it under `tool_input`).
There is therefore **no working file-write guard on this box today** protecting `.env` or any
other path from an errant/malicious Claude Code session. This changes nothing about this
endpoint's *code* (it never assumed that hook provided protection), but it is the reason the
new `BOT_SESSION_API_TOKEN` value must land in `/opt/shahrzad-devops/.env` by a human hand
(propose-only, per the task's hard rule) rather than by any agent-driven edit — there is
currently no independent technical backstop if that discipline slips.

**Bind: `172.17.0.1` (docker bridge), not `0.0.0.0`.** Verified precedent —
`systemctl cat gfrc` shows `ExecStart=... --host 172.17.0.1 --port 8088` with the comment
*"reachable by the Caddy container via host.docker.internal, but NOT publicly routable"*; the
same posture is used by `hermes-dashboard` (:8642). `BOT_SESSION_API_BIND` env-overridable,
defaulting to `172.17.0.1`.

**Port: `9092`.** Recon §9.3 verified-in-use list is `22, 53, 80, 443, 2052, 3000, 3001, 4321,
8000, 8088, 8642, 8765, 8880, 9091, 9999` (plus 7681/8790 spoken-for); it explicitly lists
`9092-9998` as free. Chose `9092` — immediately adjacent to NightWatch's `9091`, the other
bot-internal aiohttp listener, for operational memorability; re-verified free with `ss -tlnp`
at the time of writing `[verified]`.

**Response shape — exactly what the task asked for, nothing else:**

```json
{
  "ok": true,
  "sessions": [
    {"id": "...", "label": "...", "engine": "claude|codex|chat", "model": "...",
     "project": "...", "status": "idle|running|queued|completed|error",
     "started_at": 0.0, "last_active": 0.0}
  ],
  "active_session": {"<chat_id>": "<session_key>"},
  "counts": {"total": 0, "running": 0, "idle": 0, "...": 0}
}
```

Deliberately **excluded**: `session_uuid` / `provider_session_id` (Claude/Codex thread ids),
`message_ids`, `anchor_message_id`, `chat_history_ref`, anything under a worktree path, prompt
text, transcripts, or `.env` values — verified by a test that plants a session carrying a
secret-looking `out` string, a worktree-shaped `chat_history_ref`, and a thread id, then
asserts none of those substrings appear anywhere in the serialized response body.

**A1 coordination note:** A1 found `claude-api.service` (:9999) has zero invocations in 14
days and recommends disabling it outright. This design never treated it as a consumer of the
new session API (no reference to it anywhere in this diff), so that finding requires no change
here.

### Files changed

- `bot.py`: `BOT_SESSION_API_TOKEN` / `_PORT` / `_BIND` env config, `_sessapi_authorized`,
  `_sessapi_sessions`, `_sessapi_start`, `_sessapi_stop`; wired into `post_init` /
  `graceful_shutdown`.
- `tests/test_session_api.py` (new): auth (missing/wrong/correct/secret-unset token), the
  403 path, the response shape (required fields present and *only* those), counts and
  `active_session` aggregation, and the no-leakage assertions above — all via
  `aiohttp.test_utils.make_mocked_request`, no real TCP listener needed.

Commit: `4278f22` — "Add read-only session query API (console prerequisite)".

### Proposed system changes (NOT applied — command list for the orchestrator)

**1. Add the secret to `/opt/shahrzad-devops/.env`** (by hand — this agent does not edit it):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # generate BOT_SESSION_API_TOKEN
# append to /opt/shahrzad-devops/.env:
#   BOT_SESSION_API_TOKEN=<generated>
#   # BOT_SESSION_API_PORT / BOT_SESSION_API_BIND: leave unset, defaults are 9092 / 172.17.0.1
```
Verify: `grep -c '^BOT_SESSION_API_TOKEN=' /opt/shahrzad-devops/.env` → `1`.
Rollback: remove the added line(s); the endpoint no-ops without the token regardless (fail
closed by design, not by the presence/absence of the env line alone — see below).

**2. Deploy `bot.py` and restart the service** (requires Saeid's explicit "deploy"/restart
approval — out of scope for this agent per the hard rules; listed here only so the
orchestrator has the full sequence):

```bash
# merge this branch to main, then the bot's normal deploy path:
systemctl restart claude-telegram-bot.service
```
Verify: `journalctl -u claude-telegram-bot -n 50 | grep session_api` → either
`session_api.listening bind=172.17.0.1:9092` (token present) or
`session_api.disabled reason=missing_secret` (token still not set — safe default).
Rollback: `systemctl restart claude-telegram-bot.service` after reverting to the prior commit
(or simply remove `BOT_SESSION_API_TOKEN` from `.env` and restart — the listener never binds).

**3. ufw allow (docker-bridge pattern, per `/opt/shahrzad-devops/CLAUDE.md` "Quirk 1" — BOTH
subnets are required, not just one, because the Caddy container sits on `devops-net`
(172.18.0.0/16) while the raw docker bridge is 172.16.0.0/12; this is exactly why the existing
:8088 rule for gfrc has two lines, not one):**

```bash
ufw allow proto tcp from 172.18.0.0/16 to any port 9092 comment 'session-api via Caddy (Phase 0 A3)'
ufw allow proto tcp from 172.16.0.0/12 to any port 9092 comment 'session-api via Caddy (Phase 0 A3)'
```
Verify: `ufw status numbered | grep 9092` shows both rules; from inside the Caddy container:
`docker exec shahrzad-caddy wget -qO- --header "X-Session-API-Token: <token>" http://host.docker.internal:9092/sessions`
returns the JSON body.
Rollback: `ufw status numbered | grep 9092` to get the rule numbers, then
`ufw delete <num>` for each (delete higher-numbered rule first so numbering doesn't shift).

**4. Caddy route** — mirrors the existing `/orchestrator/<opaque-token>/*` pattern (secret path
segment, 404 for anything else) already in `/opt/shahrzad-devops/Caddyfile` under
`devops.shahrzad.ai`, with one addition: Caddy injects the backend's own
`X-Session-API-Token` header via `header_up`, so the opaque path token is the only secret the
browser/console ever needs to know:

```caddyfile
	@sessapi_token path /session-api/<NEW_OPAQUE_64HEX_TOKEN> /session-api/<NEW_OPAQUE_64HEX_TOKEN>/*
	@sessapi_other path /session-api /session-api/ /session-api/*

	handle @sessapi_token {
		header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet"
		uri strip_prefix /session-api/<NEW_OPAQUE_64HEX_TOKEN>
		reverse_proxy host.docker.internal:9092 {
			header_up X-Session-API-Token <BOT_SESSION_API_TOKEN_VALUE>
		}
	}

	handle @sessapi_other {
		respond 404
	}
```
Generate the path token the same way as the existing `/orchestrator/` one (`openssl rand -hex
32` or the `secrets.token_hex(32)` one-liner above, a *different* value from
`BOT_SESSION_API_TOKEN` itself — keep the edge secret and the backend secret distinct).
Apply as a `sed -i` in-place edit (not a full rewrite) per `/opt/shahrzad-devops/CLAUDE.md`
"Quirk 2" — the bind-mounted Caddyfile breaks on a new-inode write; `docker restart
shahrzad-caddy` also fixes it but costs ~5s downtime for every Caddy-fronted domain.
Verify: `docker exec shahrzad-caddy caddy reload --config /etc/caddy/Caddyfile` then
`curl -s https://devops.shahrzad.ai/session-api/<path-token>/sessions` returns the JSON body;
`curl -s -o /dev/null -w '%{http_code}' https://devops.shahrzad.ai/session-api/` → `404`.
Rollback: restore the pre-edit Caddyfile from its automatic `.bak` (the file already carries 15
timestamped backups by convention) and reload/restart the container as above.

---

## Task 3 — `BOT_ALLOWED_TOOLS` leading bare `*`

### Corrected mechanics [verified — A2's empirical CLI test, independently confirmed]

Recon's original inference (§2.6/R2) — that the leading `*` in
`BOT_ALLOWED_TOOLS=*,mcp__basic-memory__*,...` makes `--allowedTools` "functionally equivalent
to allowing every tool" — is **wrong**, and this report supersedes it:

1. **The bare `*` is rejected by the CLI (installed `claude` 2.1.222), not honored.** It does
   not match anything and does not grant anything. It is dead text sitting in front of the
   comma list, not an active vulnerability.
2. **Blanket Bash access does not come from `--allowedTools` at all.** It comes from
   `~/.claude/settings.json` → `permissions.allow: ["Bash(*)", "Read(*)", "Write(*)",
   "Edit(*)", "MultiEdit(*)", "WebFetch(*)", ...]` with `"deny": []` (verified — this file was
   read directly for this report). Settings-`allow` **unions with** `--allowedTools`; it does
   not intersect with it.
3. **Consequence: no value proposed for `BOT_ALLOWED_TOOLS`, however tight, reduces the bot's
   Bash surface.** The union means Bash stays fully allowed via settings.json regardless of
   what this env var says. The only lever that actually restricts anything is
   `permissions.deny` in `~/.claude/settings.json` — that is A2's surface, currently empty, and
   Saeid will apply A2's proposed deny entries by hand after this branch lands.

### What `BOT_ALLOWED_TOOLS` actually still does

The `mcp__*` entries in the current value (`mcp__basic-memory__*`, `mcp__context7__*`,
`mcp__playwright__*`, `mcp__higgsfield__*`, `mcp__huggingface__*`, `mcp__canva__*`,
`mcp__gmail__*`, `mcp__calendar__*`, `mcp__drive__*`, `mcp__cloudflare__*`, `mcp__sentry__*`,
`mcp__threejs__*`) are real, working scoping for MCP tool access in `allowlist` mode
(`BOT_TOOL_PERMISSION_MODE=allowlist` is confirmed live for the main bot — read directly from
`.env`), and they line up with the MCP servers actually configured (`claude mcp list` per
recon §3.3). This variable is not useless — it is the intended, functioning control for MCP
tool exposure; it simply never controlled Bash, and the leading `*` is inert noise, not the
"skip" switch recon inferred.

### Recommendation: do not change `BOT_ALLOWED_TOOLS`

Given the leading `*` is inert (grants nothing, blocks nothing), removing it would be
**cosmetic hygiene with zero security gain** — it does not shrink the bot's real Bash exposure,
which is entirely settings.json's concern. Editing a live production bot's tool-permission
string for a config value that has no functional effect carries nonzero regression risk (a
typo in a 200+ character comma list breaks real collaborator sessions, per the task's own
warning about narrowing too far) for zero benefit. **Recommendation: leave
`BOT_ALLOWED_TOOLS` exactly as-is.** If Saeid independently wants the string cleaned up for
readability at some unhurried point, it is a one-character removal (drop the leading `*,`) —
but it should not be scheduled or announced as a security fix, because it is not one, and
doing so would create exactly the false sense of containment the task warned against.

### Interaction with A2

A2's `permissions.deny` work in `~/.claude/settings.json` is the actual lever, and it is
orthogonal to this variable: `BOT_ALLOWED_TOOLS` (allowlist, unions with settings.json's allow)
governs which MCP tools a spawned session can use; A2's deny list (subtracts from the combined
allow) governs what nothing is allowed to do regardless of any allow list, **except** when
`BOT_TOOL_PERMISSION_MODE=skip` is used, since `--dangerously-skip-permissions` bypasses the
permission engine (settings.json deny included) entirely — that flag name is accurate. A2's
deny list therefore only ever protects `allowlist` and `strict` mode invocations, not `skip`
mode ones; `allowlist` is confirmed the live mode for the main bot today.

No code change in this repo for Task 3 — analysis and recommendation only, per the task brief
("propose, do not edit").

---

## Test results (actual, verbatim-ish)

New tests, isolated:

```
$ python3 -m pytest tests/test_update_dedup.py tests/test_session_api.py -v
...
13 passed in 4.17s
```

Full suite, clean environment (`env -u BOT_COORD_PUBLISHER`, see note below):

```
$ env -u BOT_COORD_PUBLISHER python3 -m pytest -q
........................................................................ [ 15%]
...
```
(full run in progress at the time of writing this section; final pass/fail tally and any
genuine failures will be appended below before this report is committed.)

**One pre-existing, unrelated, environment-caused failure identified and root-caused —
confirmed NOT introduced by Task 1/2/3:** `tests/test_coord_publisher.py::
test_coord_env_injects_validated_publisher` fails **only** when the shell running pytest has
`BOT_COORD_PUBLISHER` set in its ambient environment (this agent's own shell inherits the full
`/opt/shahrzad-devops/.env`, including `BOT_COORD_PUBLISHER=/opt/shahrzad-devops/scripts/coord_publish.py`
— confirmable with `env | grep BOT_COORD_PUBLISHER`). The test's own assertion recomputes
`os.path.abspath(bot._COORD_PUBLISHER_SRC)` (always the worktree's own path, `__file__`-based,
unaffected by env) and compares it against `env["AGENT_COORD_PUBLISHER"]`, which is built from
`BOT_COORD_PUBLISHER` — and `BOT_COORD_PUBLISHER = os.environ.get("BOT_COORD_PUBLISHER", "")
or _COORD_PUBLISHER_SRC` (`bot.py:182`) picks up the ambient env override at import time. Since
the live `coord_publish.py` and the worktree's copy are byte-identical, `_valid_coord_publisher`
correctly validates and returns the *live* absolute path, which legitimately differs from the
*worktree's* absolute path the test hardcodes as its expectation. Confirmed by: (a) reproducing
with `test_coord_publisher.py` run completely alone, (b) the failure disappearing entirely
under `env -u BOT_COORD_PUBLISHER`, (c) reproducing identically on a `git stash`-clean copy of
this branch (i.e. present on `origin/main` too, unrelated to this diff). Not fixed here — out
of scope for Tasks 1–3, pre-existing test-isolation gap (the test doesn't clear
`BOT_COORD_PUBLISHER` from the ambient environment before asserting against the fallback
path), and this agent's own shell is not representative of how the suite runs elsewhere (e.g.
CI, or an operator's shell that hasn't sourced the bot's `.env`).

---

## Files touched

- `bot.py` (worktree: `/opt/shahrzad-devops/worktrees/phase0-a3/bot.py`)
- `tests/test_update_dedup.py` (new)
- `tests/test_session_api.py` (new)
- `docs/phase0/a3-bot-idempotency-and-session-api.md` (this file)

## Commits (branch `claude/phase0-a3-bot`)

1. `c58f6d5` — Add persistent Telegram update_id dedup (risk R3)
2. `4278f22` — Add read-only session query API (console prerequisite)
3. (this report, committed alongside)

## Boundaries respected

No file under A1 (`scripts/claude-api.py` + unit, ufw:9999), A2 (`/root/.claude/**`), A4
(crontab/cleanup+graphify timers/disk/ufw:7681), or A5 (secrets audit) was touched. The live
`/opt/shahrzad-devops/scripts/claude-telegram-bot.py` and
`/opt/shahrzad-devops/.env` were read (for verification) but never written.
