# Claude Code Telegram Bot

A Telegram bot that bridges [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with Telegram, enabling you to run Claude Code commands, manage multi-session workflows, send voice commands, attach files, and deploy — all from your phone.

Built for DevOps engineers and developers who want to interact with Claude Code on the go.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-blue.svg)

## Features

- **Multi-Session Management** — Run up to 3 concurrent Claude Code sessions, each with its own project context
- **Voice Commands** — Speak in Farsi or English; Gemini transcribes and refines your voice into structured prompts
- **File Attachments** — Send documents/photos with instructions; they're passed to Claude Code automatically
- **Smart Fallback Chain** — When Claude is rate-limited, falls back to Gemini → GPT-4o → OpenRouter (configurable)
- **Runtime Model Selection** — Switch fallback models on the fly via `/model`, including OpenRouter search
- **Deploy Controls** — One-tap deploy with branch selection and confirmation
- **Health & Logs** — Quick-access buttons for server health checks and log collection
- **Usage Tracking** — Token usage estimates with hourly/daily/weekly bars and alerts
- **Session Auto-Cleanup** — Idle sessions auto-close after configurable timeout
- **Pause Window** — 5-second countdown before execution lets you cancel or edit
- **Report Generation** — Save full outputs as browsable reports with shareable links

## Quick Start

### Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) Gemini API key for voice commands

### Installation

```bash
# Clone the repo
git clone https://github.com/saeidsm/ClaudeCodeTelegramBot.git
cd ClaudeCodeTelegramBot

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your tokens and settings

# Run
python bot.py
```

### Minimal `.env`

```env
TELEGRAM_BOT_TOKEN=your-bot-token-from-botfather
TELEGRAM_CHAT_ID=123456789
GEMINI_API_KEY=your-gemini-api-key    # Optional, for voice
```

See [docs/SETUP.md](docs/SETUP.md) for the full setup guide.

## Usage

### Basic Flow

1. Start the bot: `/start`
2. Create a session: `/new my-task`
3. Select a project from the list
4. Send text messages — they go to Claude Code
5. Reply to session messages to route to specific sessions

### Voice Commands

Send a voice message in Farsi or English:
- **Short commands** auto-execute: "deploy", "health", "test", "logs"
- **Long commands** are transcribed → refined into structured prompts → confirmed before execution

### File Attachments

1. Send a file to a session (reply or auto-routes if one session active)
2. Files are queued until you send a text instruction
3. Or send a file with a caption — executes immediately

### Multi-Session

```
/new frontend-fix     → creates session, pick project
/new api-refactor     → creates another session
/sessions             → list all active sessions
/kill frontend-fix    → end a session
```

Reply to any session message to route your next command there. If only one session is active, messages auto-route.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Home screen with keyboard |
| `/new <name>` | Create a new session |
| `/sessions` | List active sessions |
| `/kill <name>` | End a session |
| `/project <name>` | Change project for current session |
| `/model` | Select fallback AI model |
| `/usage` | View token usage stats |
| `/help` | Show help |

See [docs/COMMANDS.md](docs/COMMANDS.md) for the full command reference.

## Architecture

```
Telegram Message
    ├─ Voice → Gemini STT → Refine → Confirm → Claude Code CLI
    ├─ Text  → Session Router → Claude Code CLI
    └─ File  → Queue in Session → Next text triggers execution
                                        │
                                  Claude Code CLI (async subprocess)
                                        │
                                  ┌─────┴─────┐
                                  │ Success    │ Rate Limited
                                  ▼            ▼
                              Telegram     Fallback Chain
                              Response     Gemini → GPT → OpenRouter
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details.

## Configuration

All configuration is via environment variables. See [`.env.example`](.env.example) for the full list.

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Comma-separated authorized chat IDs |
| `GEMINI_API_KEY` | No | Enables voice transcription + Gemini fallback |
| `OPENAI_API_KEY` | No | Enables GPT fallback |
| `OPENROUTER_API_KEY` | No | Enables OpenRouter model selection |
| `BOT_MAX_SESSIONS` | No | Max concurrent sessions (default: 3) |
| `BOT_SESSION_TIMEOUT_HOURS` | No | Auto-close idle sessions after N hours (default: 72) |

## Custom Scripts

The bot calls shell scripts for health, logs, and deploy. Create your own in `scripts/`:

- `scripts/health-check.sh` — Called by 📊 Health button
- `scripts/collect-logs.sh` — Called by 📋 Logs button
- `scripts/deploy-to-prod.sh` — Called by 🚀 Deploy (receives `$1=project $2=branch`)

Example scripts are included in `scripts/`. Copy and customize them for your infrastructure.

## Running as a Service

```bash
# Copy the systemd unit file
sudo cp systemd/claude-telegram-bot.service /etc/systemd/system/

# Edit paths in the service file to match your installation
sudo systemctl edit claude-telegram-bot

# Enable and start
sudo systemctl enable claude-telegram-bot
sudo systemctl start claude-telegram-bot

# Check status
sudo systemctl status claude-telegram-bot
```

## Project Structure

```
ClaudeCodeTelegramBot/
├── bot.py                          # Main bot (single file, ~1700 lines)
├── .env.example                    # Environment variable template
├── requirements.txt                # Python dependencies
├── configs/
│   ├── gemini-prompts.json         # Customizable Gemini prompts
│   └── projects.json               # Project registry (auto-generated)
├── scripts/
│   ├── health-check.sh.example     # Example health check script
│   ├── collect-logs.sh.example     # Example log collection script
│   └── deploy-to-prod.sh.example   # Example deploy script
├── systemd/
│   └── claude-telegram-bot.service # Systemd unit file
└── docs/
    ├── SETUP.md                    # Detailed setup guide
    ├── COMMANDS.md                 # Full command reference
    └── ARCHITECTURE.md             # Architecture overview
```

## Gemini Prompts

Voice transcription and prompt refinement use Gemini. Customize the prompts in `configs/gemini-prompts.json`:

```json
{
  "transcribe": {
    "model": "gemini-2.5-flash",
    "prompt": "Transcribe this voice message exactly..."
  },
  "refine": {
    "model": "gemini-2.5-flash",
    "prompt": "Convert this casual command into a structured prompt..."
  },
  "voice_commands": {
    "deploy": ["deploy", "ship it"],
    "health": ["health", "status"],
    "test": ["test", "run tests"]
  }
}
```

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

[MIT](LICENSE) — Copyright (c) 2025 Saeid Saeidimehr
