# NightWatch IPC — Protocol Reference

## Overview

The bot exposes an optional, HMAC-authenticated HTTP endpoint on
`127.0.0.1:9091` that lets an external monitoring service hand pre-formatted
digest messages to the bot for delivery to its admin chat. This is the
"delivery layer only" surface — the bot does **not** run analyses, schedule
collections, or talk to Sentry/Prometheus/etc. itself. It just receives a
fully-rendered HTML message + optional inline buttons + a list of authorized
chat IDs and forwards them to Telegram with the bot's existing dedup, retry,
and rate-limiting plumbing.

The IPC is **off by default**. Setting `BOT_NIGHTWATCH_HMAC_SECRET` to a
32+ hex-character random value enables it at bot startup. Leaving the
variable unset keeps the bot fully functional with no listener bound.

## Endpoints

### `GET /healthz`

No auth. Cheap liveness probe.

**Response 200:**

```json
{
  "ok": true,
  "uptime_s": 12345,
  "version": "1.0"
}
```

Use it from local cron/healthcheck scripts to confirm the IPC is alive
without needing to construct a signed payload.

### `POST /inject`

HMAC-authenticated. Sends a pre-formatted HTML message (and optional
inline buttons) to one or more authorized chats.

**Request headers:**

```
Content-Type: application/json
X-NightWatch-Signature: <hex sha256 hmac of body, using shared secret>
```

**Request body (full schema):**

```json
{
  "session_label": "string, ≤ 80 chars, used for dedup",
  "project":       "string, used for logging only",
  "message_html":  "string, Telegram-flavored HTML, ≤ 4000 chars",
  "buttons":       [{"text": "string", "url": "https://example.com/..."}],
  "chat_ids":      [123456789],
  "report_url":    "string, optional — for log breadcrumb only"
}
```

Field notes:

- `session_label` — used for per-(chat, label) dedup. The same label
  delivered twice within the dedup window is silently dropped on the
  second call. To force re-delivery, change the label (a `-manual`
  suffix is the convention used by the reference NightWatch service for
  republishes).
- `message_html` — Telegram HTML subset (`<b>`, `<i>`, `<u>`, `<s>`,
  `<code>`, `<pre>`, `<a>`). Anything else escapes. Hard cap **4000**
  chars to leave room for Telegram's own message envelope.
- `buttons` — up to 3 inline-keyboard buttons (one row). URLs only —
  callback buttons are not currently supported in the IPC contract.
- `chat_ids` — must be a non-empty subset of the bot's allowlist
  (`TELEGRAM_CHAT_ID` env). IDs not on the allowlist are silently
  dropped (logged but not surfaced in the response).
- `report_url` — purely cosmetic for the bot's logs. Not validated.

**Response codes:**

| Status | Meaning |
|---|---|
| 202 | accepted, at least one chat received the message; body has `delivered` / `duplicates` / `failed` counts |
| 400 | invalid payload — missing/oversized fields, malformed JSON, unsupported button shape |
| 403 | HMAC missing or invalid |
| 409 | duplicate `session_label` (within the per-chat dedup window) — body shows which chat(s) deduped |
| 500 | all chat deliveries failed (Telegram-side errors aggregated) |
| 503 | bot is in graceful-shutdown phase and refusing new work |

**Response body (202 example):**

```json
{
  "ok": true,
  "session_label": "nightwatch-2026-04-26-nightly",
  "delivered": 1,
  "duplicates": 0,
  "failed": 0
}
```

## Security

- The endpoint binds to `127.0.0.1` only — **never expose externally**.
  Use SSH tunnels, Unix-socket reverse proxies, or a localhost-only
  publisher running on the same host.
- HMAC-SHA256 over the raw request body, hex-encoded, sent in
  `X-NightWatch-Signature`. The bot recomputes the signature server-side
  with the same secret and compares in constant time.
- `chat_ids` must be in the bot's `TELEGRAM_CHAT_ID` allowlist; IDs
  outside it are silently dropped. **The IPC cannot deliver to chats the
  bot doesn't already authorize.**
- Any future extension that accepts a file path (not currently used by
  `/inject` itself) is validated against `BOT_NIGHTWATCH_ALLOWED_FILE_PREFIXES`,
  a whitelist of permitted directory roots.
- Rotate the HMAC secret by editing `.env` and restarting the bot. There
  is no in-process key rotation surface — the IPC is single-tenant.

## Telegram commands the IPC adds

When `BOT_NIGHTWATCH_HMAC_SECRET` is set, the bot registers three
commands at startup via `set_my_commands`:

| Command | Description |
|---|---|
| `/nightwatch_ping` | Health probe — returns the IPC's `/healthz` payload as a Telegram reply |
| `/nightwatch_run` | Manual trigger of the external NightWatch service if installed at the configured path. The bot itself does **not** run any analysis — it just kicks the external CLI |
| `/nightwatch_last` | Re-send the previous digest to the requesting chat |

If the external NightWatch service is not installed, `/nightwatch_run`
returns a clear error message — the IPC and the analyzer are independent.

## Reference implementation

The reference NightWatch service that pairs with this IPC reads issues
from a Sentry org, scores them, builds a daily digest snapshot, and POSTs
the rendered HTML to `/inject`. That service is private (it ships Sentry
credentials) but the **IPC protocol itself is documented above and any
external service can implement it**. A minimal Python sample looks like:

```python
import hashlib
import hmac
import json

import httpx

BOT_URL    = "http://127.0.0.1:9091"
HMAC_KEY   = b"...your shared secret..."

body = {
    "session_label": "my-monitor-2026-04-27-nightly",
    "project":       "my-monitor",
    "message_html":  "<b>Status</b>: all green",
    "buttons":       [{"text": "📊 Full Report", "url": "https://example.com/r/2026-04-27/"}],
    "chat_ids":      [123456789],
    "report_url":    "https://example.com/r/2026-04-27/",
}
raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
sig = hmac.new(HMAC_KEY, raw, hashlib.sha256).hexdigest()

resp = httpx.post(
    f"{BOT_URL}/inject",
    content=raw,
    headers={"Content-Type": "application/json", "X-NightWatch-Signature": sig},
    timeout=10.0,
)
print(resp.status_code, resp.json())
```

Implementations should retry transient 5xx with exponential backoff, but
treat 4xx as terminal — the bot will not re-accept a malformed payload
just because you re-send it.
