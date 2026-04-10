# Architecture

## Overview

The bot is a single-file Python application (~1700 lines) that bridges Telegram's Bot API with the Claude Code CLI. It runs Claude Code as an async subprocess and manages multiple concurrent sessions per chat.

## Flow

```
Telegram Message
    │
    ├─ Voice → Gemini STT → Refine → Confirm → Claude Code CLI
    ├─ Text  → Session Router → Claude Code CLI
    └─ File  → Queue in Session → Next text triggers execution
                                        │
                                        ▼
                                  Claude Code CLI
                                  (subprocess, async)
                                        │
                                  ┌─────┴─────┐
                                  │ Success    │ Rate Limited
                                  ▼            ▼
                              Telegram    Fallback Chain
                              Response    Gemini → GPT → OpenRouter
```

## Key Components

### SessionManager

Manages up to `MAX_SESSIONS` concurrent sessions per chat.

- Each session has: label, color emoji, project, status, UUID, file queue, output history
- Sessions are keyed by `chat_id:label`
- Message IDs are mapped to sessions for reply-based routing
- Auto-cleanup removes idle sessions after configurable timeout
- Permanent projects can be excluded from auto-cleanup

### UsageTracker

Tracks estimated token usage with JSON file persistence.

- Records input/output character counts per request
- Estimates tokens (chars / 4)
- Provides hourly/daily/weekly summaries with visual bars
- Alerts at 80% hourly threshold
- Auto-prunes records older than 8 days

### run_claude()

Core execution function — runs Claude Code CLI as an async subprocess.

- Uses `claude --print --session-id <uuid> <prompt>`
- Runs in the project's directory
- 60-minute timeout
- Handles "session ID already in use" by retrying with new UUID
- Detects rate limiting and triggers fallback chain
- Copies attached files to `.claude-tasks/` in the project directory

### Fallback Chain

Multi-provider fallback when Claude Code is rate-limited:

1. **Active provider** (configurable via `/model`) — tries first
2. **Gemini** — uses `google-genai` SDK
3. **OpenAI** — direct HTTP to `api.openai.com`
4. **OpenRouter** — direct HTTP to `openrouter.ai`

Each provider returns a labeled response indicating it's a fallback. If all fail, returns an error message.

### Voice Pipeline

1. **Download** — Save Telegram voice message as OGG
2. **Transcribe** — Send audio to Gemini with base64 encoding
3. **Match command** — Check against `voice_commands` mapping
4. **Short command** → Execute immediately (deploy, health, test, etc.)
5. **Long message** → Show transcription → User confirms → Refine with Gemini → Execute

### Reply Routing

Maps Telegram message IDs to sessions:

- Every bot response is registered: `message_id → session_key`
- When user replies to a message, the bot looks up which session it belongs to
- Enables natural conversation threading with multiple sessions

### Report Generation

- Creates timestamped directories in `REPORTS/`
- Writes `summary.txt` with full output
- Creates ZIP archive
- Returns browsable links (configurable via `BOT_REPORT_URL`)

## Conversation States

Multi-step flows (like creating a session with a new project) use `CONV_STATE`:

- `awaiting_session_name` — User is typing a session name
- `awaiting_project_path` — User is typing a project path
- `awaiting_model_search` — User is searching OpenRouter models

States are stored in memory per `chat_id` and cleared after the flow completes.

## External Dependencies

| Package | Purpose |
|---------|---------|
| `python-telegram-bot` | Telegram Bot API wrapper |
| `google-genai` | Gemini API (STT, refinement, fallback) |
| `openai` | OpenAI GPT fallback (imported but uses httpx directly) |
| `httpx` | Async HTTP for OpenAI/OpenRouter API calls |

## File Layout

```
bot.py
├── Config & env loading (lines 1-100)
├── UsageTracker class (lines 100-190)
├── Projects config (lines 195-240)
├── Fallback chain (lines 250-360)
├── Session model & manager (lines 360-470)
├── UI keyboards (lines 470-540)
├── Helpers (lines 545-570)
├── Auth decorator (lines 570-580)
├── Claude Code execution (lines 585-650)
├── Gemini: transcribe + refine (lines 655-695)
├── Reports (lines 700-705)
├── Send utilities (lines 710-735)
├── Session resolution (lines 737-755)
├── Core execute with pause (lines 758-840)
├── Quick actions (lines 845-900)
├── Command handlers (lines 900-1090)
├── Callback handler (lines 1110-1395)
├── Document/photo/voice handlers (lines 1400-1530)
├── Text handler (lines 1535-1677)
└── Boot & main (lines 1680-1742)
```

## Design Decisions

- **Single file:** Keeps deployment simple — just copy `bot.py` and run
- **Async subprocess:** Claude Code CLI is CPU-bound; async prevents blocking the bot
- **Session UUIDs:** Claude Code's `--session-id` enables conversation continuity
- **Reply-based routing:** Leverages Telegram's native reply threading for multi-session UX
- **Fallback chain:** Ensures the bot stays responsive even during Claude rate limits
- **Progress updater:** Shows elapsed time every 30s so users know the bot is working
- **Pause window:** Prevents accidental execution of incomplete messages
