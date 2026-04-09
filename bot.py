#!/usr/bin/env python3
"""
Claude Code Telegram Bot v4
Multi-Session | Voice (Gemini STT + LLM refinement) | Files | Rich UI

A Telegram bot that bridges Claude Code CLI with Telegram for DevOps workflows.
Supports multi-session management, voice commands (Farsi/English), file attachments,
multi-provider fallback, and production deployment controls.

https://github.com/saeidsm/ClaudeCodeTelegramBot
"""

import os, sys, json, asyncio, subprocess, logging, html, base64, uuid, time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

from telegram import (
    Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, Message
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.constants import ParseMode, ChatAction

# ── Gemini ──
try:
    from google import genai
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_OK = bool(GEMINI_API_KEY)
    if GEMINI_OK:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except ImportError:
    GEMINI_OK = False

# ── Config (all paths configurable via env vars) ──
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS = [int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]

BASE_DIR   = os.environ.get("BOT_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
REPOS      = os.environ.get("BOT_REPOS_DIR", f"{BASE_DIR}/repos")
REPORTS    = os.environ.get("BOT_REPORTS_DIR", f"{BASE_DIR}/reports")
LOGS       = os.environ.get("BOT_LOGS_DIR", f"{BASE_DIR}/logs")
SCRIPTS    = os.environ.get("BOT_SCRIPTS_DIR", f"{BASE_DIR}/scripts")
UPLOADS    = os.environ.get("BOT_UPLOADS_DIR", f"{BASE_DIR}/uploads")
PROMPTS_FILE = os.environ.get("BOT_PROMPTS_FILE", f"{BASE_DIR}/configs/gemini-prompts.json")
REPORT_URL = os.environ.get("BOT_REPORT_URL", "https://your-server.com/reports")

DEFAULT_PROJECT = os.environ.get("BOT_DEFAULT_PROJECT", "MyProject")
PAUSE_SECONDS   = int(os.environ.get("BOT_PAUSE_SECONDS", "5"))
MAX_SESSIONS    = int(os.environ.get("BOT_MAX_SESSIONS", "3"))
SESSION_TIMEOUT_MINUTES = int(os.environ.get("BOT_SESSION_TIMEOUT_HOURS", "72")) * 60

# Projects that never auto-close (comma-separated)
PERMANENT_PROJECTS = set(
    p.strip() for p in os.environ.get("BOT_PERMANENT_PROJECTS", "").split(",") if p.strip()
)

USAGE_DB_PATH  = os.environ.get("BOT_USAGE_DB", f"{BASE_DIR}/configs/usage_tracker.json")
PROJECTS_FILE  = os.environ.get("BOT_PROJECTS_FILE", f"{BASE_DIR}/configs/projects.json")

# ── OpenAI / OpenRouter (fallback providers) ──
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GPT_FALLBACK_MODEL = os.environ.get("BOT_GPT_FALLBACK_MODEL", "gpt-4o")

# Active fallback model (can be changed at runtime via /model)
ACTIVE_FALLBACK = {"provider": "gemini", "model": ""}

# ── Logging ──
for d in [LOGS, UPLOADS, os.path.dirname(PROMPTS_FILE)]:
    os.makedirs(d, exist_ok=True)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO,
    handlers=[logging.FileHandler(f"{LOGS}/telegram-bot.log"), logging.StreamHandler()])
log = logging.getLogger(__name__)

# ── Load Gemini Prompts (editable file) ──
def load_prompts():
    try:
        with open(PROMPTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "transcribe": {"model": "gemini-2.5-flash", "prompt": "Transcribe this voice message exactly. Farsi or English. Return ONLY the transcription."},
            "refine": {"model": "gemini-2.5-flash", "prompt": "Convert this casual command into a structured Claude Code prompt:\n\"{text}\"\n\nReturn ONLY the prompt."},
            "voice_commands": {}
        }

def get_voice_commands():
    p = load_prompts().get("voice_commands", {})
    mapping = {}
    for cmd, triggers in p.items():
        for t in triggers:
            mapping[t.lower()] = cmd
    return mapping

# ═══════════════════════════════════════════
#  Usage Tracker
# ═══════════════════════════════════════════
class UsageTracker:
    """Track estimated token usage per hour/day/week with JSON persistence."""

    def __init__(self, db_path=USAGE_DB_PATH):
        self.db_path = db_path
        self.hourly_limit = int(os.environ.get("BOT_HOURLY_TOKEN_LIMIT", "100000"))
        self.daily_limit  = int(os.environ.get("BOT_DAILY_TOKEN_LIMIT", "1000000"))
        self.weekly_limit = int(os.environ.get("BOT_WEEKLY_TOKEN_LIMIT", "5000000"))
        self.alert_threshold = 0.8    # 80%
        self._load()

    def _load(self):
        try:
            with open(self.db_path) as f:
                self.data = json.load(f)
        except Exception:
            self.data = {"records": []}

    def _save(self):
        try:
            with open(self.db_path, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            log.error(f"UsageTracker save error: {e}")

    def record(self, input_chars: int, output_chars: int, session_label: str = ""):
        tokens_est = (input_chars + output_chars) // 4
        entry = {
            "ts": time.time(),
            "tokens": tokens_est,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "session": session_label,
        }
        self.data.setdefault("records", []).append(entry)
        # Prune records older than 8 days
        cutoff = time.time() - 8 * 86400
        self.data["records"] = [r for r in self.data["records"] if r["ts"] > cutoff]
        self._save()

    def _sum_tokens(self, since_ts: float) -> int:
        return sum(r["tokens"] for r in self.data.get("records", []) if r["ts"] >= since_ts)

    def get_summary(self) -> dict:
        now = time.time()
        hourly = self._sum_tokens(now - 3600)
        daily  = self._sum_tokens(now - 86400)
        weekly = self._sum_tokens(now - 7 * 86400)
        return {
            "hourly_tokens": hourly,
            "daily_tokens":  daily,
            "weekly_tokens": weekly,
            "hourly_pct": min(hourly / self.hourly_limit, 1.0) if self.hourly_limit else 0,
            "daily_pct":  min(daily / self.daily_limit, 1.0)   if self.daily_limit  else 0,
            "weekly_pct": min(weekly / self.weekly_limit, 1.0)  if self.weekly_limit  else 0,
        }

    def should_alert(self) -> bool:
        s = self.get_summary()
        return s["hourly_pct"] > self.alert_threshold

    @staticmethod
    def format_bar(pct: float) -> str:
        filled = int(pct * 10)
        empty = 10 - filled
        bar = "█" * filled + "░" * empty
        if pct > 0.9:
            emoji = "🔴"
        elif pct > 0.7:
            emoji = "⚠️"
        else:
            emoji = "✅"
        return f"{bar} {int(pct * 100)}%  {emoji}"

    def format_usage_message(self, active_sessions: int = 0) -> str:
        s = self.get_summary()
        return (
            "📊 <b>Claude Code Usage</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"This hour:  <code>{self.format_bar(s['hourly_pct'])}</code>\n"
            f"Today:      <code>{self.format_bar(s['daily_pct'])}</code>\n"
            f"This week:  <code>{self.format_bar(s['weekly_pct'])}</code>\n\n"
            f"Sessions active: {active_sessions}\n"
            f"Tokens est. today: ~{s['daily_tokens']:,}"
        )


USAGE = UsageTracker()


# ═══════════════════════════════════════════
#  Projects Config
# ═══════════════════════════════════════════
def load_projects() -> list[dict]:
    """Load projects from JSON config. Auto-generate from REPOS if missing."""
    try:
        with open(PROJECTS_FILE) as f:
            return json.load(f)
    except Exception:
        pass
    # Auto-generate from repos directory
    projects = []
    if os.path.isdir(REPOS):
        for d in sorted(os.listdir(REPOS)):
            full = f"{REPOS}/{d}"
            if os.path.isdir(full):
                projects.append({"name": d, "path": full})
    save_projects(projects)
    return projects


def save_projects(projects: list[dict]):
    try:
        os.makedirs(os.path.dirname(PROJECTS_FILE), exist_ok=True)
        with open(PROJECTS_FILE, "w") as f:
            json.dump(projects, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save projects: {e}")


def get_project_names() -> list[str]:
    return [p["name"] for p in load_projects()]


def add_project(name: str, path: str):
    projects = load_projects()
    if any(p["name"] == name for p in projects):
        return
    projects.append({"name": name, "path": path})
    save_projects(projects)


def get_project_path(name: str) -> str:
    for p in load_projects():
        if p["name"] == name:
            return p["path"]
    return f"{REPOS}/{name}"


# ═══════════════════════════════════════════
#  Conversation State (multi-step flows)
# ═══════════════════════════════════════════
CONV_STATE: dict[int, dict] = {}


# ═══════════════════════════════════════════
#  Fallback Chain: Gemini → GPT → OpenRouter
# ═══════════════════════════════════════════
async def gemini_fallback(prompt: str, project: str) -> str:
    """Use Gemini as primary fallback for non-code tasks."""
    if not GEMINI_OK:
        return ""
    try:
        cfg = load_prompts().get("refine", {})
        model = cfg.get("model", "gemini-2.5-flash")
        r = gemini_client.models.generate_content(
            model=model,
            contents=[{"role": "user", "parts": [{"text":
                f"You are a DevOps assistant for the {project} project. "
                f"Be concise and actionable. You cannot execute code or SSH — only advise.\n\n{prompt}"
            }]}])
        short = model.split("-preview")[0] if "-preview" in model else model
        return f"🔄 <i>[Gemini/{short} fallback — Claude was rate-limited]</i>\n\n{r.text.strip()}"
    except Exception as e:
        log.error(f"Gemini fallback error: {e}")
        return ""


async def openai_fallback(prompt: str, project: str) -> str:
    """Use OpenAI GPT as secondary fallback."""
    if not OPENAI_API_KEY:
        return ""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": GPT_FALLBACK_MODEL,
                    "messages": [
                        {"role": "system", "content": f"You are a DevOps assistant for {project}. Be concise."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 4000,
                })
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return f"🔄 <i>[GPT fallback — Claude was rate-limited]</i>\n\n{content}"
    except Exception as e:
        log.error(f"OpenAI fallback error: {e}")
        return ""


async def openrouter_fallback(prompt: str, project: str, model: str = "") -> str:
    """Use OpenRouter with any model as fallback."""
    if not OPENROUTER_API_KEY:
        return ""
    model = model or "google/gemini-2.5-flash"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": f"You are a DevOps assistant for {project}. Be concise."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 4000,
                })
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            short_model = model.split("/")[-1] if "/" in model else model
            return f"🔄 <i>[OpenRouter/{short_model} — Claude was rate-limited]</i>\n\n{content}"
    except Exception as e:
        log.error(f"OpenRouter fallback error: {e}")
        return ""


async def gpt_fallback(prompt: str, project: str) -> str:
    """Fallback chain: active model → gemini → openai → openrouter → error."""
    provider = ACTIVE_FALLBACK.get("provider", "gemini")
    model = ACTIVE_FALLBACK.get("model", "")

    if provider == "gemini":
        r = await gemini_fallback(prompt, project)
        if r: return r
    elif provider == "openai":
        r = await openai_fallback(prompt, project)
        if r: return r
    elif provider == "openrouter":
        r = await openrouter_fallback(prompt, project, model)
        if r: return r

    if provider != "gemini":
        r = await gemini_fallback(prompt, project)
        if r: return r
    if provider != "openai":
        r = await openai_fallback(prompt, project)
        if r: return r
    if provider != "openrouter":
        r = await openrouter_fallback(prompt, project)
        if r: return r

    return "⚠️ Claude rate-limited. All fallbacks failed. Wait for rate limit to reset."


# ═══════════════════════════════════════════
#  Session Model
# ═══════════════════════════════════════════
SESSION_COLORS = ["🔵", "🟢", "🟡", "🟠", "🔴", "🟣"]

@dataclass
class Session:
    id: str
    label: str
    color_emoji: str
    session_uuid: str
    project: str = DEFAULT_PROJECT
    status: str = "idle"             # idle | running | completed | error
    started_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_ids: list = field(default_factory=list)
    anchor_message_id: Optional[int] = None
    files: list = field(default_factory=list)
    out: str = ""
    tasks: int = 0
    paused: bool = False
    voice_text: str = ""


class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.msg_to_session: dict[int, str] = {}
        self.color_index: dict[int, int] = {}

    def _next_color(self, chat_id: int) -> str:
        idx = self.color_index.get(chat_id, 0)
        color = SESSION_COLORS[idx % len(SESSION_COLORS)]
        self.color_index[chat_id] = idx + 1
        return color

    def _key(self, chat_id: int, label: str) -> str:
        return f"{chat_id}:{label}"

    def create(self, chat_id: int, label: str) -> Session:
        key = self._key(chat_id, label)
        if key in self.sessions:
            self.kill(chat_id, label)
        color = self._next_color(chat_id)
        session = Session(
            id=key, label=label, color_emoji=color,
            session_uuid=str(uuid.uuid4()),
        )
        self.sessions[key] = session
        return session

    def get(self, chat_id: int, label: str) -> Optional[Session]:
        return self.sessions.get(self._key(chat_id, label))

    def get_by_key(self, key: str) -> Optional[Session]:
        return self.sessions.get(key)

    def register_message(self, msg_id: int, session_key: str):
        self.msg_to_session[msg_id] = session_key

    def find_by_message(self, msg_id: int) -> Optional[Session]:
        key = self.msg_to_session.get(msg_id)
        if key:
            return self.sessions.get(key)
        return None

    def active_for_chat(self, chat_id: int) -> list[Session]:
        prefix = f"{chat_id}:"
        return [s for k, s in self.sessions.items()
                if k.startswith(prefix) and s.status != "completed"]

    def kill(self, chat_id: int, label: str) -> bool:
        key = self._key(chat_id, label)
        session = self.sessions.pop(key, None)
        if session:
            to_remove = [mid for mid, sk in self.msg_to_session.items() if sk == key]
            for mid in to_remove:
                del self.msg_to_session[mid]
            return True
        return False

    def get_default(self, chat_id: int) -> Optional[Session]:
        active = self.active_for_chat(chat_id)
        if len(active) == 1:
            return active[0]
        return None

    def cleanup_timed_out(self, timeout_minutes: int = SESSION_TIMEOUT_MINUTES) -> list[Session]:
        cutoff = time.time() - timeout_minutes * 60
        timed_out = []
        keys_to_remove = []
        for key, session in self.sessions.items():
            if session.project in PERMANENT_PROJECTS:
                continue
            if session.status != "running" and session.last_active < cutoff:
                timed_out.append(session)
                keys_to_remove.append(key)
        for key in keys_to_remove:
            self.sessions.pop(key, None)
            to_remove = [mid for mid, sk in self.msg_to_session.items() if sk == key]
            for mid in to_remove:
                del self.msg_to_session[mid]
        return timed_out

    def can_create(self, chat_id: int) -> bool:
        return len(self.active_for_chat(chat_id)) < MAX_SESSIONS


SM = SessionManager()
PENDING_MESSAGES: dict[int, dict] = {}

# ═══════════════════════════════════════════
#  UI
# ═══════════════════════════════════════════
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Health", "📋 Logs"],
        ["📁 Projects", "🆕 New Session"],
        ["📈 Usage", "❓ Help"],
    ],
    resize_keyboard=True,
)

def menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Usage", callback_data="menu:usage"),
            InlineKeyboardButton("📋 Sessions", callback_data="menu:sessions"),
        ],
        [
            InlineKeyboardButton("🆕 New Session", callback_data="menu:new_session"),
            InlineKeyboardButton("🏥 Health", callback_data="menu:health"),
        ],
    ])

def project_kb(session_key: str = ""):
    ps = get_project_names()
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"📦 {p}", callback_data=f"proj:{session_key}:{p}")] for p in ps])

def new_session_project_kb(label: str, has_name: bool = True):
    ps = get_project_names()
    prefix = "newproj" if has_name else "newneed"
    rows = [[InlineKeyboardButton(f"📁 {p}", callback_data=f"{prefix}:{label}:{p}")] for p in ps]
    rows.append([InlineKeyboardButton("➕ New project...", callback_data=f"addproj:{label}:{'1' if has_name else '0'}")])
    return InlineKeyboardMarkup(rows)

def sessions_kill_kb(sessions: list):
    rows = [[InlineKeyboardButton(f"🗑 {s.color_emoji} {s.label}", callback_data=f"skill:{s.id}")] for s in sessions]
    return InlineKeyboardMarkup(rows)

def kill_picker_kb(sessions: list):
    rows = [[InlineKeyboardButton(f"🗑 {s.color_emoji} {s.label}", callback_data=f"skill:{s.id}")] for s in sessions]
    return InlineKeyboardMarkup(rows)

def route_picker_kb(sessions: list):
    buttons = [InlineKeyboardButton(f"{s.color_emoji} {s.label}", callback_data=f"route:{s.id}") for s in sessions]
    return InlineKeyboardMarkup([buttons])

def after_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Save Report", callback_data="do:report"),
         InlineKeyboardButton("🚀 Deploy", callback_data="do:deploy_ask")],
        [InlineKeyboardButton("📊 Health", callback_data="do:health")]])

def deploy_branch_kb(proj):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 main", callback_data=f"dbr:{proj}:main")],
        [InlineKeyboardButton("🔧 dev", callback_data=f"dbr:{proj}:dev")],
        [InlineKeyboardButton("🤖 latest claude/*", callback_data=f"dbr:{proj}:claude_latest")],
        [InlineKeyboardButton("❌ Cancel", callback_data="do:cancel")]])

def deploy_confirm_kb(proj, branch):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Deploy {proj}@{branch}", callback_data=f"dpl:{proj}:{branch}"),
        InlineKeyboardButton("❌ Cancel", callback_data="do:cancel")]])

def pause_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏸ Pause & Edit", callback_data="do:pause")]])

def voice_confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Send to Claude", callback_data="do:voice_go")],
        [InlineKeyboardButton("✏️ Edit first", callback_data="do:voice_edit")]])

# ═══════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════
esc = html.escape

def fmt_out(text, mx=3800):
    if not text: return "<i>No output</i>"
    t = len(text) > mx
    if t: text = text[:mx]
    r = f"<pre>{esc(text)}</pre>"
    if t: r += "\n\n✂️ <i>Truncated. Tap 📦 Save Report for full.</i>"
    return r

def fmt_links(links):
    return "\n\n".join(f"🔗 <b>{k}:</b>\n<code>{v}</code>" for k, v in links.items())

def session_prefix(session: Session) -> str:
    return f"{session.color_emoji} <b>{esc(session.label)}</b>"

def elapsed_str(ts: float) -> str:
    d = int(time.time() - ts)
    if d < 60: return f"{d}s"
    if d < 3600: return f"{d // 60}m"
    return f"{d // 3600}h {(d % 3600) // 60}m"

# ═══════════════════════════════════════════
#  Auth
# ═══════════════════════════════════════════
def authorized(fn):
    async def w(update, context):
        cid = update.effective_chat.id if update.effective_chat else update.callback_query.message.chat.id
        if ALLOWED_IDS and cid not in ALLOWED_IDS:
            if update.message: await update.message.reply_text("⛔")
            elif update.callback_query: await update.callback_query.answer("⛔", show_alert=True)
            return
        return await fn(update, context)
    return w

# ═══════════════════════════════════════════
#  Claude Code
# ═══════════════════════════════════════════
async def run_claude(prompt, project, session_uuid, files=None, session_label=""):
    repo = get_project_path(project)
    if not os.path.isdir(repo):
        repo = f"{REPOS}/{project}"
    if not os.path.isdir(repo):
        try:
            os.makedirs(repo, exist_ok=True)
            log.info(f"Created project directory: {repo}")
        except Exception as e:
            return f"❌ Not found: {project}\nCould not create: {e}"
    fnote = ""
    if files:
        td = f"{repo}/.claude-tasks"; os.makedirs(td, exist_ok=True)
        copied = []
        for fp in files:
            if os.path.isfile(fp):
                d = f"{td}/{os.path.basename(fp)}"; subprocess.run(["cp", fp, d]); copied.append(d)
        if copied:
            fnote = "\n\n[ATTACHED FILES — read before starting]\n" + "\n".join(f"  - {f}" for f in copied) + "\n"
    full_prompt = prompt + fnote
    cmd = ["claude", "--print", "--session-id", session_uuid, full_prompt]
    log.info(f"Claude [{project}] session={session_uuid[:8]}: {prompt[:80]}... ({len(files or [])} files)")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": os.path.expanduser("~"),
                 "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")})
        out, err = await asyncio.wait_for(proc.communicate(), timeout=3600)
        r = out.decode("utf-8", errors="replace")
        e = err.decode("utf-8", errors="replace") if err else ""

        # Handle "Session ID already in use" — retry with new UUID
        if proc.returncode != 0 and "already in use" in (r + e).lower():
            log.warning(f"Session ID {session_uuid[:8]} in use, retrying with new UUID")
            new_uuid = str(uuid.uuid4())
            cmd[cmd.index(session_uuid)] = new_uuid
            proc2 = await asyncio.create_subprocess_exec(
                *cmd, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "HOME": os.path.expanduser("~"),
                     "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")})
            out2, err2 = await asyncio.wait_for(proc2.communicate(), timeout=3600)
            r = out2.decode("utf-8", errors="replace")
            e = err2.decode("utf-8", errors="replace") if err2 else ""
            proc = proc2

        USAGE.record(len(full_prompt), len(r), session_label)

        rate_limited = False
        if proc.returncode != 0:
            combined = (r + e).lower()
            if any(kw in combined for kw in ["rate limit", "quota", "overloaded", "429", "too many requests"]):
                rate_limited = True

        if rate_limited:
            log.warning(f"Claude rate-limited for session {session_uuid[:8]}, falling back")
            return await gpt_fallback(prompt, project)

        if proc.returncode != 0 and e:
            r += f"\n⚠️ {e[:500]}"
        return r.strip() or "(no output)"
    except asyncio.TimeoutError:
        try: proc.kill()
        except: pass
        return "⏰ Timeout (60 min)."
    except Exception as e:
        return f"❌ {e}"

# ═══════════════════════════════════════════
#  Gemini: Transcribe + Refine
# ═══════════════════════════════════════════
async def transcribe(path):
    if not GEMINI_OK: return None
    try:
        cfg = load_prompts().get("transcribe", {})
        with open(path, "rb") as f: data = f.read()
        r = gemini_client.models.generate_content(
            model=cfg.get("model", "gemini-2.5-flash"),
            contents=[{"role": "user", "parts": [
                {"text": cfg.get("prompt", "Transcribe this voice message.")},
                {"inline_data": {"mime_type": "audio/ogg", "data": base64.b64encode(data).decode()}}
            ]}])
        return r.text.strip()
    except Exception as e:
        log.error(f"Transcribe error: {e}"); return None

async def refine_prompt(text):
    if not GEMINI_OK: return text
    try:
        cfg = load_prompts().get("refine", {})
        template = cfg.get("prompt", "Refine: \"{text}\"")
        filled = template.replace("{text}", text)
        r = gemini_client.models.generate_content(
            model=cfg.get("model", "gemini-2.5-flash"),
            contents=[{"role": "user", "parts": [{"text": filled}]}])
        return r.text.strip()
    except Exception as e:
        log.error(f"Refine error: {e}"); return text

def match_cmd(text):
    cmds = get_voice_commands()
    import re
    tl = text.lower().strip()
    tl = re.sub(r"[.,!?؟،؛]", "", tl).strip()
    tl = tl.replace('ي', 'ی').replace('ك', 'ک')
    if tl in cmds: return cmds[tl]
    for w in tl.split():
        if w in cmds: return cmds[w]
    for trigger, cmd in cmds.items():
        if trigger in tl: return cmd
    return None

# ═══════════════════════════════════════════
#  Reports
# ═══════════════════════════════════════════
async def make_report(name, content):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S"); slug = f"{name}-{ts}"
    rd = f"{REPORTS}/{slug}"; os.makedirs(rd, exist_ok=True)
    with open(f"{rd}/summary.txt", "w") as f: f.write(content)
    subprocess.run(["zip", "-r", f"{REPORTS}/{slug}.zip", slug], cwd=REPORTS, capture_output=True)
    return {"Summary": f"{REPORT_URL}/{slug}/summary.txt",
            "Browse": f"{REPORT_URL}/{slug}/", "ZIP": f"{REPORT_URL}/{slug}.zip"}

# ═══════════════════════════════════════════
#  Send
# ═══════════════════════════════════════════
async def send_long(msg, text, pm=ParseMode.HTML, rm=None):
    mx = 4000
    if len(text) <= mx:
        return await msg.reply_text(text, parse_mode=pm, reply_markup=rm, disable_web_page_preview=True)
    chunks = []
    while text:
        if len(text) <= mx: chunks.append(text); break
        c = text.rfind("\n", 0, mx)
        if c == -1: c = mx
        chunks.append(text[:c]); text = text[c:].lstrip("\n")
    last = None
    for i, ch in enumerate(chunks):
        last = await msg.reply_text(ch, parse_mode=pm,
            reply_markup=rm if i == len(chunks)-1 else None, disable_web_page_preview=True)
        await asyncio.sleep(0.3)
    return last

async def download_doc(doc, ctx):
    fn = doc.file_name or f"file_{doc.file_unique_id}"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    lp = f"{UPLOADS}/{ts}_{fn}"
    tf = await ctx.bot.get_file(doc.file_id); await tf.download_to_drive(lp)
    log.info(f"Downloaded: {fn}"); return lp

# ═══════════════════════════════════════════
#  Session Resolution
# ═══════════════════════════════════════════
def resolve_session(update) -> Optional[Session]:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return None
    if msg.reply_to_message:
        reply_id = msg.reply_to_message.message_id
        session = SM.find_by_message(reply_id)
        if session:
            return session
    cid = msg.chat.id
    return SM.get_default(cid)


async def track_reply(sent_msg: Message, session: Session):
    if sent_msg:
        SM.register_message(sent_msg.message_id, session.id)
        session.message_ids.append(sent_msg.message_id)

# ═══════════════════════════════════════════
#  Core: Execute with Pause Window
# ═══════════════════════════════════════════
async def execute(update, context, prompt, session: Session, files=None, is_voice=False):
    proj = session.project; files = files or []
    fi = f"\n📎 {len(files)} file(s)" if files else ""
    pfx = session_prefix(session)

    # Pause window
    session.paused = False
    cm = await update.message.reply_text(
        f"{pfx} | {'🎤 Voice' if is_voice else '💬 Message'} received{fi}\n⏳ Starting in {PAUSE_SECONDS}s...\n\n<i>Tap to cancel:</i>",
        parse_mode=ParseMode.HTML, reply_markup=pause_kb())
    await track_reply(cm, session)

    for i in range(PAUSE_SECONDS, 0, -1):
        await asyncio.sleep(1)
        if session.paused: return
        try:
            await cm.edit_text(
                f"{pfx} | {'🎤 Voice' if is_voice else '💬 Message'} received{fi}\n⏳ Starting in {i-1}s...",
                parse_mode=ParseMode.HTML, reply_markup=pause_kb() if i > 1 else None)
        except: pass
    if session.paused: return

    # Refine if voice
    if is_voice and len(prompt.split()) > 3:
        await cm.edit_text(f"{pfx} | 🧠 <b>Refining prompt...</b>", parse_mode=ParseMode.HTML)
        refined = await refine_prompt(prompt)
        if refined and refined != prompt:
            prompt = refined
            r = await update.message.reply_text(
                f"{pfx} | 📝 <b>Refined:</b>\n<pre>{esc(prompt[:2000])}</pre>",
                parse_mode=ParseMode.HTML)
            await track_reply(r, session)

    # Run Claude with progress indicator
    session.status = "running"
    await cm.edit_text(f"{pfx} | 🤖 → <code>{proj}</code>{fi}\n⏳ Working...", parse_mode=ParseMode.HTML)

    start_time = datetime.now()

    async def progress_updater():
        dots = 0
        while True:
            await asyncio.sleep(30)
            dots = (dots + 1) % 4
            elapsed = (datetime.now() - start_time).seconds
            mins = elapsed // 60
            secs = elapsed % 60
            try:
                await cm.edit_text(
                    f"{pfx} | 🤖 → <code>{proj}</code>{fi}\n"
                    f"⏳ Working{'.' * (dots + 1)} ({mins}m {secs}s)",
                    parse_mode=ParseMode.HTML)
            except: pass

    progress_task = asyncio.create_task(progress_updater())
    try:
        output = await run_claude(prompt, proj, session.session_uuid, files, session.label)
    finally:
        progress_task.cancel()

    try: await cm.delete()
    except: pass

    session.out = output
    session.status = "idle"
    session.tasks += 1
    session.last_active = time.time()

    sent = await send_long(update.message,
        f"{pfx} | 🤖 <code>{proj}</code>\n━━━━━━━━━━━━━━━━━━━━━\n\n{fmt_out(output)}",
        rm=after_kb())
    await track_reply(sent, session)

    if len(output) > 8000:
        lnk = await make_report(f"{proj}-auto", output)
        r = await update.message.reply_text(f"📎 <b>Full output:</b>\n\n{fmt_links(lnk)}", parse_mode=ParseMode.HTML)
        await track_reply(r, session)

    if USAGE.should_alert():
        s = USAGE.get_summary()
        await update.message.reply_text(
            f"⚠️ Hourly usage at {int(s['hourly_pct']*100)}% — "
            f"consider pausing heavy tasks or switching to lighter prompts",
            parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════
#  Quick Actions (session-independent)
# ═══════════════════════════════════════════
async def do_health(u, c):
    await c.bot.send_chat_action(u.effective_chat.id, ChatAction.TYPING)
    health_script = f"{SCRIPTS}/health-check.sh"
    if not os.path.isfile(health_script):
        await u.message.reply_text("⚠️ health-check.sh not found. See scripts/ for an example.", parse_mode=ParseMode.HTML)
        return
    r = subprocess.run([health_script], capture_output=True, text=True, timeout=30)
    await u.message.reply_text(f"📊 <b>Health</b>\n\n<pre>{esc(r.stdout[:3500])}</pre>", parse_mode=ParseMode.HTML)

async def do_logs(u, c):
    await c.bot.send_chat_action(u.effective_chat.id, ChatAction.TYPING)
    logs_script = f"{SCRIPTS}/collect-logs.sh"
    if not os.path.isfile(logs_script):
        await u.message.reply_text("⚠️ collect-logs.sh not found. See scripts/ for an example.", parse_mode=ParseMode.HTML)
        return
    r = subprocess.run([logs_script], capture_output=True, text=True, timeout=30)
    await u.message.reply_text(f"📋 <b>Logs</b>\n\n<pre>{esc(r.stdout[:3500])}</pre>", parse_mode=ParseMode.HTML)

async def do_projects(u, c):
    cid = u.effective_chat.id
    session = resolve_session(u)
    key = session.id if session else ""
    active = SM.active_for_chat(cid)
    ps = get_project_names()

    lines = ["📁 <b>Available projects:</b>\n"]
    cur_proj = session.project if session else DEFAULT_PROJECT
    for p in ps:
        check = " ✅" if p == cur_proj else ""
        proj_sessions = [s for s in active if s.project == p]
        sess_info = ", ".join(f"{s.color_emoji} {s.label} ({s.status})" for s in proj_sessions) if proj_sessions else "no sessions"
        lines.append(f"  <b>{p}</b>{check} — {sess_info}")

    await u.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML, reply_markup=project_kb(key))

async def do_status(u, c):
    cid = u.effective_chat.id
    active = SM.active_for_chat(cid)
    if not active:
        await u.message.reply_text("ℹ️ No active sessions. Use /new &lt;name&gt; to start.", parse_mode=ParseMode.HTML)
        return
    lines = []
    for s in active:
        age = elapsed_str(s.started_at)
        last = elapsed_str(s.last_active)
        lines.append(
            f"{s.color_emoji} <b>{esc(s.label)}</b> — {s.status} ({age})\n"
            f"   📂 {s.project} | 📝 {s.tasks} tasks | last active {last} ago")
    await u.message.reply_text(
        f"ℹ️ <b>Active Sessions</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines) +
        f"\n\n🎤 Gemini: {'✅' if GEMINI_OK else '❌'}",
        parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════
#  Handlers: Commands
# ═══════════════════════════════════════════
@authorized
async def cmd_start(u, c):
    cid = u.effective_chat.id
    active = SM.active_for_chat(cid)
    await u.message.reply_text(
        f"🤖 <b>Claude Code Telegram Bot v4</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📂 Default project: <b>{DEFAULT_PROJECT}</b>\n"
        f"🎤 Voice: {'✅ Gemini' if GEMINI_OK else '❌ Set GEMINI_API_KEY'}\n"
        f"💬 Sessions: {len(active)} active\n\n"
        f"<b>Commands:</b>\n"
        f"/new &lt;name&gt; — create a new session\n"
        f"/sessions — list active sessions\n"
        f"/kill &lt;name&gt; — end a session\n\n"
        f"💬 Reply to any session message to interact.",
        parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)

@authorized
async def cmd_help(u, c):
    await u.message.reply_text(
        "📖 <b>How to use — v4 Multi-Session</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Sessions:</b>\n"
        "/new &lt;name&gt; — start a new session (max 3)\n"
        "/sessions — list all active sessions\n"
        "/kill &lt;name&gt; — end a session\n"
        "/project &lt;name&gt; — change project for current session\n"
        "/usage — view token usage stats\n"
        "/model — change fallback model\n\n"
        "<b>Routing:</b>\n"
        "Reply to any session message → routes to that session\n"
        "If only 1 session active → auto-routes there\n\n"
        "💬 <b>Text:</b> Type anything → Claude Code\n"
        "🎤 <b>Voice:</b> Speak in Farsi/English\n"
        "    Short commands auto-execute (deploy, test...)\n"
        "    Long commands → refined → confirm → execute\n"
        "📎 <b>Files:</b> Send files → then reply with instructions\n"
        "⏸ <b>Pause:</b> 5s window to cancel after sending\n"
        "📦 <b>Reports:</b> Tap Save Report → link for Claude Chat\n\n"
        "<b>Smart features:</b>\n"
        "• Sessions auto-close after configurable inactivity\n"
        "• Multi-provider fallback when Claude is rate-limited\n"
        "• Usage alerts at 80% hourly limit\n",
        parse_mode=ParseMode.HTML)

@authorized
async def cmd_usage(u, c):
    cid = u.effective_chat.id
    active = SM.active_for_chat(cid)
    await u.message.reply_text(
        USAGE.format_usage_message(len(active)),
        parse_mode=ParseMode.HTML)

@authorized
async def cmd_new(u, c):
    cid = u.effective_chat.id
    parts = u.message.text.strip().split(maxsplit=1)
    has_name = len(parts) > 1
    label = parts[1].strip() if has_name else f"session-{int(time.time()) % 10000}"

    if not SM.can_create(cid):
        active = SM.active_for_chat(cid)
        await u.message.reply_text(
            f"⚠️ Max {MAX_SESSIONS} concurrent sessions. Kill one first:",
            parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
        return

    await u.message.reply_text(
        f"🆕 New session{f' <b>{esc(label)}</b>' if has_name else ''} — which project?",
        parse_mode=ParseMode.HTML, reply_markup=new_session_project_kb(label, has_name=has_name))

@authorized
async def cmd_sessions(u, c):
    cid = u.effective_chat.id
    active = SM.active_for_chat(cid)
    if not active:
        await u.message.reply_text("No active sessions. Use /new &lt;name&gt; to start.", parse_mode=ParseMode.HTML)
        return
    lines = []
    for s in active:
        status_icon = {"idle": "💤", "running": "⏳", "error": "❌"}.get(s.status, "❓")
        age = elapsed_str(s.started_at)
        last = elapsed_str(s.last_active)
        lines.append(f"{s.color_emoji} <b>{esc(s.label)}</b> — {status_icon} {s.status} ({age})\n"
                      f"   📂 {s.project} | 📝 {s.tasks} tasks | last {last} ago")
    await u.message.reply_text(
        f"📋 <b>Active Sessions:</b>\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.HTML, reply_markup=sessions_kill_kb(active))

@authorized
async def cmd_kill(u, c):
    cid = u.effective_chat.id
    parts = u.message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        active = SM.active_for_chat(cid)
        if not active:
            await u.message.reply_text("No active sessions.", parse_mode=ParseMode.HTML)
        elif len(active) == 1:
            s = active[0]
            SM.kill(cid, s.label)
            await u.message.reply_text(f"🗑 Session <b>{esc(s.label)}</b> ended.", parse_mode=ParseMode.HTML)
        else:
            await u.message.reply_text(
                "Which session to kill?",
                parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
        return
    label = parts[1].strip()
    if SM.kill(cid, label):
        await u.message.reply_text(f"🗑 Session <b>{esc(label)}</b> ended.", parse_mode=ParseMode.HTML)
    else:
        await u.message.reply_text(f"❌ No session named <b>{esc(label)}</b>", parse_mode=ParseMode.HTML)

@authorized
async def cmd_project(u, c):
    session = resolve_session(u)
    parts = u.message.text.strip().split(maxsplit=1)
    if not session and len(parts) < 2:
        await do_projects(u, c)
        return
    if not session:
        await do_projects(u, c)
        return
    if len(parts) < 2:
        key = session.id if session else ""
        await u.message.reply_text(
            f"{session_prefix(session)} | 📂 Current: <b>{session.project}</b>",
            parse_mode=ParseMode.HTML, reply_markup=project_kb(key))
        return
    proj = parts[1].strip()
    if not os.path.isdir(get_project_path(proj)):
        await u.message.reply_text(f"❌ Project <b>{esc(proj)}</b> not found.", parse_mode=ParseMode.HTML)
        return
    session.project = proj
    sent = await u.message.reply_text(
        f"{session_prefix(session)} | 📂 Project → <b>{esc(proj)}</b>",
        parse_mode=ParseMode.HTML)
    await track_reply(sent, session)

# ═══════════════════════════════════════════
#  /model — Fallback Model Selection
# ═══════════════════════════════════════════
OPENROUTER_POPULAR = [
    ("google/gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("google/gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("openai/gpt-4o", "GPT-4o"),
    ("openai/gpt-4.1", "GPT-4.1"),
    ("anthropic/claude-sonnet-4", "Claude Sonnet 4"),
    ("qwen/qwen3-235b", "Qwen3 235B"),
    ("deepseek/deepseek-r1", "DeepSeek R1"),
    ("meta-llama/llama-4-maverick", "Llama 4 Maverick"),
]

@authorized
async def cmd_model(u, c):
    prov = ACTIVE_FALLBACK["provider"]
    model = ACTIVE_FALLBACK.get("model", "")

    prompts_cfg = load_prompts()
    gemini_transcribe = prompts_cfg.get("transcribe", {}).get("model", "gemini-2.5-flash")
    gemini_refine = prompts_cfg.get("refine", {}).get("model", "gemini-2.5-flash")

    current = f"{prov}" + (f" ({model or gemini_refine})" if prov == "gemini" else f" ({model})" if model else "")

    rows = [
        [InlineKeyboardButton(
            ("✅ " if prov == "gemini" else "🔹 ") + f"Gemini — {gemini_refine} (primary)",
            callback_data=f"mdl:gemini:{gemini_refine}")],
        [InlineKeyboardButton(
            ("✅ " if prov == "openai" else "🔹 ") + "GPT-4o (OpenAI)",
            callback_data="mdl:openai:gpt-4o")],
    ]
    for model_id, label in OPENROUTER_POPULAR:
        is_active = prov == "openrouter" and model == model_id
        icon = "✅" if is_active else "🔸"
        rows.append([InlineKeyboardButton(f"{icon} {label}", callback_data=f"mdl:openrouter:{model_id}")])

    rows.append([InlineKeyboardButton("🔍 Search OpenRouter...", callback_data="mdl:search")])

    await u.message.reply_text(
        f"🤖 <b>Fallback Model</b>\n"
        f"Active: <code>{esc(current)}</code>\n\n"
        f"🎤 STT: <code>{gemini_transcribe}</code>\n"
        f"🧠 Refine: <code>{gemini_refine}</code>\n"
        f"<i>(STT/Refine always use Gemini from config)</i>\n\n"
        f"Choose fallback for when Claude Code is rate-limited:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows))

# ═══════════════════════════════════════════
#  Error Handler
# ═══════════════════════════════════════════
async def error_handler(update, context):
    err = context.error
    if "Query is too old" in str(err) or "query id is invalid" in str(err):
        log.debug(f"Expired callback (ignored): {err}")
        return
    log.error(f"Bot error: {err}", exc_info=err)
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id,
                f"⚠️ <code>{esc(str(err)[:200])}</code>",
                parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ═══════════════════════════════════════════
#  Handler: Inline Buttons
# ═══════════════════════════════════════════
@authorized
async def on_callback(u, c):
    q = u.callback_query; await q.answer()
    d = q.data; cid = q.message.chat.id

    session = SM.find_by_message(q.message.message_id)

    if d.startswith("proj:"):
        parts = d.split(":", 2)
        session_key = parts[1] if len(parts) > 2 else ""
        proj_name = parts[2] if len(parts) > 2 else parts[1]
        if session_key:
            session = SM.get_by_key(session_key)
        if session:
            session.project = proj_name
            await q.edit_message_text(
                f"{session_prefix(session)} | 📂 → <b>{proj_name}</b>",
                parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text(f"📂 <b>{proj_name}</b> selected.\n💬 Create a session with /new to start.", parse_mode=ParseMode.HTML)

    elif d == "do:report":
        if not session or not session.out:
            await c.bot.send_message(cid, "📭 No output."); return
        lnk = await make_report(f"{session.project}-task", session.out)
        sent = await c.bot.send_message(cid,
            f"{session_prefix(session)} | 📦 <b>Report</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n{fmt_links(lnk)}\n\n💡 <i>Copy Summary → paste in Claude Chat</i>",
            parse_mode=ParseMode.HTML)
        if session: await track_reply(sent, session)

    elif d == "do:deploy_ask":
        proj = session.project if session else DEFAULT_PROJECT
        await c.bot.send_message(cid, f"🚀 <b>Deploy {proj}</b>\nSelect branch:", parse_mode=ParseMode.HTML, reply_markup=deploy_branch_kb(proj))

    elif d.startswith("dbr:"):
        _, proj, br = d.split(":")
        if br == "claude_latest":
            r = subprocess.run(["git","branch","-r","--sort=-committerdate"], cwd=get_project_path(proj), capture_output=True, text=True)
            cbs = [b.strip().replace("origin/","") for b in r.stdout.splitlines() if "claude/" in b]
            br = cbs[0] if cbs else "main"
        await q.edit_message_text(f"⚠️ <b>Deploy {proj}@{br}</b> → production?\n\nSure?", parse_mode=ParseMode.HTML, reply_markup=deploy_confirm_kb(proj, br))

    elif d.startswith("dpl:"):
        _, proj, br = d.split(":")
        deploy_script = f"{SCRIPTS}/deploy-to-prod.sh"
        await q.edit_message_text(f"🚀 Deploying <b>{proj}@{br}</b>...", parse_mode=ParseMode.HTML)
        if os.path.isfile(deploy_script):
            r = subprocess.run([deploy_script, proj, br], capture_output=True, text=True, timeout=120)
            ok = r.returncode == 0
            await c.bot.send_message(cid, f"{'✅' if ok else '❌'} <b>{'Done' if ok else 'Failed'}</b>\n\n<pre>{esc(r.stdout[:3000])}</pre>", parse_mode=ParseMode.HTML)
        else:
            await c.bot.send_message(cid, "⚠️ deploy-to-prod.sh not found. Create it in scripts/.", parse_mode=ParseMode.HTML)

    elif d == "do:health":
        await c.bot.send_chat_action(cid, ChatAction.TYPING)
        health_script = f"{SCRIPTS}/health-check.sh"
        if os.path.isfile(health_script):
            r = subprocess.run([health_script], capture_output=True, text=True, timeout=30)
            await c.bot.send_message(cid, f"📊 <b>Health</b>\n\n<pre>{esc(r.stdout[:3500])}</pre>", parse_mode=ParseMode.HTML)
        else:
            await c.bot.send_message(cid, "⚠️ health-check.sh not found.", parse_mode=ParseMode.HTML)

    elif d == "do:pause":
        if session:
            session.paused = True
        await q.edit_message_text("⏸ <b>Paused!</b>\n\n✏️ Send corrected message (reply to a session).\n<i>Previous discarded.</i>", parse_mode=ParseMode.HTML)

    elif d == "do:voice_go":
        if not session:
            await c.bot.send_message(cid, "❓ Session not found."); return
        vt = session.voice_text; session.voice_text = ""
        if not vt: return
        if len(vt.split()) > 3:
            await c.bot.send_message(cid, f"{session_prefix(session)} | 🧠 <b>Refining...</b>", parse_mode=ParseMode.HTML)
            vt = await refine_prompt(vt)
            sent = await c.bot.send_message(cid, f"{session_prefix(session)} | 📝 <b>Refined:</b>\n<pre>{esc(vt[:2000])}</pre>", parse_mode=ParseMode.HTML)
            await track_reply(sent, session)
        await c.bot.send_chat_action(cid, ChatAction.TYPING)
        files = session.files; session.files = []
        session.status = "running"
        output = await run_claude(vt, session.project, session.session_uuid, files or None, session.label)
        session.out = output; session.status = "idle"
        session.tasks += 1; session.last_active = time.time()
        sent = await c.bot.send_message(cid,
            f"{session_prefix(session)} | 🤖 <code>{session.project}</code>\n━━━━━━━━━━━━━━━━━━━━━\n\n{fmt_out(output)}",
            parse_mode=ParseMode.HTML, reply_markup=after_kb())
        await track_reply(sent, session)
        if len(output) > 8000:
            lnk = await make_report(f"{session.project}-auto", output)
            sent = await c.bot.send_message(cid, f"📎 <b>Full output:</b>\n\n{fmt_links(lnk)}", parse_mode=ParseMode.HTML)
            await track_reply(sent, session)

    elif d == "do:voice_edit":
        if session:
            session.voice_text = ""
        await q.edit_message_text("✏️ <b>Edit mode.</b> Type corrected message (reply to session):", parse_mode=ParseMode.HTML)

    elif d.startswith("newproj:"):
        parts = d.split(":", 2)
        label = parts[1] if len(parts) > 2 else f"session-{int(time.time()) % 10000}"
        proj_name = parts[2] if len(parts) > 2 else parts[1]
        if not SM.can_create(cid):
            active = SM.active_for_chat(cid)
            await q.edit_message_text(
                f"⚠️ Max {MAX_SESSIONS} sessions. Kill one first.",
                parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
            return
        session = SM.create(cid, label)
        session.project = proj_name
        await q.edit_message_text(
            f"{session.color_emoji} Session <b>{esc(session.label)}</b> created.\n"
            f"📁 Project: <b>{proj_name}</b>\n\n"
            f"Reply to this message to interact with this session.",
            parse_mode=ParseMode.HTML)
        session.anchor_message_id = q.message.message_id
        SM.register_message(q.message.message_id, session.id)
        session.message_ids.append(q.message.message_id)
        log.info(f"Session created: {session.label} → {proj_name} for chat {cid}")

    elif d.startswith("newneed:"):
        parts = d.split(":", 2)
        auto_label = parts[1] if len(parts) > 2 else f"session-{int(time.time()) % 10000}"
        proj_name = parts[2] if len(parts) > 2 else parts[1]
        if not SM.can_create(cid):
            active = SM.active_for_chat(cid)
            await q.edit_message_text(
                f"⚠️ Max {MAX_SESSIONS} sessions. Kill one first.",
                parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
            return
        CONV_STATE[cid] = {"state": "awaiting_session_name", "project": proj_name, "auto_label": auto_label}
        await q.edit_message_text(
            f"📁 Project: <b>{esc(proj_name)}</b>\n\nEnter a name for this session (or tap Skip):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip", callback_data=f"skipname:{auto_label}:{proj_name}")]
            ]))

    elif d.startswith("skipname:"):
        parts = d.split(":", 2)
        auto_label = parts[1] if len(parts) > 2 else f"session-{int(time.time()) % 10000}"
        proj_name = parts[2] if len(parts) > 2 else parts[1]
        CONV_STATE.pop(cid, None)
        if not SM.can_create(cid):
            active = SM.active_for_chat(cid)
            await q.edit_message_text(
                f"⚠️ Max {MAX_SESSIONS} sessions. Kill one first.",
                parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
            return
        session = SM.create(cid, auto_label)
        session.project = proj_name
        await q.edit_message_text(
            f"{session.color_emoji} Session <b>{esc(session.label)}</b> created.\n"
            f"📁 Project: <b>{proj_name}</b>\n\n"
            f"Reply to this message to interact with this session.",
            parse_mode=ParseMode.HTML)
        session.anchor_message_id = q.message.message_id
        SM.register_message(q.message.message_id, session.id)
        session.message_ids.append(q.message.message_id)
        log.info(f"Session created: {session.label} → {proj_name} for chat {cid}")

    elif d.startswith("addproj:"):
        parts = d.split(":", 2)
        label = parts[1] if len(parts) > 2 else f"session-{int(time.time()) % 10000}"
        has_name_flag = parts[2] if len(parts) > 2 else "1"
        CONV_STATE[cid] = {"state": "awaiting_project_path", "label": label, "has_name": has_name_flag == "1"}
        await q.edit_message_text(
            "📂 Enter the full path to your project:\n"
            "Example: <code>/home/user/repos/MyProject</code>",
            parse_mode=ParseMode.HTML)

    elif d.startswith("skill:"):
        session_key = d.split(":", 1)[1]
        session = SM.get_by_key(session_key)
        if session:
            label = session.label
            chat_id_str = session_key.split(":")[0]
            try:
                SM.kill(int(chat_id_str), label)
            except ValueError:
                pass
            await q.edit_message_text(f"🗑 Session <b>{esc(label)}</b> ended.", parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text("❌ Session not found or already ended.", parse_mode=ParseMode.HTML)

    elif d.startswith("route:"):
        session_key = d.split(":", 1)[1]
        session = SM.get_by_key(session_key)
        pending = PENDING_MESSAGES.pop(cid, None)
        if session and pending:
            await q.edit_message_text(f"→ Sent to {session.color_emoji} {esc(session.label)}", parse_mode=ParseMode.HTML)
            original_msg = pending["message"]
            original_update = pending.get("update")
            SM.register_message(original_msg.message_id, session.id)
            f = session.files; session.files = []
            if original_update:
                await execute(original_update, c, pending["text"], session, f or None)
            else:
                session.status = "running"
                output = await run_claude(pending["text"], session.project, session.session_uuid, f or None, session.label)
                session.out = output; session.status = "idle"
                session.tasks += 1; session.last_active = time.time()
                sent = await c.bot.send_message(cid,
                    f"{session_prefix(session)} | 🤖 <code>{session.project}</code>\n━━━━━━━━━━━━━━━━━━━━━\n\n{fmt_out(output)}",
                    parse_mode=ParseMode.HTML, reply_markup=after_kb())
                await track_reply(sent, session)
        elif session:
            await q.edit_message_text(f"→ {session.color_emoji} {esc(session.label)} selected, but no pending message.", parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text("❌ Session not found.", parse_mode=ParseMode.HTML)

    elif d == "do:cancel":
        await q.edit_message_text("❌ Cancelled.")

    elif d == "menu:usage":
        active = SM.active_for_chat(cid)
        await c.bot.send_message(cid, USAGE.format_usage_message(len(active)), parse_mode=ParseMode.HTML)

    elif d == "menu:sessions":
        active = SM.active_for_chat(cid)
        if not active:
            await c.bot.send_message(cid, "No active sessions. Use /new &lt;name&gt; to start.", parse_mode=ParseMode.HTML)
        else:
            lines = []
            for s in active:
                status_icon = {"idle": "💤", "running": "⏳", "error": "❌"}.get(s.status, "❓")
                age = elapsed_str(s.started_at)
                last = elapsed_str(s.last_active)
                lines.append(f"{s.color_emoji} <b>{esc(s.label)}</b> — {status_icon} {s.status} ({age})\n"
                              f"   📂 {s.project} | 📝 {s.tasks} tasks | last {last} ago")
            await c.bot.send_message(cid,
                f"<b>Active sessions:</b>\n\n" + "\n\n".join(lines) +
                "\n\n/kill &lt;name&gt; — end a session",
                parse_mode=ParseMode.HTML)

    elif d == "menu:new_session":
        if not SM.can_create(cid):
            active = SM.active_for_chat(cid)
            await c.bot.send_message(cid,
                f"⚠️ Max {MAX_SESSIONS} concurrent sessions. Kill one first:",
                parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
        else:
            label = f"session-{int(time.time()) % 10000}"
            await c.bot.send_message(cid,
                f"🆕 New session — which project?",
                parse_mode=ParseMode.HTML, reply_markup=new_session_project_kb(label, has_name=False))

    elif d == "menu:health":
        await c.bot.send_chat_action(cid, ChatAction.TYPING)
        health_script = f"{SCRIPTS}/health-check.sh"
        if os.path.isfile(health_script):
            r = subprocess.run([health_script], capture_output=True, text=True, timeout=30)
            await c.bot.send_message(cid, f"📊 <b>Health</b>\n\n<pre>{esc(r.stdout[:3500])}</pre>", parse_mode=ParseMode.HTML)
        else:
            await c.bot.send_message(cid, "⚠️ health-check.sh not found.", parse_mode=ParseMode.HTML)

    elif d.startswith("mdl:"):
        parts = d.split(":", 2)
        provider = parts[1] if len(parts) > 1 else "gemini"
        model = parts[2] if len(parts) > 2 else ""

        if provider == "search":
            CONV_STATE[cid] = {"state": "awaiting_model_search"}
            await q.edit_message_text(
                "🔍 Type a model name or keyword to search OpenRouter:\n"
                "Examples: <code>qwen</code>, <code>llama</code>, <code>deepseek</code>, <code>gemma</code>",
                parse_mode=ParseMode.HTML)
            return

        ACTIVE_FALLBACK["provider"] = provider
        ACTIVE_FALLBACK["model"] = model
        short = model.split("/")[-1] if "/" in model else model
        await q.edit_message_text(
            f"✅ Fallback model set to: <b>{provider}</b> (<code>{esc(short)}</code>)",
            parse_mode=ParseMode.HTML)
        log.info(f"Fallback model changed: {provider}/{model}")

    elif d.startswith("mdlpick:"):
        model_id = d.split(":", 1)[1]
        ACTIVE_FALLBACK["provider"] = "openrouter"
        ACTIVE_FALLBACK["model"] = model_id
        short = model_id.split("/")[-1] if "/" in model_id else model_id
        await q.edit_message_text(
            f"✅ Fallback → OpenRouter: <b>{esc(short)}</b>",
            parse_mode=ParseMode.HTML)
        log.info(f"Fallback model changed: openrouter/{model_id}")

# ═══════════════════════════════════════════
#  Handler: Documents
# ═══════════════════════════════════════════
@authorized
async def on_document(u, c):
    session = resolve_session(u)
    lp = await download_doc(u.message.document, c)
    cap = u.message.caption or ""

    if cap.strip():
        if not session:
            session = SM.create(u.effective_chat.id, f"task-{int(time.time()) % 10000}")
            anchor = await u.message.reply_text(
                f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b> created.",
                parse_mode=ParseMode.HTML)
            await track_reply(anchor, session)

        all_f = session.files + [lp]; session.files = []
        await execute(u, c, cap.strip(), session, all_f)
    else:
        if not session:
            await u.message.reply_text(
                "📎 File received. Reply to a session message with instructions, or create one with /new.",
                parse_mode=ParseMode.HTML)
            return
        session.files.append(lp)
        names = [os.path.basename(f) for f in session.files]
        sent = await u.message.reply_text(
            f"{session_prefix(session)} | 📎 <b>Queued ({len(session.files)}):</b>\n" +
            "\n".join(f"  • <code>{esc(n)}</code>" for n in names) +
            "\n\n💬 <i>Reply to send instructions.</i>",
            parse_mode=ParseMode.HTML)
        await track_reply(sent, session)

@authorized
async def on_photo(u, c):
    session = resolve_session(u)
    photo = u.message.photo[-1]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    lp = f"{UPLOADS}/{ts}_photo.jpg"
    tf = await c.bot.get_file(photo.file_id); await tf.download_to_drive(lp)
    cap = u.message.caption or ""

    if cap.strip():
        if not session:
            session = SM.create(u.effective_chat.id, f"task-{int(time.time()) % 10000}")
            anchor = await u.message.reply_text(
                f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b> created.",
                parse_mode=ParseMode.HTML)
            await track_reply(anchor, session)

        all_f = session.files + [lp]; session.files = []
        await execute(u, c, cap.strip(), session, all_f)
    else:
        if not session:
            await u.message.reply_text(
                "📷 Photo received. Reply to a session message with instructions, or create one with /new.",
                parse_mode=ParseMode.HTML)
            return
        session.files.append(lp)
        sent = await u.message.reply_text(
            f"{session_prefix(session)} | 📷 <b>Queued.</b> Files: {len(session.files)}\n💬 <i>Reply to send instructions.</i>",
            parse_mode=ParseMode.HTML)
        await track_reply(sent, session)

# ═══════════════════════════════════════════
#  Handler: Voice
# ═══════════════════════════════════════════
@authorized
async def on_voice(u, c):
    session = resolve_session(u)
    if not GEMINI_OK:
        await u.message.reply_text("🎤 Voice needs GEMINI_API_KEY in .env"); return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    lp = f"{UPLOADS}/{ts}_voice.ogg"
    tf = await c.bot.get_file(u.message.voice.file_id); await tf.download_to_drive(lp)

    await c.bot.send_chat_action(u.effective_chat.id, ChatAction.TYPING)
    st = await u.message.reply_text("🎤 <b>Transcribing...</b>", parse_mode=ParseMode.HTML)
    if session:
        await track_reply(st, session)

    txt = await transcribe(lp)
    if not txt:
        await st.edit_text("❌ Transcription failed."); return

    # Quick command?
    cmd = match_cmd(txt)
    if cmd:
        await st.edit_text(f"🎤 <code>{esc(txt)}</code>\n⚡ <b>{cmd}</b>", parse_mode=ParseMode.HTML)
        if cmd == "deploy":
            proj = session.project if session else DEFAULT_PROJECT
            await c.bot.send_message(u.effective_chat.id, f"🚀 <b>Deploy {proj}</b>", parse_mode=ParseMode.HTML, reply_markup=deploy_branch_kb(proj))
        elif cmd == "health": await do_health(u, c)
        elif cmd == "logs": await do_logs(u, c)
        elif cmd == "test":
            if not session:
                session = SM.create(u.effective_chat.id, f"test-{int(time.time()) % 10000}")
                anchor = await u.message.reply_text(f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b>", parse_mode=ParseMode.HTML)
                await track_reply(anchor, session)
            f = session.files; session.files = []
            await execute(u, c, "Run all tests and report results", session, f or None, True)
        elif cmd == "confirm":
            await u.message.reply_text("✅ <b>Confirmed.</b>", parse_mode=ParseMode.HTML)
        elif cmd == "new":
            session = SM.create(u.effective_chat.id, f"session-{int(time.time()) % 10000}")
            anchor = await u.message.reply_text(
                f"{session.color_emoji} Session <b>{esc(session.label)}</b> created.\nReply to interact.",
                parse_mode=ParseMode.HTML)
            await track_reply(anchor, session)
        elif cmd == "standup":
            if not session:
                session = SM.create(u.effective_chat.id, f"standup-{int(time.time()) % 10000}")
                anchor = await u.message.reply_text(f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b>", parse_mode=ParseMode.HTML)
                await track_reply(anchor, session)
            f = session.files; session.files = []
            await execute(u, c, "standup", session, f or None, True)
        return

    # Long voice → show transcription + confirm
    if not session:
        session = SM.create(u.effective_chat.id, f"voice-{int(time.time()) % 10000}")
        anchor = await u.message.reply_text(
            f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b> created.",
            parse_mode=ParseMode.HTML)
        await track_reply(anchor, session)

    session.voice_text = txt
    sent = await st.edit_text(
        f"{session_prefix(session)} | 🎤 <b>Transcription:</b>\n\n<code>{esc(txt)}</code>\n\n"
        f"🧠 <i>Will be refined into a structured prompt before sending.</i>",
        parse_mode=ParseMode.HTML, reply_markup=voice_confirm_kb())
    SM.register_message(st.message_id, session.id)

# ═══════════════════════════════════════════
#  Handler: Text
# ═══════════════════════════════════════════
@authorized
async def on_text(u, c):
    t = u.message.text.strip()
    if not t: return
    cid = u.effective_chat.id

    # Handle conversation states (multi-step flows)
    conv = CONV_STATE.get(cid)
    if conv:
        state = conv.get("state")

        if state == "awaiting_session_name":
            CONV_STATE.pop(cid, None)
            proj_name = conv["project"]
            label = t.strip().replace(" ", "-")[:30]
            if not SM.can_create(cid):
                active = SM.active_for_chat(cid)
                await u.message.reply_text(
                    f"⚠️ Max {MAX_SESSIONS} sessions. Kill one first:",
                    parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
                return
            session = SM.create(cid, label)
            session.project = proj_name
            sent = await u.message.reply_text(
                f"{session.color_emoji} Session <b>{esc(session.label)}</b> created.\n"
                f"📁 Project: <b>{proj_name}</b>\n\n"
                f"Reply to this message to interact with this session.",
                parse_mode=ParseMode.HTML)
            session.anchor_message_id = sent.message_id
            SM.register_message(sent.message_id, session.id)
            session.message_ids.append(sent.message_id)
            log.info(f"Session created: {session.label} → {proj_name} for chat {cid}")
            return

        if state == "awaiting_project_path":
            CONV_STATE.pop(cid, None)
            path = t.strip()
            label = conv["label"]
            has_name = conv["has_name"]
            proj_name = os.path.basename(path.rstrip("/"))
            if not proj_name:
                await u.message.reply_text("❌ Invalid path.", parse_mode=ParseMode.HTML)
                return
            add_project(proj_name, path)
            log.info(f"New project added: {proj_name} → {path}")

            if has_name:
                if not SM.can_create(cid):
                    active = SM.active_for_chat(cid)
                    await u.message.reply_text(
                        f"⚠️ Max {MAX_SESSIONS} sessions.",
                        parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
                    return
                session = SM.create(cid, label)
                session.project = proj_name
                sent = await u.message.reply_text(
                    f"📁 Project <b>{esc(proj_name)}</b> added.\n"
                    f"{session.color_emoji} Session <b>{esc(session.label)}</b> created.\n\n"
                    f"Reply to this message to interact with this session.",
                    parse_mode=ParseMode.HTML)
                session.anchor_message_id = sent.message_id
                SM.register_message(sent.message_id, session.id)
                session.message_ids.append(sent.message_id)
            else:
                CONV_STATE[cid] = {"state": "awaiting_session_name", "project": proj_name, "auto_label": label}
                await u.message.reply_text(
                    f"📁 Project <b>{esc(proj_name)}</b> added.\n\nEnter a name for this session (or tap Skip):",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭ Skip", callback_data=f"skipname:{label}:{proj_name}")]
                    ]))
            return

        if state == "awaiting_model_search":
            CONV_STATE.pop(cid, None)
            query = t.strip().lower()
            if not OPENROUTER_API_KEY:
                await u.message.reply_text("⚠️ OPENROUTER_API_KEY not set.", parse_mode=ParseMode.HTML)
                return
            await u.message.reply_text("🔍 Searching OpenRouter...", parse_mode=ParseMode.HTML)
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get("https://openrouter.ai/api/v1/models")
                    resp.raise_for_status()
                    models = resp.json().get("data", [])
                matches = [m for m in models if query in m.get("id", "").lower() or query in m.get("name", "").lower()][:10]
                if not matches:
                    await u.message.reply_text(f"❌ No models found for <code>{esc(query)}</code>", parse_mode=ParseMode.HTML)
                    return
                rows = []
                for m in matches:
                    mid = m["id"]
                    name = m.get("name", mid)[:40]
                    rows.append([InlineKeyboardButton(f"🔸 {name}", callback_data=f"mdlpick:{mid}")])
                await u.message.reply_text(
                    f"🔍 Results for <code>{esc(query)}</code>:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(rows))
            except Exception as e:
                await u.message.reply_text(f"❌ Search failed: {e}", parse_mode=ParseMode.HTML)
            return

    # Handle persistent keyboard button taps
    KB_MAP = {
        "📊 Health": "health", "📋 Logs": "logs", "📁 Projects": "projects",
        "🆕 New Session": "new_session", "📈 Usage": "usage", "❓ Help": "help",
    }
    if t in KB_MAP:
        action = KB_MAP[t]
        if action == "health": await do_health(u, c)
        elif action == "logs": await do_logs(u, c)
        elif action == "projects": await do_projects(u, c)
        elif action == "usage": await cmd_usage(u, c)
        elif action == "help": await cmd_help(u, c)
        elif action == "new_session":
            u.message.text = "/new"
            await cmd_new(u, c)
        return

    # Resolve session
    session = resolve_session(u)

    if not session:
        active = SM.active_for_chat(cid)
        if len(active) > 1:
            PENDING_MESSAGES[cid] = {"text": t, "files": [], "message": u.message, "update": u}
            await u.message.reply_text(
                "❓ Which session?",
                parse_mode=ParseMode.HTML, reply_markup=route_picker_kb(active))
            return
        # No sessions → auto-create one
        session = SM.create(cid, f"session-{int(time.time()) % 10000}")
        anchor = await u.message.reply_text(
            f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b> created.\nReply to interact, or continue below.",
            parse_mode=ParseMode.HTML)
        await track_reply(anchor, session)

    SM.register_message(u.message.message_id, session.id)

    f = session.files; session.files = []
    await execute(u, c, t, session, f or None)

# ═══════════════════════════════════════════
#  Boot
# ═══════════════════════════════════════════
async def session_cleanup_task(app):
    """Background task: clean up timed-out sessions every 5 minutes."""
    try:
        while True:
            await asyncio.sleep(300)
            try:
                timed_out = SM.cleanup_timed_out()
                for s in timed_out:
                    log.info(f"Session auto-closed (timeout): {s.label}")
                    chat_id_str = s.id.split(":")[0]
                    try:
                        chat_id = int(chat_id_str)
                        await app.bot.send_message(
                            chat_id,
                            f"⏰ Session <b>{esc(s.label)}</b> auto-closed after {SESSION_TIMEOUT_MINUTES // 60}h inactivity.",
                            parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
            except Exception as e:
                log.error(f"Session cleanup error: {e}")
    except asyncio.CancelledError:
        log.info("Session cleanup task cancelled (shutdown)")
        return


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Home"),
        BotCommand("help", "Help"),
        BotCommand("new", "New session"),
        BotCommand("sessions", "List sessions"),
        BotCommand("kill", "End session"),
        BotCommand("project", "Change project"),
        BotCommand("model", "Fallback model"),
        BotCommand("usage", "Usage stats"),
    ])
    asyncio.create_task(session_cleanup_task(app))
    log.info(f"Bot v4 ready. Gemini={'OK' if GEMINI_OK else 'OFF'} | GPT fallback={'OK' if OPENAI_API_KEY else 'OFF'}")

def main():
    if not BOT_TOKEN: print("Set TELEGRAM_BOT_TOKEN in .env"); sys.exit(1)
    log.info("Starting Claude Code Telegram Bot v4 (Multi-Session)...")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
