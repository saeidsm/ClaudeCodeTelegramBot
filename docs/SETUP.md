# Setup Guide

Complete guide to setting up the Claude Code Telegram Bot.

## Table of Contents

1. [Create a Telegram Bot](#1-create-a-telegram-bot)
2. [Get Your Telegram Chat ID](#2-get-your-telegram-chat-id)
3. [Get a Gemini API Key](#3-get-a-gemini-api-key)
4. [Install Claude Code CLI](#4-install-claude-code-cli)
5. [Authenticate Claude Code](#5-authenticate-claude-code)
6. [SSH Setup for Production](#6-ssh-setup-for-production)
7. [Configure Projects](#7-configure-projects)
8. [Install the Bot](#8-install-the-bot)
9. [Run as a Systemd Service](#9-run-as-a-systemd-service)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Create a Telegram Bot

1. Open Telegram and search for [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Choose a display name (e.g., "My Claude Bot")
4. Choose a username (must end in `bot`, e.g., `my_claude_code_bot`)
5. BotFather will give you a token like `123456:ABC-DEF...`
6. Save this token — you'll need it for `TELEGRAM_BOT_TOKEN`

**Optional settings via BotFather:**
- `/setdescription` — Set what users see before starting the bot
- `/setabouttext` — Short description in the bot's profile
- `/setuserpic` — Upload a profile picture

## 2. Get Your Telegram Chat ID

Your chat ID restricts who can use the bot. To find it:

**Method 1: Use @userinfobot**
1. Search for [@userinfobot](https://t.me/userinfobot) on Telegram
2. Send it any message
3. It replies with your chat ID (a number like `123456789`)

**Method 2: Use the bot API**
1. Send any message to your new bot
2. Visit: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Look for `"chat":{"id":123456789,...}`

**Multiple users:** Comma-separate IDs in `TELEGRAM_CHAT_ID`:
```env
TELEGRAM_CHAT_ID=123456789,987654321
```

## 3. Get a Gemini API Key

Gemini is used for voice transcription and as a fallback when Claude is rate-limited.

1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Sign in with your Google account
3. Click "Create API Key"
4. Copy the key (starts with `AI...`)
5. Save it for `GEMINI_API_KEY`

> **Note:** Gemini is optional. Without it, voice commands won't work, but text and file features work fine.

## 4. Install Claude Code CLI

Claude Code CLI is the core engine. The bot runs it as a subprocess.

### Option A: npm (requires Node.js 18+)

```bash
npm install -g @anthropic-ai/claude-code
```

### Option B: Direct install

```bash
curl -fsSL https://cli.anthropic.com/install.sh | sh
```

### Verify installation

```bash
claude --version
```

## 5. Authenticate Claude Code

### Option A: Anthropic Subscription (recommended)

If you have a Claude Pro, Team, or Enterprise subscription:

```bash
claude login
# Follow the browser flow to authenticate
```

### Option B: API Key

If you have an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxx
claude --version  # verify it works
```

### Verify

```bash
claude --print "Hello, world"
```

You should see a response from Claude. If this works, the bot will work too.

> **Important:** The bot runs `claude --print` as a subprocess. Make sure the user running the bot has Claude Code authenticated. If running as a systemd service, ensure the service user's home directory has the Claude credentials.

## 6. SSH Setup for Production

If you want to use the deploy feature to push to a production server:

```bash
# Generate SSH key (if you don't have one)
ssh-keygen -t ed25519 -C "bot-deploy-key"

# Copy to production server
ssh-copy-id user@your-production-server

# Test connection
ssh user@your-production-server "echo 'SSH works'"
```

Set in `.env`:
```env
PROD_SERVER_IP=your-server-ip
PROD_SERVER_USER=root
```

## 7. Configure Projects

Projects are directories that Claude Code operates in. There are two ways to configure them:

### Auto-discovery

If you set `BOT_REPOS_DIR` (default: `./repos`), the bot auto-discovers subdirectories as projects:

```
repos/
├── my-web-app/        → Project "my-web-app"
├── api-service/       → Project "api-service"
└── infra-config/      → Project "infra-config"
```

### Manual configuration

Edit `configs/projects.json`:

```json
[
  {"name": "my-web-app", "path": "/home/user/projects/my-web-app"},
  {"name": "api-service", "path": "/home/user/projects/api-service"}
]
```

You can also add projects at runtime via the bot's inline keyboard when creating a new session.

## 8. Install the Bot

```bash
# Clone
git clone https://github.com/saeidsm/ClaudeCodeTelegramBot.git
cd ClaudeCodeTelegramBot

# Create virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # or your preferred editor

# Set up example scripts
cp scripts/health-check.sh.example scripts/health-check.sh
cp scripts/collect-logs.sh.example scripts/collect-logs.sh
cp scripts/deploy-to-prod.sh.example scripts/deploy-to-prod.sh
chmod +x scripts/*.sh

# Run
python bot.py
```

## 9. Run as a Systemd Service

For production use, run the bot as a systemd service:

```bash
# Edit the service file to match your paths
nano systemd/claude-telegram-bot.service

# Copy to systemd
sudo cp systemd/claude-telegram-bot.service /etc/systemd/system/

# Reload, enable, and start
sudo systemctl daemon-reload
sudo systemctl enable claude-telegram-bot
sudo systemctl start claude-telegram-bot

# Check status
sudo systemctl status claude-telegram-bot

# View logs
sudo journalctl -u claude-telegram-bot -f
```

**Important:** If you use a virtual environment, update `ExecStart` in the service file:
```ini
ExecStart=/opt/your-bot/.venv/bin/python /opt/your-bot/bot.py
```

## 10. Troubleshooting

### Bot doesn't respond

- Check the bot token is correct in `.env`
- Verify your chat ID is in `TELEGRAM_CHAT_ID`
- Check logs: `tail -f logs/telegram-bot.log`
- If running as a service: `journalctl -u claude-telegram-bot -f`

### Claude Code returns errors

- Verify Claude CLI works: `claude --print "test"`
- Check authentication: `claude login`
- Ensure the project directory exists and is accessible
- Check the PATH includes Claude Code: `which claude`

### Voice commands don't work

- Verify `GEMINI_API_KEY` is set in `.env`
- Test Gemini separately in Python:
  ```python
  from google import genai
  client = genai.Client(api_key="your-key")
  r = client.models.generate_content(model="gemini-2.5-flash", contents="Hello")
  print(r.text)
  ```

### "Session ID already in use" errors

This is normal — the bot automatically retries with a new session UUID. If it persists, restart the bot.

### Rate limiting

When Claude Code is rate-limited, the bot automatically falls back to:
1. Gemini (if `GEMINI_API_KEY` is set)
2. GPT-4o (if `OPENAI_API_KEY` is set)
3. OpenRouter (if `OPENROUTER_API_KEY` is set)

You'll see a message indicating which fallback was used.

### Permissions errors

- If running as root, ensure `HOME` is set correctly in the service file
- If running as a regular user, ensure they have read/write access to the bot directory
- Ensure the user has Claude Code authenticated

### Bot crashes on startup

- Check Python version: `python3 --version` (needs 3.10+)
- Install missing dependencies: `pip install -r requirements.txt`
- Check `.env` for syntax errors (no quotes around values, no trailing spaces)
