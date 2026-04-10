#!/usr/bin/env python3
"""
Shahrzad DevOps Telegram Bot v4
Multi-Session | Voice (Gemini STT + LLM refinement) | Files | Rich UI
"""

import os, sys, json, asyncio, subprocess, logging, html, base64, uuid, time, re
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

# ── Config ──
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS = [int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]
REPOS      = "/opt/shahrzad-devops/repos"
REPORTS    = "/opt/shahrzad-devops/reports"
LOGS       = "/opt/shahrzad-devops/logs"
SCRIPTS    = "/opt/shahrzad-devops/scripts"
UPLOADS    = "/opt/shahrzad-devops/uploads"
PROMPTS_FILE = "/opt/shahrzad-devops/configs/gemini-prompts.json"
REPORT_URL = "https://devops.shahrzad.ai/reports"
DEFAULT_PROJECT = "ZigguratKids4"
PAUSE_SECONDS = 5
MAX_SESSIONS  = 3
SESSION_TIMEOUT_MINUTES = 72 * 60  # 72 hours for most sessions
PERMANENT_PROJECTS = {"ZigguratKids4"}  # never auto-close these
USAGE_DB_PATH = "/opt/shahrzad-devops/configs/usage_tracker.json"
PROJECTS_FILE = "/opt/shahrzad-devops/configs/projects.json"

# ── OpenAI (GPT fallback) ──
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GPT_FALLBACK_MODEL = "gpt-4o"

# Active fallback model (can be changed at runtime via /model)
# "gemini" = use Gemini from gemini-prompts.json, "openai" = GPT-4o, "openrouter:<model>" = custom
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
        self.hourly_limit = 100_000   # tokens — adjust based on plan
        self.daily_limit  = 1_000_000
        self.weekly_limit = 5_000_000
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
        bar = "\u2588" * filled + "\u2591" * empty
        if pct > 0.9:
            emoji = "\U0001f534"
        elif pct > 0.7:
            emoji = "\u26a0\ufe0f"
        else:
            emoji = "\u2705"
        return f"{bar} {int(pct * 100)}%  {emoji}"

    def format_usage_message(self, active_sessions: int = 0) -> str:
        s = self.get_summary()
        return (
            "\U0001f4ca <b>Claude Code Usage</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
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
    """Get list of project names from config."""
    return [p["name"] for p in load_projects()]


def add_project(name: str, path: str):
    """Add a new project to the config."""
    projects = load_projects()
    # Don't add duplicates
    if any(p["name"] == name for p in projects):
        return
    projects.append({"name": name, "path": path})
    save_projects(projects)


def get_project_path(name: str) -> str:
    """Get project path by name. Falls back to REPOS/<name>."""
    for p in load_projects():
        if p["name"] == name:
            return p["path"]
    return f"{REPOS}/{name}"


# ═══════════════════════════════════════════
#  Conversation State (multi-step flows)
# ═══════════════════════════════════════════
# chat_id -> {"state": str, "data": dict}
CONV_STATE: dict[int, dict] = {}


# ═══════════════════════════════════════════
#  Fallback Chain: Gemini → GPT → OpenRouter
# ═══════════════════════════════════════════
async def gemini_fallback(prompt: str, project: str) -> str:
    """Use Gemini as primary fallback for non-code tasks."""
    if not GEMINI_OK:
        return ""  # empty = try next fallback
    try:
        # Use refine model from gemini-prompts.json (most capable)
        cfg = load_prompts().get("refine", {})
        model = cfg.get("model", "gemini-2.5-flash")
        r = gemini_client.models.generate_content(
            model=model,
            contents=[{"role": "user", "parts": [{"text":
                f"You are a DevOps assistant for the {project} project at Shahrzad.ai. "
                f"Be concise and actionable. You cannot execute code or SSH — only advise.\n\n{prompt}"
            }]}])
        short = model.split("-preview")[0] if "-preview" in model else model
        return f"\U0001f504 <i>[Gemini/{short} fallback — Claude was rate-limited]</i>\n\n{r.text.strip()}"
    except Exception as e:
        log.error(f"Gemini fallback error: {e}")
        return ""  # empty = try next


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
            return f"\U0001f504 <i>[GPT fallback — Claude was rate-limited]</i>\n\n{content}"
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
                    "HTTP-Referer": "https://shahrzad.ai",
                    "X-Title": "Shahrzad DevOps",
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
            return f"\U0001f504 <i>[OpenRouter/{short_model} — Claude was rate-limited]</i>\n\n{content}"
    except Exception as e:
        log.error(f"OpenRouter fallback error: {e}")
        return ""


async def gpt_fallback(prompt: str, project: str) -> str:
    """Fallback chain: active model → gemini → openai → openrouter → error."""
    provider = ACTIVE_FALLBACK.get("provider", "gemini")
    model = ACTIVE_FALLBACK.get("model", "")

    # Try active fallback first
    if provider == "gemini":
        r = await gemini_fallback(prompt, project)
        if r: return r
    elif provider == "openai":
        r = await openai_fallback(prompt, project)
        if r: return r
    elif provider == "openrouter":
        r = await openrouter_fallback(prompt, project, model)
        if r: return r

    # Chain: try remaining providers
    if provider != "gemini":
        r = await gemini_fallback(prompt, project)
        if r: return r
    if provider != "openai":
        r = await openai_fallback(prompt, project)
        if r: return r
    if provider != "openrouter":
        r = await openrouter_fallback(prompt, project)
        if r: return r

    return "\u26a0\ufe0f Claude rate-limited. All fallbacks failed. Wait for rate limit to reset."


# ═══════════════════════════════════════════
#  Session Model
# ═══════════════════════════════════════════
SESSION_COLORS = ["🔵", "🟢", "🟡", "🟠", "🔴", "🟣"]

@dataclass
class Session:
    id: str                          # unique session id
    label: str                       # user-visible name
    color_emoji: str                 # rotating color indicator
    session_uuid: str                # UUID for claude --session-id
    project: str = DEFAULT_PROJECT
    status: str = "idle"             # idle | running | completed | error
    started_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_ids: list = field(default_factory=list)   # telegram msg IDs belonging to this session
    anchor_message_id: Optional[int] = None           # the /new reply message
    files: list = field(default_factory=list)
    out: str = ""
    tasks: int = 0
    paused: bool = False
    voice_text: str = ""


class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Session] = {}       # session_id -> Session (per chat)
        self.msg_to_session: dict[int, str] = {}     # telegram_message_id -> session_id
        self.color_index: dict[int, int] = {}         # chat_id -> next color index

    def _next_color(self, chat_id: int) -> str:
        idx = self.color_index.get(chat_id, 0)
        color = SESSION_COLORS[idx % len(SESSION_COLORS)]
        self.color_index[chat_id] = idx + 1
        return color

    def _key(self, chat_id: int, label: str) -> str:
        return f"{chat_id}:{label}"

    def create(self, chat_id: int, label: str) -> Session:
        key = self._key(chat_id, label)
        # If session with same label exists, kill it first
        if key in self.sessions:
            self.kill(chat_id, label)
        color = self._next_color(chat_id)
        session = Session(
            id=key,
            label=label,
            color_emoji=color,
            session_uuid=str(uuid.uuid4()),
        )
        self.sessions[key] = session
        return session

    def get(self, chat_id: int, label: str) -> Optional[Session]:
        return self.sessions.get(self._key(chat_id, label))

    def get_by_key(self, key: str) -> Optional[Session]:
        return self.sessions.get(key)

    def register_message(self, msg_id: int, session_key: str):
        """Map a telegram message ID to a session for reply routing."""
        self.msg_to_session[msg_id] = session_key

    def find_by_message(self, msg_id: int) -> Optional[Session]:
        """Find session by telegram message ID (for reply routing)."""
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
            # Clean up message mappings
            to_remove = [mid for mid, sk in self.msg_to_session.items() if sk == key]
            for mid in to_remove:
                del self.msg_to_session[mid]
            return True
        return False

    def get_default(self, chat_id: int) -> Optional[Session]:
        """Get default session: if only 1 active, return it."""
        active = self.active_for_chat(chat_id)
        if len(active) == 1:
            return active[0]
        return None

    def cleanup_timed_out(self, timeout_minutes: int = SESSION_TIMEOUT_MINUTES) -> list[Session]:
        """Remove sessions inactive for longer than timeout. Returns removed sessions.
        Sessions on PERMANENT_PROJECTS are never auto-closed."""
        cutoff = time.time() - timeout_minutes * 60
        timed_out = []
        keys_to_remove = []
        for key, session in self.sessions.items():
            # Never auto-close sessions on permanent projects
            if session.project in PERMANENT_PROJECTS:
                continue
            if session.status != "running" and session.last_active < cutoff:
                timed_out.append(session)
                keys_to_remove.append(key)
        for key in keys_to_remove:
            session = self.sessions.pop(key, None)
            # Clean up message mappings
            to_remove = [mid for mid, sk in self.msg_to_session.items() if sk == key]
            for mid in to_remove:
                del self.msg_to_session[mid]
        return timed_out

    def can_create(self, chat_id: int) -> bool:
        """Check if chat hasn't exceeded MAX_SESSIONS."""
        return len(self.active_for_chat(chat_id)) < MAX_SESSIONS


SM = SessionManager()
# Store pending messages for multi-session routing: chat_id -> (text, files, message)
PENDING_MESSAGES: dict[int, dict] = {}
# Track which session is "focused" per chat — messages without reply go here
ACTIVE_SESSION: dict[int, str] = {}  # chat_id -> session_key
# Buffer for multi-message concatenation (Telegram splits long text)
MESSAGE_BUFFER: dict[int, dict] = {}  # chat_id -> {"texts": [], "timer": task, "update": update, "time": float}

# ═══════════════════════════════════════════
#  Delayed Prompts
# ═══════════════════════════════════════════
DELAY_PATTERN = re.compile(r"^:DELAY=(\d+)(M|H):\s*", re.IGNORECASE)
DELAY_NEXT_PATTERN = re.compile(r"^:DELAY=NEXT:\s*", re.IGNORECASE)

@dataclass
class DelayedPrompt:
    id: str
    chat_id: int
    prompt: str
    project: str
    session_label: str
    session_key: str
    scheduled_at: float
    fire_at: float
    delay_str: str
    task: Optional[asyncio.Task] = None
    fired: bool = False
    cancelled: bool = False
    files: list = field(default_factory=list)
    fire_after_task: bool = False  # True = fire 5 min after last task completes

# Global store: delay_id -> DelayedPrompt
DELAYED_PROMPTS: dict[str, DelayedPrompt] = {}
# Pending delay picks: chat_id -> delay info waiting for session selection
PENDING_DELAYS: dict[int, dict] = {}


def parse_delay(text: str) -> tuple[Optional[int], str, bool]:
    """Parse :DELAY=30M: or :DELAY=2H: or :DELAY=NEXT: prefix.
    Returns (seconds, remaining_text, fire_after_task) or (None, text, False)."""
    # Check NEXT pattern first
    m_next = DELAY_NEXT_PATTERN.match(text)
    if m_next:
        remaining = text[m_next.end():]
        return 300, remaining, True  # 5 min after last task
    m = DELAY_PATTERN.match(text)
    if not m:
        return None, text, False
    amount = int(m.group(1))
    unit = m.group(2).upper()
    if unit == "M":
        seconds = amount * 60
    else:  # H
        seconds = amount * 3600
    remaining = text[m.end():]
    return seconds, remaining, False


def format_delay(seconds: int) -> str:
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{f' {m}m' if m else ''}"
    return f"{seconds // 60}m"


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
    """Inline quick-action buttons — shown on /start and /help only."""
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
    """Project picker for /new flow — callback stores the session label.
    has_name=False uses 'newneed:' prefix so we know to ask for name after project pick."""
    ps = get_project_names()
    prefix = "newproj" if has_name else "newneed"
    rows = [[InlineKeyboardButton(f"📁 {p}", callback_data=f"{prefix}:{label}:{p}")] for p in ps]
    rows.append([InlineKeyboardButton("➕ New project...", callback_data=f"addproj:{label}:{'1' if has_name else '0'}")])
    return InlineKeyboardMarkup(rows)

def sessions_kill_kb(sessions: list):
    """Inline kill buttons for /sessions display."""
    rows = [[InlineKeyboardButton(f"🗑 {s.color_emoji} {s.label}", callback_data=f"skill:{s.id}")] for s in sessions]
    return InlineKeyboardMarkup(rows)

def kill_picker_kb(sessions: list):
    """Inline buttons for /kill without args."""
    rows = [[InlineKeyboardButton(f"🗑 {s.color_emoji} {s.label}", callback_data=f"skill:{s.id}")] for s in sessions]
    return InlineKeyboardMarkup(rows)

def route_picker_kb(sessions: list):
    """Inline session picker when user sends message without reply and multiple sessions active."""
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
        # Fallback to REPOS/<project>
        repo = f"{REPOS}/{project}"
    if not os.path.isdir(repo):
        # Try to create the directory (user may have added a new project path)
        try:
            os.makedirs(repo, exist_ok=True)
            log.info(f"Created project directory: {repo}")
        except Exception as e:
            return f"\u274c Not found: {project}\nCould not create: {e}"
    fnote = ""
    if files:
        td = f"{repo}/.claude-tasks"; os.makedirs(td, exist_ok=True)
        copied = []
        for fp in files:
            if os.path.isfile(fp):
                d = f"{td}/{os.path.basename(fp)}"; subprocess.run(["cp", fp, d]); copied.append(d)
        if copied:
            fnote = "\n\n[ATTACHED FILES \u2014 read before starting]\n" + "\n".join(f"  - {f}" for f in copied) + "\n"
    full_prompt = prompt + fnote
    cmd = ["claude", "--print", "--session-id", session_uuid, full_prompt]
    log.info(f"Claude [{project}] session={session_uuid[:8]}: {prompt[:80]}... ({len(files or [])} files)")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": "/root", "PATH": "/root/.local/bin:/usr/local/bin:/usr/bin:/bin"})
        out, err = await asyncio.wait_for(proc.communicate(), timeout=3600)
        r = out.decode("utf-8", errors="replace")
        e = err.decode("utf-8", errors="replace") if err else ""

        # Handle "Session ID already in use" — generate new UUID and retry once
        if proc.returncode != 0 and "already in use" in (r + e).lower():
            log.warning(f"Session ID {session_uuid[:8]} in use, retrying with new UUID")
            new_uuid = str(uuid.uuid4())
            cmd[cmd.index(session_uuid)] = new_uuid
            proc2 = await asyncio.create_subprocess_exec(
                *cmd, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "HOME": "/root", "PATH": "/root/.local/bin:/usr/local/bin:/usr/bin:/bin"})
            out2, err2 = await asyncio.wait_for(proc2.communicate(), timeout=3600)
            r = out2.decode("utf-8", errors="replace")
            e = err2.decode("utf-8", errors="replace") if err2 else ""
            proc = proc2  # use new proc for remaining checks

        # Track usage
        USAGE.record(len(full_prompt), len(r), session_label)

        # Detect rate limiting
        rate_limited = False
        if proc.returncode != 0:
            combined = (r + e).lower()
            if any(kw in combined for kw in ["rate limit", "quota", "overloaded", "429", "too many requests"]):
                rate_limited = True

        if rate_limited:
            log.warning(f"Claude rate-limited for session {session_uuid[:8]}, falling back to GPT")
            return await gpt_fallback(prompt, project)

        if proc.returncode != 0 and e:
            r += f"\n\u26a0\ufe0f {e[:500]}"
        return r.strip() or "(no output)"
    except asyncio.TimeoutError:
        try: proc.kill()
        except: pass
        return "\u23f0 Timeout (60 min)."
    except Exception as e:
        return f"\u274c {e}"

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
def set_active(chat_id: int, session: Session):
    """Set which session is currently focused for this chat."""
    ACTIVE_SESSION[chat_id] = session.id


def get_active(chat_id: int) -> Optional[Session]:
    """Get the currently focused session for this chat."""
    key = ACTIVE_SESSION.get(chat_id)
    if key:
        s = SM.get_by_key(key)
        if s and s.status != "completed":
            return s
    # Fallback: if only 1 active, use it
    return SM.get_default(chat_id)


def resolve_session(update) -> Optional[Session]:
    """Resolve which session a message belongs to.
    Priority: 1) reply-to, 2) active session, 3) single active session."""
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return None
    cid = msg.chat.id

    # 1. Check if replying to a session message → switch active to that
    if msg.reply_to_message:
        reply_id = msg.reply_to_message.message_id
        session = SM.find_by_message(reply_id)
        if session and session.status != "completed":
            set_active(cid, session)
            return session

    # 2. Return active session (most recently interacted)
    return get_active(cid)


async def track_reply(sent_msg: Message, session: Session):
    """Register a sent message as belonging to a session and set it as active."""
    if sent_msg:
        SM.register_message(sent_msg.message_id, session.id)
        session.message_ids.append(sent_msg.message_id)
        # Set as active for this chat
        chat_id_str = session.id.split(":")[0]
        try:
            set_active(int(chat_id_str), session)
        except ValueError:
            pass

# ═══════════════════════════════════════════
#  Core: Execute with Pause Window
# ═══════════════════════════════════════════
async def execute(update, context, prompt, session: Session, files=None, is_voice=False):
    proj = session.project; files = files or []
    fi = f"\n📎 {len(files)} file(s)" if files else ""
    pfx = session_prefix(session)

    # ── Pause window ──
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

    # ── Refine if voice ──
    if is_voice and len(prompt.split()) > 3:
        await cm.edit_text(f"{pfx} | 🧠 <b>Refining prompt...</b>", parse_mode=ParseMode.HTML)
        refined = await refine_prompt(prompt)
        if refined and refined != prompt:
            prompt = refined
            r = await update.message.reply_text(
                f"{pfx} | 📝 <b>Refined:</b>\n<pre>{esc(prompt[:2000])}</pre>",
                parse_mode=ParseMode.HTML)
            await track_reply(r, session)

    # ── Run Claude with progress indicator ──
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

    # Check if this session is the active one — if not, add "switch" button
    cid_str = session.id.split(":")[0]
    try:
        cid_int = int(cid_str)
    except ValueError:
        cid_int = 0
    current_active = get_active(cid_int)
    is_bg_session = current_active and current_active.id != session.id

    # Build reply markup: after_kb + optional switch button
    if is_bg_session:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"↩️ Switch to {session_prefix(session)}", callback_data=f"switch:{session.id}")],
            [InlineKeyboardButton("📦 Save Report", callback_data="do:report"),
             InlineKeyboardButton("🚀 Deploy", callback_data="do:deploy_ask")],
            [InlineKeyboardButton("📊 Health", callback_data="do:health")]])
    else:
        kb = after_kb()

    sent = await send_long(update.message,
        f"{pfx} | \U0001f916 <code>{proj}</code>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n{fmt_out(output)}",
        rm=kb)
    await track_reply(sent, session)

    if len(output) > 8000:
        lnk = await make_report(f"{proj}-auto", output)
        r = await update.message.reply_text(f"\U0001f4ce <b>Full output:</b>\n\n{fmt_links(lnk)}", parse_mode=ParseMode.HTML)
        await track_reply(r, session)

    # Auto-alert if usage is high
    if USAGE.should_alert():
        s = USAGE.get_summary()
        await update.message.reply_text(
            f"\u26a0\ufe0f Hourly usage at {int(s['hourly_pct']*100)}% \u2014 "
            f"consider pausing heavy tasks or switching to lighter prompts",
            parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════
#  Quick Actions (session-independent)
# ═══════════════════════════════════════════
async def do_health(u, c):
    await c.bot.send_chat_action(u.effective_chat.id, ChatAction.TYPING)
    r = subprocess.run([f"{SCRIPTS}/health-check.sh"], capture_output=True, text=True, timeout=30)
    await u.message.reply_text(f"📊 <b>Health</b>\n\n<pre>{esc(r.stdout[:3500])}</pre>", parse_mode=ParseMode.HTML)

async def do_logs(u, c):
    await c.bot.send_chat_action(u.effective_chat.id, ChatAction.TYPING)
    r = subprocess.run([f"{SCRIPTS}/collect-logs.sh", "138.197.76.197", "30"], capture_output=True, text=True, timeout=30)
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
        # Show sessions per project
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
        f"🤖 <b>Shahrzad DevOps Bot v4</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
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
        "\U0001f4d6 <b>How to use \u2014 v4 Multi-Session</b>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        "<b>Sessions:</b>\n"
        "/new &lt;name&gt; \u2014 start a new session (max 3)\n"
        "/sessions \u2014 list all active sessions\n"
        "/kill &lt;name&gt; \u2014 end a session\n"
        "/project &lt;name&gt; \u2014 change project for current session\n"
        "/usage \u2014 view token usage stats\n\n"
        "<b>Routing:</b>\n"
        "Reply to any session message \u2192 routes to that session\n"
        "If only 1 session active \u2192 auto-routes there\n\n"
        "\U0001f4ac <b>Text:</b> Type anything \u2192 Claude Code\n"
        "\U0001f3a4 <b>Voice:</b> Speak in Farsi/English\n"
        "    Short commands auto-execute (\u062f\u06cc\u067e\u0644\u0648\u06cc\u060c \u062a\u0633\u062a...)\n"
        "    Long commands \u2192 refined \u2192 confirm \u2192 execute\n"
        "\U0001f4ce <b>Files:</b> Send files \u2192 then reply with instructions\n"
        "\u23f8 <b>Pause:</b> 5s window to cancel after sending\n"
        "\U0001f4e6 <b>Reports:</b> Tap Save Report \u2192 link for Claude Chat\n\n"
        "⏰ <b>Delayed prompts:</b>\n"
        "<code>:DELAY=30M:</code> your prompt → runs in 30 minutes\n"
        "<code>:DELAY=2H:</code> your prompt → runs in 2 hours\n"
        "/delayed — view/cancel pending\n\n"
        "<b>Smart features:</b>\n"
        "\u2022 Sessions auto-close after 72h inactivity (ZigguratKids4: never)\n"
        "\u2022 GPT fallback when Claude is rate-limited\n"
        "\u2022 Usage alerts at 80% hourly limit\n",
        parse_mode=ParseMode.HTML)

@authorized
async def cmd_usage(u, c):
    """Show usage stats: /usage"""
    cid = u.effective_chat.id
    active = SM.active_for_chat(cid)
    await u.message.reply_text(
        USAGE.format_usage_message(len(active)),
        parse_mode=ParseMode.HTML)

@authorized
async def cmd_new(u, c):
    """Create a new session: /new <name>"""
    cid = u.effective_chat.id
    parts = u.message.text.strip().split(maxsplit=1)
    has_name = len(parts) > 1
    label = parts[1].strip() if has_name else f"session-{int(time.time()) % 10000}"

    if not SM.can_create(cid):
        active = SM.active_for_chat(cid)
        await u.message.reply_text(
            f"\u26a0\ufe0f Max {MAX_SESSIONS} concurrent sessions. Kill one first:",
            parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
        return

    # Show project picker — session created on project selection
    # If no name given, we'll ask for name after project selection
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
    """Kill a session: /kill <name>"""
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
    """Change project for session: /project <name>"""
    session = resolve_session(u)
    parts = u.message.text.strip().split(maxsplit=1)
    # If no session and no args → show project list (same as Projects button)
    if not session and len(parts) < 2:
        await do_projects(u, c)
        return
    if not session:
        # Has args but no session — still show projects
        await do_projects(u, c)
        return
    if len(parts) < 2:
        key = session.id if session else ""
        await u.message.reply_text(
            f"{session_prefix(session)} | 📂 Current: <b>{session.project}</b>",
            parse_mode=ParseMode.HTML, reply_markup=project_kb(key))
        return
    proj = parts[1].strip()
    if not os.path.isdir(f"{REPOS}/{proj}"):
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
    """Show/change active fallback model: /model"""
    prov = ACTIVE_FALLBACK["provider"]
    model = ACTIVE_FALLBACK.get("model", "")

    # Get Gemini models from config
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
#  Delayed Prompt Execution
# ═══════════════════════════════════════════
async def schedule_delayed_prompt(dp: DelayedPrompt, app):
    """Sleep for the delay, then execute the prompt."""
    delay_secs = dp.fire_at - time.time()
    if delay_secs > 0:
        log.info(f"Delayed prompt {dp.id[:8]}: sleeping {delay_secs:.0f}s")
        await asyncio.sleep(delay_secs)

    if dp.cancelled:
        log.info(f"Delayed prompt {dp.id[:8]}: cancelled, skipping")
        return

    dp.fired = True
    chat_id = dp.chat_id
    project = dp.project

    # Try to find the original session
    session = SM.get_by_key(dp.session_key)
    if not session or session.status == "completed":
        # Session closed — create a new one in the same project
        label = f"delayed-{int(time.time()) % 10000}"
        session = SM.create(chat_id, label)
        session.project = project
        try:
            await app.bot.send_message(
                chat_id,
                f"⏰ {session.color_emoji} New session <b>{esc(session.label)}</b> created for delayed prompt.\n"
                f"📁 Project: <b>{project}</b>",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            log.error(f"Delayed prompt notify error: {e}")

    # Notify that the delayed prompt is firing
    try:
        notify_msg = await app.bot.send_message(
            chat_id,
            f"⏰ <b>Delayed prompt firing now!</b>\n"
            f"{session_prefix(session)} | 📂 <code>{project}</code>\n"
            f"Scheduled {dp.delay_str} ago\n\n"
            f"<pre>{esc(dp.prompt[:500])}</pre>",
            parse_mode=ParseMode.HTML)
        await track_reply(notify_msg, session)
    except Exception as e:
        log.error(f"Delayed prompt notify error: {e}")

    # Execute
    session.status = "running"
    session.last_active = time.time()
    try:
        output = await run_claude(dp.prompt, project, session.session_uuid, dp.files or None, session.label)
    except Exception as e:
        output = f"❌ Delayed execution error: {e}"

    session.out = output
    session.status = "idle"
    session.tasks += 1
    session.last_active = time.time()

    try:
        sent = await app.bot.send_message(
            chat_id,
            f"⏰ {session_prefix(session)} | 🤖 <code>{project}</code> (delayed)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n{fmt_out(output)}",
            parse_mode=ParseMode.HTML, reply_markup=after_kb())
        await track_reply(sent, session)

        if len(output) > 8000:
            lnk = await make_report(f"{project}-delayed", output)
            r = await app.bot.send_message(
                chat_id,
                f"📎 <b>Full output:</b>\n\n{fmt_links(lnk)}",
                parse_mode=ParseMode.HTML)
            await track_reply(r, session)
    except Exception as e:
        log.error(f"Delayed prompt output error: {e}")

    # Cleanup
    DELAYED_PROMPTS.pop(dp.id, None)


async def schedule_next_delayed_prompt(dp: DelayedPrompt, app):
    """Wait for session to become idle, then fire 5 minutes later."""
    log.info(f"NEXT delayed prompt {dp.id[:8]}: waiting for session {dp.session_key} to finish task")
    try:
        # Poll every 10 seconds — check if session is idle
        while not dp.cancelled:
            session = SM.get_by_key(dp.session_key)
            if not session or session.status == "completed":
                # Session gone — fire after 5 min from now
                break
            if session.status in ("idle", "error"):
                # Session is idle — wait 5 more minutes then fire
                break
            await asyncio.sleep(10)

        if dp.cancelled:
            log.info(f"NEXT delayed prompt {dp.id[:8]}: cancelled")
            return

        # Wait 5 minutes after task completion
        log.info(f"NEXT delayed prompt {dp.id[:8]}: session idle, waiting 5 min")
        dp.fire_at = time.time() + 300
        dp.delay_str = "after task + 5m"
        await asyncio.sleep(300)

        if dp.cancelled:
            log.info(f"NEXT delayed prompt {dp.id[:8]}: cancelled during wait")
            return

        # Now fire using the standard execution logic
        dp.fired = True
        dp.fire_after_task = False  # reset so standard logic works
        chat_id = dp.chat_id
        project = dp.project

        session = SM.get_by_key(dp.session_key)
        if not session or session.status == "completed":
            label = f"delayed-{int(time.time()) % 10000}"
            session = SM.create(chat_id, label)
            session.project = project
            try:
                await app.bot.send_message(
                    chat_id,
                    f"⏰ {session.color_emoji} New session <b>{esc(session.label)}</b> created for delayed prompt.\n"
                    f"📁 Project: <b>{project}</b>",
                    parse_mode=ParseMode.HTML)
            except Exception as e:
                log.error(f"NEXT delayed prompt notify error: {e}")

        try:
            notify_msg = await app.bot.send_message(
                chat_id,
                f"⏭ <b>Delayed prompt firing now!</b> (after task + 5m)\n"
                f"{session_prefix(session)} | 📂 <code>{project}</code>\n\n"
                f"<pre>{esc(dp.prompt[:500])}</pre>",
                parse_mode=ParseMode.HTML)
            await track_reply(notify_msg, session)
        except Exception as e:
            log.error(f"NEXT delayed prompt notify error: {e}")

        session.status = "running"
        session.last_active = time.time()
        try:
            output = await run_claude(dp.prompt, project, session.session_uuid, dp.files or None, session.label)
        except Exception as e:
            output = f"❌ Delayed execution error: {e}"

        session.out = output
        session.status = "idle"
        session.tasks += 1
        session.last_active = time.time()

        try:
            sent = await app.bot.send_message(
                chat_id,
                f"⏭ {session_prefix(session)} | 🤖 <code>{project}</code> (after task)\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n{fmt_out(output)}",
                parse_mode=ParseMode.HTML, reply_markup=after_kb())
            await track_reply(sent, session)

            if len(output) > 8000:
                lnk = await make_report(f"{project}-delayed", output)
                r = await app.bot.send_message(
                    chat_id,
                    f"📎 <b>Full output:</b>\n\n{fmt_links(lnk)}",
                    parse_mode=ParseMode.HTML)
                await track_reply(r, session)
        except Exception as e:
            log.error(f"NEXT delayed prompt output error: {e}")

    except asyncio.CancelledError:
        log.info(f"NEXT delayed prompt {dp.id[:8]}: task cancelled")
    finally:
        DELAYED_PROMPTS.pop(dp.id, None)


@authorized
async def cmd_delayed(u, c):
    """List and manage delayed prompts: /delayed"""
    cid = u.effective_chat.id
    pending = [dp for dp in DELAYED_PROMPTS.values()
               if dp.chat_id == cid and not dp.fired and not dp.cancelled]

    if not pending:
        await u.message.reply_text("📭 No pending delayed prompts.", parse_mode=ParseMode.HTML)
        return

    lines = []
    rows = []
    for dp in sorted(pending, key=lambda x: x.fire_at):
        if dp.fire_after_task:
            time_info = "⏳ after last task + 5m"
        else:
            remaining = max(0, dp.fire_at - time.time())
            time_info = f"fires in <b>{format_delay(int(remaining))}</b>"
        lines.append(
            f"⏰ <b>{dp.delay_str}</b> → {time_info}\n"
            f"   📂 {dp.project} | 💬 <code>{esc(dp.prompt[:60])}</code>")
        rows.append([
            InlineKeyboardButton("▶️ Send Now", callback_data=f"delaysend:{dp.id}"),
            InlineKeyboardButton("⏭ After Task", callback_data=f"delaynext:{dp.id}"),
            InlineKeyboardButton("❌", callback_data=f"delaycancel:{dp.id}"),
        ])

    await u.message.reply_text(
        f"⏰ <b>Pending Delayed Prompts ({len(pending)})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows) if rows else None)


# ═══════════════════════════════════════════
#  Error Handler
# ═══════════════════════════════════════════
async def error_handler(update, context):
    """Handle errors gracefully."""
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

    # Resolve session from the callback message
    session = SM.find_by_message(q.message.message_id)

    if d.startswith("proj:"):
        # proj:<session_key>:<project>
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
            r = subprocess.run(["git","branch","-r","--sort=-committerdate"], cwd=f"{REPOS}/{proj}", capture_output=True, text=True)
            cbs = [b.strip().replace("origin/","") for b in r.stdout.splitlines() if "claude/" in b]
            br = cbs[0] if cbs else "main"
        await q.edit_message_text(f"⚠️ <b>Deploy {proj}@{br}</b> → production?\n\nSure?", parse_mode=ParseMode.HTML, reply_markup=deploy_confirm_kb(proj, br))

    elif d.startswith("dpl:"):
        _, proj, br = d.split(":")
        await q.edit_message_text(f"🚀 Deploying <b>{proj}@{br}</b>...", parse_mode=ParseMode.HTML)
        r = subprocess.run([f"{SCRIPTS}/deploy-to-prod.sh", proj, br], capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        await c.bot.send_message(cid, f"{'✅' if ok else '❌'} <b>{'Done' if ok else 'Failed'}</b>\n\n<pre>{esc(r.stdout[:3000])}</pre>", parse_mode=ParseMode.HTML)

    elif d == "do:health":
        await c.bot.send_chat_action(cid, ChatAction.TYPING)
        r = subprocess.run([f"{SCRIPTS}/health-check.sh"], capture_output=True, text=True, timeout=30)
        await c.bot.send_message(cid, f"📊 <b>Health</b>\n\n<pre>{esc(r.stdout[:3500])}</pre>", parse_mode=ParseMode.HTML)

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
        # newproj:<label>:<project> — create session with chosen project
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
        # Track the anchor message
        session.anchor_message_id = q.message.message_id
        SM.register_message(q.message.message_id, session.id)
        session.message_ids.append(q.message.message_id)
        log.info(f"Session created (picker): {session.label} → {proj_name} for chat {cid}")

    elif d.startswith("newneed:"):
        # newneed:<auto_label>:<project> — project selected but no name given, ask for name
        parts = d.split(":", 2)
        auto_label = parts[1] if len(parts) > 2 else f"session-{int(time.time()) % 10000}"
        proj_name = parts[2] if len(parts) > 2 else parts[1]
        if not SM.can_create(cid):
            active = SM.active_for_chat(cid)
            await q.edit_message_text(
                f"⚠️ Max {MAX_SESSIONS} sessions. Kill one first.",
                parse_mode=ParseMode.HTML, reply_markup=kill_picker_kb(active))
            return
        # Store state: waiting for session name
        CONV_STATE[cid] = {"state": "awaiting_session_name", "project": proj_name, "auto_label": auto_label}
        await q.edit_message_text(
            f"📁 Project: <b>{esc(proj_name)}</b>\n\nEnter a name for this session (or tap Skip):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip", callback_data=f"skipname:{auto_label}:{proj_name}")]
            ]))

    elif d.startswith("skipname:"):
        # skipname:<auto_label>:<project> — user skipped naming, use auto-generated label
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
        log.info(f"Session created (skip-name): {session.label} → {proj_name} for chat {cid}")

    elif d.startswith("addproj:"):
        # addproj:<label>:<has_name> — user wants to add a new project path
        parts = d.split(":", 2)
        label = parts[1] if len(parts) > 2 else f"session-{int(time.time()) % 10000}"
        has_name_flag = parts[2] if len(parts) > 2 else "1"
        CONV_STATE[cid] = {"state": "awaiting_project_path", "label": label, "has_name": has_name_flag == "1"}
        await q.edit_message_text(
            "📂 Enter the full path on this server:\n"
            "Example: <code>/opt/shahrzad-devops/repos/MyProject</code>\n"
            "Or on production: <code>~/MyNewProject</code>",
            parse_mode=ParseMode.HTML)

    elif d.startswith("skill:"):
        # skill:<session_key> — kill a session via inline button
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
        # route:<session_key> — route pending message to chosen session
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
                # Fallback: run claude directly and send output
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

    elif d.startswith("delaycancel:"):
        delay_id = d.split(":", 1)[1]
        dp = DELAYED_PROMPTS.get(delay_id)
        if dp and not dp.fired:
            dp.cancelled = True
            if dp.task:
                dp.task.cancel()
            DELAYED_PROMPTS.pop(delay_id, None)
            await q.edit_message_text(
                f"❌ Delayed prompt cancelled.\n<code>{esc(dp.prompt[:100])}</code>",
                parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text("❌ Already fired or not found.", parse_mode=ParseMode.HTML)

    elif d.startswith("delaysend:"):
        # Send Now — fire the delayed prompt immediately
        delay_id = d.split(":", 1)[1]
        dp = DELAYED_PROMPTS.get(delay_id)
        if dp and not dp.fired and not dp.cancelled:
            # Cancel the existing timer
            dp.cancelled = True
            if dp.task:
                dp.task.cancel()
            DELAYED_PROMPTS.pop(delay_id, None)
            await q.edit_message_text(
                f"▶️ Sending now...\n<code>{esc(dp.prompt[:100])}</code>",
                parse_mode=ParseMode.HTML)
            # Create a new prompt that fires immediately
            dp_now = DelayedPrompt(
                id=str(uuid.uuid4()), chat_id=dp.chat_id, prompt=dp.prompt,
                project=dp.project, session_label=dp.session_label,
                session_key=dp.session_key, scheduled_at=time.time(),
                fire_at=time.time(), delay_str="now", files=dp.files,
            )
            DELAYED_PROMPTS[dp_now.id] = dp_now
            dp_now.task = asyncio.create_task(schedule_delayed_prompt(dp_now, c.application))
        else:
            await q.edit_message_text("❌ Already fired or not found.", parse_mode=ParseMode.HTML)

    elif d.startswith("delaynext:"):
        # Send 5 min after last task
        delay_id = d.split(":", 1)[1]
        dp = DELAYED_PROMPTS.get(delay_id)
        if dp and not dp.fired and not dp.cancelled:
            # Cancel the existing timer
            if dp.task:
                dp.task.cancel()
            # Convert to fire_after_task mode
            dp.fire_after_task = True
            dp.delay_str = "after task + 5m"
            dp.fire_at = 0  # will be set when task completes
            # Reschedule with the NEXT logic
            dp.task = asyncio.create_task(schedule_next_delayed_prompt(dp, c.application))
            await q.edit_message_text(
                f"⏭ Will send 5 min after last task completes.\n"
                f"📂 {dp.project} | <code>{esc(dp.prompt[:100])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"delaycancel:{dp.id}")]
                ]))
        else:
            await q.edit_message_text("❌ Already fired or not found.", parse_mode=ParseMode.HTML)

    elif d.startswith("dpick:"):
        # User picked a session/project for a delayed prompt
        parts = d.split(":", 2)
        delay_id = parts[1]
        target = parts[2] if len(parts) > 2 else ""
        pending = PENDING_DELAYS.pop(cid, None)
        if not pending or pending["delay_id"] != delay_id:
            await q.edit_message_text("❌ Delay selection expired.", parse_mode=ParseMode.HTML)
        else:
            # Resolve target session
            if target == "__new__":
                label = f"delayed-{int(time.time()) % 10000}"
                session = SM.create(cid, label)
                # Use current session's project as default
                cur = SM.get_by_key(pending["current_session_key"])
                session.project = cur.project if cur else DEFAULT_PROJECT
                try:
                    await c.bot.send_message(
                        cid,
                        f"{session.color_emoji} New session <b>{esc(session.label)}</b> created.\n"
                        f"📁 Project: <b>{session.project}</b>",
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            else:
                session = SM.get_by_key(target)
                if not session or session.status == "completed":
                    await q.edit_message_text("❌ Session no longer active.", parse_mode=ParseMode.HTML)
                    return

            is_next = pending["is_next"]
            delay_secs = pending["delay_secs"]
            dp = DelayedPrompt(
                id=delay_id,
                chat_id=cid,
                prompt=pending["prompt"],
                project=session.project,
                session_label=session.label,
                session_key=session.id,
                scheduled_at=time.time(),
                fire_at=0 if is_next else time.time() + delay_secs,
                delay_str="after task + 5m" if is_next else format_delay(delay_secs),
                files=pending["files"],
                fire_after_task=is_next,
            )
            DELAYED_PROMPTS[delay_id] = dp

            app = c.application
            if is_next:
                dp.task = asyncio.create_task(schedule_next_delayed_prompt(dp, app))
                await q.edit_message_text(
                    f"⏭ <b>Prompt scheduled!</b> (after last task + 5m)\n"
                    f"{session_prefix(session)} | 📂 <code>{session.project}</code>\n\n"
                    f"<pre>{esc(dp.prompt[:300])}</pre>\n\n"
                    f"Use /delayed to view or cancel.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"delaycancel:{delay_id}")]
                    ]))
            else:
                dp.task = asyncio.create_task(schedule_delayed_prompt(dp, app))
                fire_time = datetime.fromtimestamp(dp.fire_at).strftime("%H:%M:%S")
                await q.edit_message_text(
                    f"⏰ <b>Prompt scheduled!</b>\n"
                    f"{session_prefix(session)} | 📂 <code>{session.project}</code>\n\n"
                    f"⏱ Delay: <b>{dp.delay_str}</b>\n"
                    f"🕐 Fires at: <b>{fire_time}</b>\n\n"
                    f"<pre>{esc(dp.prompt[:300])}</pre>\n\n"
                    f"Use /delayed to view or cancel.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"delaycancel:{delay_id}")]
                    ]))
            log.info(f"Delayed prompt {delay_id[:8]} scheduled for session {session.label} ({'NEXT' if is_next else dp.delay_str})")

    elif d == "do:cancel":
        await q.edit_message_text("❌ Cancelled.")

    elif d.startswith("switch:"):
        # switch:<session_key> — user taps "Switch to session" button
        session_key = d.split(":", 1)[1]
        session = SM.get_by_key(session_key)
        if session and session.status != "completed":
            set_active(cid, session)
            await q.answer(f"Switched to {session.color_emoji} {session.label}", show_alert=False)
        else:
            await q.answer("Session not found or ended.", show_alert=True)

    # ── Menu inline buttons ──
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
        r = subprocess.run([f"{SCRIPTS}/health-check.sh"], capture_output=True, text=True, timeout=30)
        await c.bot.send_message(cid, f"📊 <b>Health</b>\n\n<pre>{esc(r.stdout[:3500])}</pre>", parse_mode=ParseMode.HTML)

    # ── Model selection ──
    elif d.startswith("mdl:"):
        parts = d.split(":", 2)
        provider = parts[1] if len(parts) > 1 else "gemini"
        model = parts[2] if len(parts) > 2 else ""

        if provider == "search":
            # Search OpenRouter models
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
        # mdlpick:<model_id> — select from search results
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

    # Auto-create session if none exists
    if not session:
        session = SM.create(u.effective_chat.id, f"task-{int(time.time()) % 10000}")
        anchor = await u.message.reply_text(
            f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b> created.",
            parse_mode=ParseMode.HTML)
        await track_reply(anchor, session)

    if cap.strip():
        all_f = session.files + [lp]; session.files = []
        await execute(u, c, cap.strip(), session, all_f)
    else:
        session.files.append(lp)
        names = [os.path.basename(f) for f in session.files]
        sent = await u.message.reply_text(
            f"{session_prefix(session)} | 📎 <b>Queued ({len(session.files)}):</b>\n" +
            "\n".join(f"  • <code>{esc(n)}</code>" for n in names) +
            "\n\n💬 <i>Send instructions to use these files.</i>",
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

    # Auto-create session if none exists
    if not session:
        session = SM.create(u.effective_chat.id, f"task-{int(time.time()) % 10000}")
        anchor = await u.message.reply_text(
            f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b> created.",
            parse_mode=ParseMode.HTML)
        await track_reply(anchor, session)

        all_f = session.files + [lp]; session.files = []
        await execute(u, c, cap.strip(), session, all_f)
    else:
        session.files.append(lp)
        sent = await u.message.reply_text(
            f"{session_prefix(session)} | 📷 <b>Queued.</b> Files: {len(session.files)}\n💬 <i>Send instructions to use these files.</i>",
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
    # st was already tracked or register it now
    SM.register_message(st.message_id, session.id)

# ═══════════════════════════════════════════
#  Handler: Text
# ═══════════════════════════════════════════
@authorized
async def on_text(u, c):
    t = u.message.text.strip()
    if not t: return
    cid = u.effective_chat.id

    # ── Handle conversation states (multi-step flows) ──
    conv = CONV_STATE.get(cid)
    if conv:
        state = conv.get("state")

        if state == "awaiting_session_name":
            # User typed a session name
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
            log.info(f"Session created (named): {session.label} → {proj_name} for chat {cid}")
            return

        if state == "awaiting_project_path":
            # User typed a project path
            CONV_STATE.pop(cid, None)
            path = t.strip()
            label = conv["label"]
            has_name = conv["has_name"]
            # Derive project name from path
            proj_name = os.path.basename(path.rstrip("/"))
            if not proj_name:
                await u.message.reply_text("❌ Invalid path.", parse_mode=ParseMode.HTML)
                return
            # Add to projects config
            add_project(proj_name, path)
            log.info(f"New project added: {proj_name} → {path}")

            if has_name:
                # Name was already given, create session directly
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
                # No name given — ask for name
                CONV_STATE[cid] = {"state": "awaiting_session_name", "project": proj_name, "auto_label": label}
                await u.message.reply_text(
                    f"📁 Project <b>{esc(proj_name)}</b> added.\n\nEnter a name for this session (or tap Skip):",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭ Skip", callback_data=f"skipname:{label}:{proj_name}")]
                    ]))
            return

        if state == "awaiting_model_search":
            # User typed a model search query
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
            # Simulate /new
            u.message.text = "/new"
            await cmd_new(u, c)
        return

    # Resolve session (uses active session, not just reply-to)
    session = resolve_session(u)

    if not session:
        # No active session at all → auto-create
        session = SM.create(cid, f"session-{int(time.time()) % 10000}")
        anchor = await u.message.reply_text(
            f"{session.color_emoji} Auto-session <b>{esc(session.label)}</b> created.",
            parse_mode=ParseMode.HTML)
        await track_reply(anchor, session)

    # Register the user's message as belonging to this session
    SM.register_message(u.message.message_id, session.id)
    set_active(cid, session)

    # ── Message buffer: concatenate rapid successive messages ──
    # Telegram splits long text into multiple messages. We buffer them
    # and send as one after a short pause (2 seconds of silence).
    buf = MESSAGE_BUFFER.get(cid)
    if buf and buf.get("session_key") == session.id:
        # Append to existing buffer
        buf["texts"].append(t)
        buf["update"] = u
        buf["time"] = time.time()
        # Cancel old timer, set new one
        if buf.get("timer"):
            buf["timer"].cancel()
        buf["timer"] = asyncio.get_event_loop().call_later(
            2.0, lambda: asyncio.ensure_future(_flush_buffer(cid, c)))
        return

    # Start new buffer
    MESSAGE_BUFFER[cid] = {
        "texts": [t],
        "session_key": session.id,
        "update": u,
        "time": time.time(),
        "timer": asyncio.get_event_loop().call_later(
            2.0, lambda: asyncio.ensure_future(_flush_buffer(cid, c))),
    }


async def _flush_buffer(cid: int, context):
    """Flush message buffer and execute combined prompt."""
    buf = MESSAGE_BUFFER.pop(cid, None)
    if not buf:
        return
    if buf.get("timer"):
        buf["timer"].cancel()

    combined = "\n".join(buf["texts"])
    session_key = buf["session_key"]
    update = buf["update"]
    session = SM.get_by_key(session_key)

    if not session or session.status == "completed":
        return

    # ── Check for :DELAY=...: prefix ──
    delay_secs, remaining_text, is_next = parse_delay(combined)
    if delay_secs is not None and remaining_text.strip():
        f = session.files; session.files = []
        delay_id = uuid.uuid4().hex[:8]  # short ID for callback data limits

        # Store pending delay — ask user which session/project to use
        active_sessions = SM.active_for_chat(cid)
        PENDING_DELAYS[cid] = {
            "delay_id": delay_id,
            "prompt": remaining_text.strip(),
            "delay_secs": delay_secs,
            "is_next": is_next,
            "files": f,
            "current_session_key": session.id,
        }

        # Build session/project picker buttons
        picker_rows = []
        for s in active_sessions:
            label_text = f"{s.color_emoji} {s.label} ({s.project})"
            picker_rows.append([InlineKeyboardButton(
                label_text, callback_data=f"dpick:{delay_id}:{s.id}")])
        picker_rows.append([InlineKeyboardButton(
            "🆕 New session (random name)", callback_data=f"dpick:{delay_id}:__new__")])

        delay_label = "after last task + 5m" if is_next else format_delay(delay_secs)
        try:
            confirm = await update.message.reply_text(
                f"⏰ <b>Delayed prompt received!</b>\n"
                f"⏱ Delay: <b>{delay_label}</b>\n\n"
                f"<pre>{esc(remaining_text.strip()[:300])}</pre>\n\n"
                f"📌 <b>Which session/project should this run in?</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(picker_rows))
            await track_reply(confirm, session)
        except Exception as e:
            log.error(f"Delay picker error: {e}")
        return

    f = session.files; session.files = []
    await execute(update, context, combined, session, f or None)

# ═══════════════════════════════════════════
#  Boot
# ═══════════════════════════════════════════
async def session_cleanup_task(app):
    """Background task: clean up timed-out sessions every 5 minutes."""
    try:
        while True:
            await asyncio.sleep(300)  # every 5 min
            try:
                timed_out = SM.cleanup_timed_out()
                for s in timed_out:
                    log.info(f"Session auto-closed (timeout): {s.label}")
                    chat_id_str = s.id.split(":")[0]
                    try:
                        chat_id = int(chat_id_str)
                        await app.bot.send_message(
                            chat_id,
                            f"\u23f0 Session <b>{esc(s.label)}</b> auto-closed after {SESSION_TIMEOUT_MINUTES // 60}h inactivity.",
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
        BotCommand("start", "\U0001f3e0 Home"),
        BotCommand("help", "\U0001f4d6 Help"),
        BotCommand("new", "\u2795 New session"),
        BotCommand("sessions", "\U0001f4cb List sessions"),
        BotCommand("kill", "\U0001f5d1 End session"),
        BotCommand("project", "\U0001f4c2 Change project"),
        BotCommand("model", "\U0001f916 Fallback model"),
        BotCommand("usage", "\U0001f4ca Usage stats"),
        BotCommand("delayed", "⏰ Pending delayed prompts"),
    ])
    # Start background cleanup task
    asyncio.create_task(session_cleanup_task(app))
    log.info(f"Bot v4 ready. Gemini={'\u2705' if GEMINI_OK else '\u274c'} | GPT fallback={'\u2705' if OPENAI_API_KEY else '\u274c'}")

def main():
    if not BOT_TOKEN: print("❌ Set TELEGRAM_BOT_TOKEN"); sys.exit(1)
    log.info("🚀 Starting Shahrzad DevOps Bot v4 (Multi-Session)...")
    from telegram.ext import Defaults
    from telegram.request import HTTPXRequest
    # Increase timeouts to prevent ReadError during long Claude operations
    request = HTTPXRequest(
        read_timeout=60,
        write_timeout=60,
        connect_timeout=30,
        pool_timeout=30,
    )
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).request(request).read_timeout(60).write_timeout(60).connect_timeout(30).pool_timeout(30).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("delayed", cmd_delayed))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
