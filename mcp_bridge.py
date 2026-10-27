"""MCP bridge for the Telegram bot engine (owner-only pilot).

Design contract (docs/MCP_BRIDGE.md):
- NO second engine, NO second SessionManager, NO second queue. Every tool
  call routes through the SAME console/session-API handlers the Telegram
  console uses, via a minimal fake request — so the nonce dedup, the shared
  rate limit, the owner-field refusal, the label/project validation and
  every future console guard apply to MCP verbatim, not as a copy.
- Transport: MCP streamable-HTTP subset (JSON responses) mounted on the
  bot's EXISTING session-API aiohttp app, behind the EXISTING
  X-Session-API-Token (also accepted as Authorization: Bearer, same value).
  No new key, no new account.
- Scope: owner-only (the console-bound chat), projects limited to
  BOT_MCP_PROJECTS (default: the Rag4Engineers pilot), engine claude only in
  v1 (codex/chat stay Telegram/console-only), cancellation only for QUEUED
  work — a running child's process end is NOT provable from the bot, so
  running-cancel stays unavailable per the phase-2 decision.
- Worktree delta capture: bot.run_claude calls capture_worktree_delta()
  before the worktree is removed, so status/diff/changed files/sha256
  manifest survive the turn (see WorktreeSession.preserve_on_exit).

This module deliberately imports NOTHING from bot.py (dependency direction:
bot imports mcp_bridge and injects callables via wire()). That keeps it
unit-testable with fakes, and keeps bot.py the single owner of session
state. Only stdlib + aiohttp.web for the HTTP envelope.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web as _aw

# ── configuration ───────────────────────────────────────────────────────────
MCP_SERVER_NAME = "claude-telegram-bot-mcp"
MCP_SERVER_VERSION = "1.0.0"
MCP_PROTOCOL_VERSION = "2025-06-18"
# Projects the MCP surface may drive. Enforced as an INTERSECTION with the
# registered project registry at every write; the console itself keeps
# accepting every registered project for the Telegram/console paths.
MCP_DEFAULT_PROJECTS = "Rag4Engineers"

# Capture bounds. Changed/untracked files are copied verbatim (bounded);
# nothing else about the worktree leaves the box through a report.
CAPTURE_MAX_FILES = int(os.environ.get("BOT_MCP_CAPTURE_MAX_FILES", "200") or 200)
CAPTURE_MAX_FILE_BYTES = int(os.environ.get("BOT_MCP_CAPTURE_MAX_FILE_BYTES", "") or 5 * 1024 * 1024)
CAPTURE_MAX_TOTAL_BYTES = int(os.environ.get("BOT_MCP_CAPTURE_MAX_TOTAL_BYTES", "") or 25 * 1024 * 1024)
CAPTURE_MAX_DIFF_BYTES = int(os.environ.get("BOT_MCP_CAPTURE_MAX_DIFF_BYTES", "") or 4 * 1024 * 1024)
CAPTURE_ROOT = Path(os.environ.get(
    "BOT_MCP_CAPTURE_DIR", str(Path(os.environ.get("BOT_DATA_ROOT", "/opt/shahrzad-devops")) / "captures")))
# A worktree's .git is a FILE (gitdir pointer), not a directory — isdir()
# would classify every real worktree as "not a git worktree".
_GIT_PATH = os.path.join("{wt}", ".git")
# Never copy a file whose name looks like a credential into a report ZIP —
# nor let one into diff.patch. Broadened after review: name-substring OR
# credential extension OR .env basename OR a bare ssh key (secrets.json,
# credentials.yml, prod.env must all match; .env.example matching is the
# safe direction and costs one skipped copy).
_SECRET_NAME_RE = re.compile(
    r"secret|token|passwd|password|credential|api[_-]?key"
    r"|\.pem$|\.key$|\.age$|\.env($|\.)|(^|/)id_(rsa|ed25519)$", re.I)
# Skips that are deliberate POLICY (content is either forbidden or exists
# elsewhere) and so do NOT invalidate the capture. Anything else — a path
# git printed that we could not read/copy — downgrades ok to False, which
# makes the caller preserve the tree instead of trusting the capture.
_POLICY_SKIP = {"secret_looking_name", "capture_budget"}
# Captures follow the same retention as reports (15 days, pruned lazily).
CAPTURE_RETENTION_DAYS = 15


@dataclass
class CaptureResult:
    """Outcome of one turn's worktree-delta capture.

    ok=False + dirty=True means files existed that we could NOT durably
    save; bot.run_claude then sets WorktreeSession.preserve_on_exit so the
    tree is left on disk instead of being rm -rf'd away."""
    ok: bool
    dirty: bool
    path: str = ""
    reason: str = ""


# ═══════════════════════════════════════════
#  Worktree delta capture (bot.run_claude calls this on EVERY claude turn)
# ═══════════════════════════════════════════

def _run_git(cwd: str, *args: str, timeout: int = 30) -> "tuple[int, str]":
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _safe_session_dir(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)


def capture_worktree_delta(wt_path: str, session_id: str) -> CaptureResult:
    """Capture the uncommitted delta of a git worktree BEFORE its removal.

    Writes status.porcelain, diff.patch, a sha256 manifest and bounded
    copies of changed/untracked files under CAPTURE_ROOT/<safe-sid>/<stamp>/.
    Never raises — failures come back as CaptureResult(ok=False, dirty=True,
    reason=...) so the caller can preserve the tree. Non-git worktrees are
    'clean' by definition (the rsync fallback mirrors the project dir).

    Parsing is machine-readable (-z, quotepath off): quoted/Persian paths and
    renames must never silently turn into dead filesystem paths — an
    unparsable record means a file we could NOT save, which downgrades ok
    (the caller then preserves the tree; C3 after review)."""
    safe = _safe_session_dir(session_id)
    if not os.path.exists(_GIT_PATH.format(wt=wt_path)):
        return CaptureResult(ok=True, dirty=False, reason="not_a_git_worktree")
    try:
        rc, status = _run_git(wt_path, "-c", "core.quotepath=false",
                              "status", "--porcelain=v1", "-z")
        if rc != 0:
            return CaptureResult(ok=False, dirty=True, reason=f"git_status_rc={rc}")
        recs = [r for r in status.split("\0") if r]
        listed, i = [], 0
        while i < len(recs):
            rec = recs[i]
            i += 1
            if len(rec) >= 3:
                listed.append(rec[3:])
            if len(rec) >= 2 and rec[0] in "RC" and i < len(recs):
                i += 1  # rename/copy: the OLD path is a second record (we keep the new)
        rc_ls, others = _run_git(wt_path, "ls-files", "--others",
                                 "--exclude-standard", "-z")
        untracked = [p for p in (others or "").split("\0") if p] if rc_ls == 0 else []
        others_set = set(untracked)
        if not listed and not untracked:
            return CaptureResult(ok=True, dirty=False)  # clean: nothing to lose
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = Path(CAPTURE_ROOT) / safe / stamp
        (out / "files").mkdir(parents=True, exist_ok=True)
        _prune_old_captures(safe)
        manifest = {
            "session": session_id, "worktree": os.path.basename(wt_path),
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": {}, "skipped": [],
            # ponytail: no test-result sniffing in v1 — any file the session
            # wrote (pytest output included) is a changed file and is
            # captured verbatim as one.
            "test_results": "captured_as_changed_files",
        }
        paths = list(dict.fromkeys(listed + untracked))  # de-dup, keep order
        # C2 after review: a secret-named TRACKED file's content must not
        # ride into the ZIP through git diff output — the copy-step filter
        # alone never covered that side. With any secret-looking path in the
        # delta the whole diff is skipped (copies still carry non-secret
        # files); losing the unified view once beats leaking once.
        secret_present = [p for p in paths if _SECRET_NAME_RE.search(p)]
        if secret_present:
            manifest["diff_skipped"] = "secret-looking paths present in the delta"
        else:
            rc_d, diff = _run_git(wt_path, "diff", "--binary")
            manifest["diff_bytes_capped"] = len(diff or "") > CAPTURE_MAX_DIFF_BYTES
            (out / "diff.patch").write_text((diff or "")[:CAPTURE_MAX_DIFF_BYTES],
                                            encoding="utf-8")
        ok = True
        total = count = 0
        for rel in paths:
            if count >= CAPTURE_MAX_FILES or total >= CAPTURE_MAX_TOTAL_BYTES:
                manifest["skipped"].append({"path": rel, "reason": "capture_budget"})
                continue
            src = os.path.join(wt_path, rel)
            try:
                real = os.path.realpath(src)
                if os.path.islink(src) or not os.path.isfile(real) or real.startswith(os.path.join(wt_path, ".git")):
                    reason = "not_a_regular_file"
                elif _SECRET_NAME_RE.search(rel):
                    reason = "secret_looking_name"
                else:
                    data = Path(real).read_bytes()
                    if len(data) <= CAPTURE_MAX_FILE_BYTES:
                        dst = out / "files" / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(data)
                        manifest["files"][rel] = {"sha256": hashlib.sha256(data).hexdigest(),
                                                  "bytes": len(data)}
                        total += len(data)
                        count += 1
                        continue
                    # Untracked content exists nowhere else (git diff never
                    # sees it) — an oversize untracked file is a real loss;
                    # oversize tracked content still sits in diff.patch.
                    manifest["skipped"].append({"path": rel, "reason": "file_too_large"})
                    if rel in others_set:
                        ok = False
                    continue
            except OSError as ex:
                reason = f"os_error:{type(ex).__name__}"
            manifest["skipped"].append({"path": rel, "reason": reason})
            if reason not in _POLICY_SKIP:
                ok = False  # something dirty was NOT durably saved
        (out / "status.porcelain").write_text(status, encoding="utf-8")
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        return CaptureResult(ok=ok, dirty=True, path=str(out))
    except Exception as ex:  # disk full, permissions, git timeout …
        return CaptureResult(ok=False, dirty=True, reason=f"{type(ex).__name__}: {ex}"[:200])


def _prune_old_captures(safe: str) -> None:
    """15-day retention (same as reports), this session's dirs only,
    best-effort — never a reason to fail a capture."""
    try:
        cutoff = time.time() - CAPTURE_RETENTION_DAYS * 86400
        root = Path(CAPTURE_ROOT) / safe
        if not root.is_dir():
            return
        for p in root.iterdir():
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass


def quarantine_stale_worktree(wt_path: str, session_id: str) -> str:
    """C1 after review: a stale worktree whose capture could NOT be proven
    is moved aside under the session's capture root — NEVER rm -rf'd. The
    path is then free for the fresh spawn; `git worktree prune` (caller)
    clears the stale registration. Returns the quarantine dir, '' on
    failure (the caller then refuses the destructive path)."""
    safe = _safe_session_dir(session_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = Path(CAPTURE_ROOT) / safe / f"stale-{stamp}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(wt_path, str(dst))
        return str(dst)
    except Exception:
        return ""


def latest_capture_dir(session_id: str) -> str:
    """Newest capture directory for a session ('' when none). Best-effort:
    a missing/empty tree is not an error, it just means no turn left
    anything behind yet."""
    safe = _safe_session_dir(session_id)
    try:
        latest = max((Path(CAPTURE_ROOT) / safe).iterdir(), key=lambda p: p.stat().st_mtime)
        return str(latest)
    except (OSError, ValueError):
        return ""


# ═══════════════════════════════════════════
#  MCP JSON-RPC surface (deps injected by bot._sessapi_start)
# ═══════════════════════════════════════════

_DEPS: dict = {}


class TurnCapture:
    """Async CM used by bot.run_claude AROUND the WorktreeSession block
    (inner, so its __aexit__ runs BEFORE the worktree is removed).

    Captures the uncommitted delta before cleanup. Cancellation-safe by
    construction: if the capture cannot be PROVEN complete (cancel, timeout,
    error), the worktree is marked preserve_on_exit and left on disk — a
    failed safety net must never become silent data loss."""

    def __init__(self, wt, wt_path: str, session_id: str):
        self.wt = wt
        self.wt_path = wt_path
        self.session_id = session_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        cap = None
        try:
            # 120s > the four _run_git timeouts the capture can stack (30s
            # each) — a budget below the worst case would abort every large
            # capture as "unproven" and leave a zombie thread racing the
            # next rm -rf (found in review).
            task = asyncio.to_thread(capture_worktree_delta, self.wt_path, self.session_id)
            cap = await asyncio.wait_for(asyncio.shield(task), timeout=120)
        except asyncio.CancelledError:
            self.wt.preserve_on_exit = True   # capture unproven: keep the tree
            return False
        except Exception:
            self.wt.preserve_on_exit = True   # capture failed: keep the tree
            return False
        if cap.dirty and not cap.ok:
            self.wt.preserve_on_exit = True
        return False


def wire(deps: dict) -> None:
    """Inject the bot's own callables. bot.py owns every function here;
    the bridge never imports bot (keeps it testable with fakes)."""
    global _DEPS
    _DEPS = dict(deps)


def _allowed_projects() -> list:
    allowed = [p.strip() for p in (_DEPS.get("mcp_projects") or "").split(",") if p.strip()]
    registered = set(_DEPS["project_names"]())
    return [p for p in allowed if p in registered]


def _scope_error(session) -> "tuple[str, int, str] | None":
    """(error, http, detail) when a session is outside the MCP v1 scope,
    else None. One gate for every label-resolving tool: MCP drives ONLY
    claude sessions inside the pilot projects — a Codex session created via
    Telegram must not be drivable from MCP (M2 after review: run_codex has
    no TurnCapture, so driving it from MCP would re-open the exact
    no-capture rm-rf this pilot closes)."""
    if session is None:
        return ("no_such_session", 404, "no session with that label")
    if getattr(session, "project", "") not in _allowed_projects():
        return ("POLICY_DENIED", 403, "session project is outside the MCP pilot scope")
    if getattr(session, "engine", "claude") != "claude":
        return ("ENGINE_UNAVAILABLE", 409, "MCP v1 drives claude sessions only (pilot)")
    return None


_TOOLS = [
    {"name": "projects_list", "description": "Projects this MCP surface may drive (owner pilot scope).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "models_list", "description": "Model families/aliases and what each currently resolves to.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "sessions_list", "description": "Live sessions of the bound owner chat (same SessionManager as Telegram).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "session_new", "description": "Create a session (label, registered project, engine claude, optional model alias).",
     "inputSchema": {"type": "object", "required": ["label", "project"],
                     "properties": {"label": {"type": "string"}, "project": {"type": "string"},
                                    "engine": {"type": "string"}, "model": {"type": "string"}}}},
    {"name": "send", "description": "Submit a turn to a session (resume when it exists). A duplicate nonce is refused, never re-run.",
     "inputSchema": {"type": "object", "required": ["label", "message", "nonce"],
                     "properties": {"label": {"type": "string"}, "message": {"type": "string"},
                                    "nonce": {"type": "integer",
                                              "description": "Epoch milliseconds, strictly increasing per client; anything <= a nonce already seen is refused as a duplicate."}}}},
    {"name": "status", "description": "Status, queued/running state, last output tail, requested/effective model metadata.",
     "inputSchema": {"type": "object", "required": ["label"],
                     "properties": {"label": {"type": "string"}, "full": {"type": "boolean"}}}},
    {"name": "cancel", "description": "Cancel QUEUED work only. Running work cannot be cancelled here: the child's process end is not provable from the bot, so it stays unavailable.",
     "inputSchema": {"type": "object", "required": ["label"], "properties": {"label": {"type": "string"}}}},
    {"name": "report", "description": "Build the report ZIP: summary + latest captured worktree delta (status/diff/changed files/sha256 manifest).",
     "inputSchema": {"type": "object", "required": ["label"], "properties": {"label": {"type": "string"}}}},
]


class _FakeReq:
    """Minimal aiohttp request stand-in: exactly what the console handlers
    read (headers / query / await json() / app['bot']). Routing MCP through
    the REAL handlers is the whole point — no re-implemented validation."""
    __slots__ = ("headers", "query", "_body", "app")

    def __init__(self, body: dict, headers: dict, query: "dict | None" = None, app: "dict | None" = None):
        self.headers = headers
        self.query = query or {}
        self._body = body
        self.app = app if app is not None else {"bot": _DEPS.get("bot")}

    async def json(self):
        if not isinstance(self._body, dict):
            raise ValueError("not a JSON object")
        return self._body


async def _call(handler, body: dict, query: "dict | None" = None) -> "tuple[int, dict]":
    """Run one console handler with a fake request; return (status, payload)."""
    resp = await handler(_FakeReq(body, {"X-Session-API-Token": _DEPS["token"]}, query))
    return resp.status, json.loads(resp.text)


def _tool_text(payload: "dict | None", status: int = 200) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(
        {"http_status": status, **(payload or {})}, ensure_ascii=False)}],
        "isError": status >= 400}


async def _tool_projects_list() -> dict:
    return _tool_text({"ok": True, "projects": _allowed_projects(),
                       "registered_count": len(_DEPS["project_names"]()),
                       "all_registered": False})


async def _tool_models_list() -> dict:
    status, payload = await _call(_DEPS["models"])
    return _tool_text({"ok": True, **payload}, status)


async def _tool_sessions_list() -> dict:
    status, payload = await _call(_DEPS["sessions"])
    return _tool_text(payload, status)


async def _tool_session_new(a: dict) -> dict:
    project = str(a.get("project") or "").strip()
    if project not in _allowed_projects():
        return _tool_text({"ok": False, "error": "POLICY_DENIED",
                           "detail": f"project '{project}' is not in the MCP pilot scope",
                           "allowed": _allowed_projects()}, 403)
    engine = str(a.get("engine") or "claude").strip()
    if engine != "claude":  # v1 pilot: claude only; codex/chat via Telegram/console
        return _tool_text({"ok": False, "error": "ENGINE_UNAVAILABLE",
                           "detail": "MCP v1 is claude-only (pilot)"}, 400)
    body = {"action": "new", "label": a.get("label"), "project": project,
            "engine": engine, "model": a.get("model") or "",
            "nonce": _DEPS["next_nonce"]()}
    status, payload = await _call(_DEPS["control"], body)
    return _tool_text(payload, status)


async def _tool_send(a: dict) -> dict:
    label = str(a.get("label") or "").strip()
    _, session = _DEPS["lookup_session"](label)
    err = _scope_error(session)
    if err:
        return _tool_text({"ok": False, "error": err[0], "detail": err[2]}, err[1])
    body = {"label": label, "message": a.get("message"), "nonce": a.get("nonce")}
    status, payload = await _call(_DEPS["send"], body)
    return _tool_text(payload, status)


async def _tool_status(a: dict) -> dict:
    label = str(a.get("label") or "").strip()
    _, session = _DEPS["lookup_session"](label)
    err = _scope_error(session)
    if err:
        return _tool_text({"ok": False, "error": err[0], "detail": err[2]}, err[1])
    status, payload = await _call(
        _DEPS["output"], {}, {"label": label, "full": "1" if a.get("full") else ""})
    payload["model"] = _DEPS["model_metadata"](session)
    return _tool_text(payload, status)


async def _tool_cancel(a: dict) -> dict:
    label = str(a.get("label") or "").strip()
    _, session = _DEPS["lookup_session"](label)
    err = _scope_error(session)
    if err:
        return _tool_text({"ok": False, "error": err[0], "detail": err[2]}, err[1])
    st = getattr(session, "status", "idle")
    if st == "running":
        # Phase-2 decision: no TERM without proven child ownership + proven
        # process end; releasing the slot on an unproven kill is forbidden.
        return _tool_text({"ok": False, "error": "cancel_unavailable",
                           "detail": "running work: the child's process end is not provable from "
                                     "the bot; use Telegram /kill or wait for completion"}, 409)
    if st != "queued":
        return _tool_text({"ok": True, "label": label, "cancelled": 0, "state": st,
                           "detail": "nothing queued to cancel"})
    # TOCTOU (accepted, ponytail): status may flip queued->running between the
    # check and cancel_session_tasks; that path is the bot's normal /kill
    # task-cancellation, never a blind TERM.
    n = _DEPS["cancel_tasks"](session.id)
    _DEPS["audit"]("mcp_cancel", label, "", f"cancelled={n}")
    return _tool_text({"ok": True, "label": label, "cancelled": n, "state": "queued"})


async def _tool_report(a: dict) -> dict:
    label = str(a.get("label") or "").strip()
    _, session = _DEPS["lookup_session"](label)
    err = _scope_error(session)
    if err:
        return _tool_text({"ok": False, "error": err[0], "detail": err[2]}, err[1])
    res = await _DEPS["report_for"](session)
    return _tool_text({"ok": bool(res.ok), "status": res.status, "error": res.error,
                       "links": dict(res.links), "slug": res.slug})


_DISPATCH = {
    "projects_list": _tool_projects_list, "models_list": _tool_models_list,
    "sessions_list": _tool_sessions_list, "session_new": _tool_session_new,
    "send": _tool_send, "status": _tool_status, "cancel": _tool_cancel,
    "report": _tool_report,
}


async def dispatch_rpc(payload: "dict | None") -> "tuple[int, str]":
    """Pure JSON-RPC dispatch: (http_status, body). Used by handle_http and
    directly by the offline tests. Auth is the CALLER's job (handle_http)."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return 400, json.dumps({"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32600, "message": "invalid request"}})
    method = str(payload.get("method") or "")
    req_id = payload.get("id")
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
        if payload.get("params") is not None:
            return 200, json.dumps({"jsonrpc": "2.0", "id": req_id,
                                    "error": {"code": -32602, "message": "params must be an object"}})
    if req_id is None and method.startswith("notifications/"):
        return 202, ""
    if method == "initialize":
        # Never echo an arbitrary requested version back (we implement one
        # subset); the server answers with what it actually supports.
        return 200, json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION}}})
    if method == "tools/list":
        return 200, json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS}})
    if method == "tools/call":
        name = str(params.get("name") or "")
        tool = _DISPATCH.get(name)
        if tool is None:
            return 200, json.dumps({"jsonrpc": "2.0", "id": req_id,
                                    "error": {"code": -32602, "message": f"unknown tool: {name}"}})
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return 200, json.dumps({"jsonrpc": "2.0", "id": req_id,
                                    "error": {"code": -32602, "message": "arguments must be an object"}})
        try:
            return 200, json.dumps({"jsonrpc": "2.0", "id": req_id, "result": await tool(args)})
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # a failed tool is a JSON-RPC error, never a crash
            return 200, json.dumps({"jsonrpc": "2.0", "id": req_id,
                                    "error": {"code": -32603, "message": f"tool error: {type(ex).__name__}"}})
    return 200, json.dumps({"jsonrpc": "2.0", "id": req_id,
                            "error": {"code": -32601, "message": f"method not found: {method}"}})


async def handle_http(req) -> "_aw.Response":
    """POST /mcp/rpc — the MCP streamable-HTTP endpoint. Same token as the
    console (X-Session-API-Token or Authorization: Bearer with that value).
    Registered by bot._sessapi_start only while BOT_MCP_ENABLED is truthy;
    disabled by default so deployment is an explicit activation."""
    # Auth BEFORE the wired check: enablement state is not for unauthenticated
    # callers to learn (403 without/with a bad token regardless of wiring).
    authorized = _DEPS.get("authorized")
    if not authorized or not authorized(req):
        return _aw.json_response({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32001, "message": "auth_failed"}}, status=403)
    if not _DEPS:
        return _aw.json_response({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32000, "message": "bridge not wired"}}, status=503)
    try:
        payload = await req.json()
    except Exception:
        return _aw.json_response({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "parse error"}}, status=400)
    status, body = await dispatch_rpc(payload if isinstance(payload, dict) else None)
    if status == 202:
        return _aw.Response(status=202)
    return _aw.json_response(json.loads(body) if body else {}, status=status)


def _selfcheck() -> None:
    """import-time smoke: the dispatch table advertises every tool."""
    missing = [t["name"] for t in _TOOLS if not _DISPATCH.get(t["name"])]
    assert not missing, f"tools without a handler: {missing}"


_selfcheck()