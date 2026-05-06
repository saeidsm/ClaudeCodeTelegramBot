# Claude Code Telegram Bot — Setup & Deployment Guide

A self-hostable Telegram bot that orchestrates Claude Code sessions on your own VPS. Supports per-session worktree isolation, model selection, and multi-bot deployment for teams.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Single-Bot Setup (yourself only)](#single-bot-setup)
3. [Multi-Bot Setup (you + collaborators)](#multi-bot-setup)
4. [Configuration Reference](#configuration-reference)
5. [Operational Tasks](#operational-tasks)
6. [Troubleshooting](#troubleshooting)
7. [Architecture Notes](#architecture-notes)

---

## Prerequisites

### VPS / Host

- **Ubuntu 22.04+** (other Linux distros likely work but untested)
- **Minimum 2 GB RAM** for a single bot with up to 4 concurrent Claude Code sessions
- **4 GB RAM** recommended if running multiple bot instances or expecting concurrent activity from collaborators
- **20 GB disk** minimum (worktrees and reports use temporary space)
- **systemd** as the service manager
- **root or sudo** access for service installation

### Software

| Software | Version | Notes |
|---|---|---|
| Python | 3.10+ | system Python is fine |
| Node.js | 18+ | required by Claude Code CLI |
| Claude Code CLI | 2.1.117+ | `--model` flag required |
| git | 2.30+ | worktree support required |
| Caddy or Nginx | latest | for serving reports (optional) |

### Anthropic Account

- A Claude subscription that includes Claude Code (Pro, Team, or Enterprise)
- The Claude Code CLI authenticated on the host (`claude login` once as root or your service user)

### Telegram

- A Telegram account
- Your numeric chat ID (send `/start` to [@userinfobot](https://t.me/userinfobot) to get it)
- One bot token per bot instance (created via [@BotFather](https://t.me/BotFather))

---

## Single-Bot Setup

This is the simplest setup — one bot for one person, one set of repos.

### Step 1 — Clone the repository

```bash
sudo mkdir -p /opt/claude-bot
sudo chown $(whoami):$(whoami) /opt/claude-bot
cd /opt/claude-bot
git clone https://github.com/saeidsm/ClaudeCodeTelegramBot.git
cd ClaudeCodeTelegramBot
```

### Step 2 — Install Python dependencies

```bash
pip3 install --break-system-packages -r requirements.txt
```

(The `--break-system-packages` flag is required on Ubuntu 24.04+ due to PEP 668. On older Ubuntu, plain `pip3 install -r requirements.txt` works.)

### Step 3 — Install Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude --version    # confirm 2.1.117+
claude login        # one-time browser auth (open the URL it prints)
```

### Step 4 — Get your Telegram credentials

1. **Bot token:** In Telegram, chat with [@BotFather](https://t.me/BotFather)
   - `/newbot`
   - Pick a display name (e.g., `My DevOps Bot`)
   - Pick a username ending in `bot` (e.g., `mydevops_bot`)
   - Copy the token it gives you (looks like `1234567890:ABC...`)

2. **Your chat ID:** Chat with [@userinfobot](https://t.me/userinfobot) and send `/start`. It replies with your numeric ID.

### Step 5 — Create the data directory layout

The bot needs a working directory for repos, reports, configs, and logs. The default layout is `/opt/shahrzad-devops/` but you can override.

```bash
sudo mkdir -p /opt/claude-bot-data/{repos,reports,logs,configs,uploads,scripts}
sudo chown -R $(whoami):$(whoami) /opt/claude-bot-data

# Initialize empty config files
echo '{}' > /opt/claude-bot-data/configs/projects.json
echo '{}' > /opt/claude-bot-data/configs/bot-state.json
echo '{}' > /opt/claude-bot-data/configs/usage_tracker.json

# Copy the gemini prompts template if you'll use voice features
cp configs/gemini-prompts.json /opt/claude-bot-data/configs/

# Optional: a token for tokenized report URLs (security)
python3 -c "import secrets; print('REPORTS_PATH_TOKEN=' + secrets.token_hex(32))" \
    > /opt/claude-bot-data/configs/reports-token.env
```

### Step 6 — Create `.env`

```bash
nano /opt/claude-bot-data/.env
```

Paste and edit:

```bash
# ── Telegram ──
TELEGRAM_BOT_TOKEN=<paste-bot-token-from-botfather>
TELEGRAM_CHAT_ID=<your-numeric-chat-id>

# ── AI keys (optional but recommended) ──
GEMINI_API_KEY=         # for voice transcription + casual command refinement
OPENAI_API_KEY=         # for fallback when Claude rate-limited
OPENROUTER_API_KEY=     # for additional fallback model options

# ── GitHub (optional) ──
# Personal Access Token if Claude Code will push to your repos
BOT_GIT_REMOTE_TOKEN=

# ── Paths (override defaults if your data dir differs) ──
BOT_DATA_ROOT=/opt/claude-bot-data
BOT_REPOS=/opt/claude-bot-data/repos
BOT_REPORTS=/opt/claude-bot-data/reports
BOT_LOGS=/opt/claude-bot-data/logs
BOT_CONFIGS_DIR=/opt/claude-bot-data/configs
BOT_UPLOADS=/opt/claude-bot-data/uploads
BOT_WORKTREE_ROOT=/tmp/claude-sessions

# ── Feature flags ──
BOT_DEPLOY_ENABLED=true             # enable /deploy button + voice command
BOT_NIGHTWATCH_IPC_ENABLED=false    # only enable if you also run NightWatch
BOT_MAX_SESSIONS=4                  # concurrent sessions per chat

# ── Default Claude Code model ──
# "" = CLI default (latest Sonnet/Opus per your plan)
# "sonnet" = explicit Sonnet
# "opus" = explicit Opus
# "claude-sonnet-4-6" = pinned model name
BOT_DEFAULT_CLAUDE_MODEL=
```

Lock the file:

```bash
chmod 600 /opt/claude-bot-data/.env
```

### Step 7 — Deploy bot Python file

The systemd service runs `bot.py` from a fixed scripts location:

```bash
sudo cp /opt/claude-bot/ClaudeCodeTelegramBot/bot.py /opt/claude-bot-data/scripts/claude-telegram-bot.py
sudo chmod +x /opt/claude-bot-data/scripts/claude-telegram-bot.py
```

### Step 8 — Install systemd service

Create `/etc/systemd/system/claude-telegram-bot.service`:

```ini
[Unit]
Description=Claude Code Telegram Bot
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/claude-bot-data/scripts/claude-telegram-bot.py
WorkingDirectory=/opt/claude-bot-data
Restart=always
RestartSec=5
Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=/opt/claude-bot-data/.env

# Memory limits (adjust to your VPS)
MemoryHigh=2G
MemoryMax=2500M

# Graceful shutdown
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-telegram-bot.service
sudo systemctl status claude-telegram-bot.service
journalctl -u claude-telegram-bot.service -f --since '1 minute ago'
```

You should see:

```
🚀 Starting Shahrzad DevOps Bot v4 (Multi-Session)...
Bot v4 ready. Gemini=✅ | GPT fallback=✅
Application started
```

### Step 9 — Install worktree garbage collection

The bot creates per-session worktrees in `/tmp/claude-sessions/` and removes them on session end. As a safety net, install a GC timer:

```bash
sudo cp systemd/claude-worktree-gc.service /etc/systemd/system/
sudo cp systemd/claude-worktree-gc.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-worktree-gc.timer
systemctl list-timers | grep worktree   # confirm scheduled at 02:00 UTC daily
```

### Step 10 — First test from Telegram

In Telegram, find your bot (`@<your_bot_username>`) and start a chat:

1. Send `/start` — you should see a welcome message
2. Send `/new test`
3. Pick a project (or create one — first-time use shows empty list)
4. Send a prompt like `pwd && git status`
5. The bot should reply with output from inside the worktree

If the bot doesn't respond, check `journalctl -u claude-telegram-bot.service -f` for errors.

---

## Multi-Bot Setup

For teams: you (admin) keep your existing bot, and each collaborator gets their own bot instance with isolated repos, configs, and limited permissions.

### Why a separate bot per person?

- **Isolation:** collaborators can't see your repos, can't deploy to your servers, can't access your secrets
- **Independent quotas:** each instance has its own MAX_SESSIONS, GitHub token, AI API keys
- **Independent Telegram bots:** each person uses a separate `@<botname>` so chats don't mix
- **Single binary:** all instances run the same `bot.py` — config differences are env-driven

### Step 1 — Confirm prerequisites for collaborator

- Collaborator's Telegram chat ID (they get it from [@userinfobot](https://t.me/userinfobot))
- Collaborator's GitHub username (if they want to commit/push)
- Decision on default model — `sonnet` is a sensible default for cost-conscious deployment

### Step 2 — Create a new bot in BotFather

You (the admin) create the bot in your Telegram account. Settings can be transferred to the collaborator later via BotFather's `/transfer` command if desired.

1. Chat with [@BotFather](https://t.me/BotFather)
2. `/newbot`
3. Name: e.g., `MyTeam Collab Bot`
4. Username: e.g., `myteam_collab_bot` (must end in `bot`)
5. Copy the new token

### Step 3 — Create collab data directory

```bash
sudo mkdir -p /opt/claude-bot-data/repos-collab \
              /opt/claude-bot-data/reports-collab \
              /opt/claude-bot-data/logs-collab \
              /opt/claude-bot-data/configs-collab \
              /opt/claude-bot-data/uploads-collab
sudo chown -R $(whoami):$(whoami) /opt/claude-bot-data/*-collab

echo '{}' > /opt/claude-bot-data/configs-collab/projects.json
echo '{}' > /opt/claude-bot-data/configs-collab/bot-state.json
echo '{}' > /opt/claude-bot-data/configs-collab/usage_tracker.json

cp /opt/claude-bot-data/configs/gemini-prompts.json \
   /opt/claude-bot-data/configs-collab/

# Random token for collab reports
python3 -c "import secrets; t = secrets.token_hex(32); print(f'REPORTS_PATH_TOKEN={t}')" \
    > /opt/claude-bot-data/configs-collab/reports-token.env
chmod 600 /opt/claude-bot-data/configs-collab/reports-token.env
```

### Step 4 — Create `.env.collab`

```bash
nano /opt/claude-bot-data/.env.collab
```

Paste and edit:

```bash
# ── Telegram ──
TELEGRAM_BOT_TOKEN=<paste-collab-bot-token>
TELEGRAM_CHAT_ID=<collaborator-chat-id>

# ── AI keys ──
# Collaborator provides their own (or leaves empty to disable features)
GEMINI_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=

# ── GitHub ──
# Collaborator's PAT (scoped to THEIR own repos only)
BOT_GIT_REMOTE_TOKEN=

# ── Paths (KEEP these — they isolate this bot from yours) ──
BOT_DATA_ROOT=/opt/claude-bot-data
BOT_REPOS=/opt/claude-bot-data/repos-collab
BOT_REPORTS=/opt/claude-bot-data/reports-collab
BOT_LOGS=/opt/claude-bot-data/logs-collab
BOT_CONFIGS_DIR=/opt/claude-bot-data/configs-collab
BOT_UPLOADS=/opt/claude-bot-data/uploads-collab
BOT_WORKTREE_ROOT=/tmp/claude-sessions-collab

# ── Feature flags (collaborator-restricted) ──
BOT_DEPLOY_ENABLED=false             # collaborator can't deploy to your servers
BOT_NIGHTWATCH_IPC_ENABLED=false     # only the admin bot binds the IPC port
BOT_MAX_SESSIONS=1                   # one session at a time

# ── Default Claude Code model (Sonnet for token economy) ──
BOT_DEFAULT_CLAUDE_MODEL=sonnet

# ── Production-server access — DELIBERATELY ABSENT ──
# Don't include PROD_HOST or any SSH key references for the collab bot
```

```bash
chmod 600 /opt/claude-bot-data/.env.collab
```

### Step 5 — Create the collab systemd service

`/etc/systemd/system/claude-telegram-bot-collab.service`:

```ini
[Unit]
Description=Claude Code Telegram Bot — COLLAB instance
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/claude-bot-data/scripts/claude-telegram-bot.py
WorkingDirectory=/opt/claude-bot-data
Restart=always
RestartSec=5
Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=/opt/claude-bot-data/.env.collab

# Memory limits — smaller for 1-session collab instance
MemoryHigh=800M
MemoryMax=1G

KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Note: the `ExecStart` is the **same** `bot.py` file as the main bot. The two services differ only by `EnvironmentFile`.

### Step 6 — Start the collab bot

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-telegram-bot-collab.service
sudo systemctl status claude-telegram-bot-collab.service
journalctl -u claude-telegram-bot-collab.service --since '1 minute ago' --no-pager | tail -20
```

### Step 7 — Collaborator's first test

Collaborator sends `/start` to their bot in Telegram:

- Welcome message appears
- `/new test` works
- Empty project list (collaborator creates their own)
- `/deploy` button (if it appears) returns `🔒 Deploy is disabled for this bot instance`
- `/new` rejects a second concurrent session (`MAX_SESSIONS=1`)

### Step 8 — Collaborator's GitHub setup (optional)

If collaborator wants Claude Code to push commits:

1. Collaborator generates a PAT on GitHub:
   - GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
   - Scopes: `repo`
   - Copy the token
2. Admin updates `.env.collab`:
   ```bash
   nano /opt/claude-bot-data/.env.collab
   # update BOT_GIT_REMOTE_TOKEN=ghp_xxx
   sudo systemctl restart claude-telegram-bot-collab.service
   ```

---

## Configuration Reference

### Telegram

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Comma-separated allowed chat IDs |

### AI Keys (all optional)

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Voice transcription, casual prompt refinement |
| `OPENAI_API_KEY` | GPT-4o fallback when Claude rate-limited |
| `OPENROUTER_API_KEY` | Access to OpenRouter models for fallback |

### Paths (all default to `/opt/shahrzad-devops` — override per instance)

| Variable | Default | Description |
|---|---|---|
| `BOT_DATA_ROOT` | `/opt/shahrzad-devops` | Top-level data directory |
| `BOT_REPOS` | `${BOT_DATA_ROOT}/repos` | Where projects are checked out |
| `BOT_REPORTS` | `${BOT_DATA_ROOT}/reports` | Where Claude saves reports |
| `BOT_LOGS` | `${BOT_DATA_ROOT}/logs` | Bot operational logs |
| `BOT_CONFIGS_DIR` | `${BOT_DATA_ROOT}/configs` | Bot config files |
| `BOT_UPLOADS` | `${BOT_DATA_ROOT}/uploads` | Telegram file uploads land here |
| `BOT_SCRIPTS` | `${BOT_DATA_ROOT}/scripts` | Helper scripts (deploy, etc.) |
| `BOT_WORKTREE_ROOT` | `/tmp/claude-sessions` | Per-session ephemeral worktrees |

### Feature Flags

| Variable | Default | Description |
|---|---|---|
| `BOT_DEPLOY_ENABLED` | `true` | Allow `/deploy` command and button |
| `BOT_NIGHTWATCH_IPC_ENABLED` | `true` | Bind the IPC port for NightWatch integration |
| `BOT_MAX_SESSIONS` | `4` | Concurrent sessions per chat |
| `BOT_DEFAULT_CLAUDE_MODEL` | `""` (CLI default) | `sonnet`, `opus`, or full model name |

### Other

| Variable | Description |
|---|---|
| `BOT_GIT_REMOTE_TOKEN` | GitHub PAT for git operations |

---

## Operational Tasks

### Update the bot

```bash
cd /opt/claude-bot/ClaudeCodeTelegramBot
git pull origin main
sudo cp bot.py /opt/claude-bot-data/scripts/claude-telegram-bot.py
sudo systemctl restart claude-telegram-bot.service
# repeat for collab if running
sudo systemctl restart claude-telegram-bot-collab.service
```

### Update Claude Code CLI

```bash
npm update -g @anthropic-ai/claude-code
claude --version
sudo systemctl restart claude-telegram-bot.service
```

### View logs

```bash
journalctl -u claude-telegram-bot.service -f
journalctl -u claude-telegram-bot-collab.service -f
```

### Inspect active worktrees

```bash
ls -la /tmp/claude-sessions/        # main bot
ls -la /tmp/claude-sessions-collab/ # collab bot
```

### Manual GC trigger

```bash
sudo systemctl start claude-worktree-gc.service
journalctl -u claude-worktree-gc.service --since '5 minutes ago'
```

### Backup configs

The bot's state is in `${BOT_CONFIGS_DIR}/`:

- `projects.json` — registered projects per chat
- `bot-state.json` — active sessions (auto-saved every 30s)
- `usage_tracker.json` — Claude usage statistics

```bash
tar czf /tmp/bot-backup-$(date +%Y%m%d).tar.gz \
    /opt/claude-bot-data/.env \
    /opt/claude-bot-data/configs/
```

### Add a project to the bot

Inside Telegram:

1. `/new my-session` (creates session)
2. Pick `📁 New Project`
3. Type the project name
4. Bot creates `${BOT_REPOS}/<name>/`
5. SSH into the host, `git clone` your repo into that directory:
   ```bash
   cd /opt/claude-bot-data/repos/<name>
   git clone https://github.com/youruser/yourrepo.git .
   ```

(In a future release the bot may handle the clone itself.)

---

## Troubleshooting

### Bot doesn't respond to `/start`

```bash
sudo systemctl status claude-telegram-bot.service
journalctl -u claude-telegram-bot.service --since '5 minutes ago' --no-pager
```

Common causes:
- Wrong `TELEGRAM_CHAT_ID` (bot ignores unknown chats with no error)
- `TELEGRAM_BOT_TOKEN` invalid or revoked
- Service not running

### `worktree.add_failed` in journal

Likely causes:
- Disk full (`df -h`)
- Project not a git repo (the bot rsyncs non-git projects instead)
- Branch doesn't exist on origin (bot falls back to `main`)

### Claude Code spawn fails immediately

```bash
claude --version    # should be 2.1.117+
claude --help | grep model    # confirm --model flag
which claude
ls -la ~/.claude/   # confirm session records exist
```

If `claude` is not found by the bot, check the `PATH` in the systemd unit file.

### Two bot instances try to bind the same NightWatch port

Set `BOT_NIGHTWATCH_IPC_ENABLED=false` on the collab `.env`. Only the main bot should bind port 9091.

### `/deploy` button appears but does nothing

Check `BOT_DEPLOY_ENABLED` in the right `.env`. After changing, `systemctl restart` the service.

### Voice features don't work

- Confirm `GEMINI_API_KEY` is set in the relevant `.env`
- Check journal for `Voice: ❌` on bot startup — means key is missing or invalid

---

## Architecture Notes

### Per-session worktree isolation

Every Claude Code spawn creates an ephemeral git worktree at `${BOT_WORKTREE_ROOT}/<session_id>` based on `origin/<branch>` (default: `main`). The worktree is on a detached HEAD — to commit, Claude Code creates a branch inside the worktree and pushes it.

When the session ends, the worktree is removed via `git worktree remove --force` plus `rm -rf` as fallback.

This eliminates cross-session contamination: two sessions on the same project don't fight over `.git/HEAD`.

### Multi-bot pattern

Multiple `*.service` files run the same `bot.py` with different `EnvironmentFile`. Each instance's `BOT_DATA_ROOT`, `BOT_WORKTREE_ROOT`, etc., diverge so file-system state is isolated.

### Session state persistence

Sessions are saved to `${BOT_CONFIGS_DIR}/bot-state.json` every 30 seconds. On bot restart, sessions are restored from this file. Claude Code's own session UUIDs persist in `~/.claude/` and survive restarts.

### Reports

When Claude Code completes a task and `Save Report` is tapped, the result is written to `${BOT_REPORTS}/<token>/<project>/<timestamp>/`. If you want the report URL to be public, configure Caddy or Nginx to serve `${BOT_REPORTS}/<token>/` (the token-gated path acts as a soft access control).

---

## License & Contributing

See [LICENSE](LICENSE) and contribute via PRs to https://github.com/saeidsm/ClaudeCodeTelegramBot.

For issues, open a GitHub issue.

## Related

- [NightWatch IPC](docs/NIGHTWATCH_IPC.md) — integrate with NightWatch monitoring
- [ARCHITECTURE](docs/ARCHITECTURE.md) — internal design
- [COMMANDS](docs/COMMANDS.md) — full Telegram command reference
