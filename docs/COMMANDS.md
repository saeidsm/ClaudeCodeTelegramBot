# Command Reference

Complete reference for all bot commands, buttons, and interactions.

## Telegram Commands

### `/start`

Displays the home screen with a persistent keyboard and bot status.

Shows: default project, voice status (Gemini on/off), active session count.

### `/new <name>`

Create a new Claude Code session.

```
/new my-task          # Named session
/new                  # Auto-named (session-XXXX)
```

After creating, you'll pick a project from the inline keyboard. Max concurrent sessions is configurable via `BOT_MAX_SESSIONS` (default: 3).

### `/sessions`

List all active sessions with their status, project, task count, and age.

Status icons:
- 💤 Idle — waiting for input
- ⏳ Running — Claude Code is executing
- ❌ Error — last command failed

Each session shows a 🗑 button to kill it.

### `/kill <name>`

End a session by name.

```
/kill my-task         # Kill specific session
/kill                 # If one session: kills it. If multiple: shows picker.
```

### `/project <name>`

Change the project for the current session.

```
/project my-web-app   # Switch to project
/project               # Show project list with inline buttons
```

### `/model`

Select the fallback AI model used when Claude Code is rate-limited.

Shows a menu with:
- **Gemini** (primary fallback) — uses the model from `gemini-prompts.json`
- **GPT-4o** (OpenAI) — requires `OPENAI_API_KEY`
- **OpenRouter models** — popular models + search. Requires `OPENROUTER_API_KEY`

The selected model persists for the runtime session. Default: Gemini.

### `/usage`

Display token usage estimates with visual bars:

```
This hour:  ██████░░░░ 60%  ✅
Today:      ███░░░░░░░ 30%  ✅
This week:  █░░░░░░░░░ 10%  ✅
```

Shows sessions active and estimated tokens today.

### `/help`

Show the help screen with an overview of all features and how to use them.

---

## Persistent Keyboard Buttons

The home screen shows a reply keyboard with quick-access buttons:

| Button | Action |
|--------|--------|
| 📊 Health | Run `scripts/health-check.sh` and show output |
| 📋 Logs | Run `scripts/collect-logs.sh` and show output |
| 📁 Projects | List available projects with session info |
| 🆕 New Session | Create a new session (same as `/new`) |
| 📈 Usage | Show token usage stats (same as `/usage`) |
| ❓ Help | Show help (same as `/help`) |

---

## Inline Buttons (Callback Actions)

These appear as inline keyboard buttons in bot responses:

### After Claude Code Output

| Button | Action |
|--------|--------|
| 📦 Save Report | Save output as a browsable report with shareable links |
| 🚀 Deploy | Start deploy flow (select branch → confirm) |
| 📊 Health | Quick health check |

### Deploy Flow

1. Select branch: `main`, `dev`, or latest `claude/*` branch
2. Confirm: "Deploy project@branch → production? Sure?"
3. Runs `scripts/deploy-to-prod.sh <project> <branch>`

### Pause Window

When you send a message, there's a 5-second countdown:

| Button | Action |
|--------|--------|
| ⏸ Pause & Edit | Cancel execution, discard message, type a new one |

### Voice Confirmation

After transcribing a long voice message:

| Button | Action |
|--------|--------|
| ✅ Send to Claude | Refine and execute the transcribed text |
| ✏️ Edit first | Discard transcription, type manually |

---

## Voice Commands

Send a voice message in Farsi or English. The bot uses Gemini for speech-to-text.

### Quick Commands (auto-execute)

These single-word commands execute immediately without confirmation:

| Trigger Words | Action |
|--------------|--------|
| "deploy", "ship it", "go" | Start deploy flow |
| "health", "status", "check" | Run health check |
| "logs", "log" | Show logs |
| "test", "run tests" | Run all tests |
| "yes", "allow", "ok", "confirm", "approve" | Confirm |
| "new", "reset", "fresh" | Create new session |
| "standup", "morning" | Run standup report |

### Long Voice Messages

Messages longer than 3 words go through:
1. **Transcription** — Gemini converts audio to text
2. **Display** — Shows you the transcription
3. **Confirm** — You tap ✅ to proceed or ✏️ to edit
4. **Refinement** — Gemini converts casual speech into a structured Claude Code prompt
5. **Execution** — Refined prompt sent to Claude Code

### Customizing Voice Commands

Edit `configs/gemini-prompts.json` to add/modify trigger words:

```json
{
  "voice_commands": {
    "deploy": ["deploy", "ship it", "go", "push"],
    "health": ["health", "status", "check", "ping"]
  }
}
```

---

## File Handling

### Documents

Send any file (code, config, image, PDF, etc.):

- **With caption:** Executes immediately with the caption as the prompt
- **Without caption:** Queued in the session. Send a text message to trigger execution with all queued files.

### Photos

Same behavior as documents. Photos are saved as JPG and passed to Claude Code.

### Multiple Files

Send multiple files one by one — they queue up. Then send a text instruction to execute with all queued files.

---

## Session Routing

The bot routes messages to sessions using this priority:

1. **Reply** — If you reply to a session message, it routes there
2. **Single session** — If only one session is active, auto-routes
3. **Picker** — If multiple sessions active and no reply, shows a picker
4. **Auto-create** — If no sessions exist, auto-creates one

---

## Session Lifecycle

1. **Create** — `/new <name>` or auto-created on first message
2. **Active** — Receives messages, runs Claude Code, tracks files
3. **Idle** — No activity for a while
4. **Auto-close** — After `BOT_SESSION_TIMEOUT_HOURS` (default: 72h) of inactivity
5. **Kill** — Manual close via `/kill` or inline button

Sessions in `BOT_PERMANENT_PROJECTS` never auto-close.
