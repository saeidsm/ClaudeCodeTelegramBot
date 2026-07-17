"""Shared Claude/Codex coordination store (Phase 2 §H).

A single atomic, lock-protected central store — NOT a per-worktree file (those
are isolated and invisible to sibling agents). Keyed by canonical repo path so
two code agents working the same repo can see each other's session, engine,
model, branch, worktree, task summary, expected paths, status and timing.

The bot owns entry lifecycle (create on run start, update on completion/cancel,
reconcile stale on startup/shutdown) — we never rely on model self-reporting.
A rendered ``AGENT_COORDINATION.md`` is copied into each ephemeral worktree root
(never committed) so the running agent can read it.

All mutations take an exclusive ``flock`` and do a read-modify-write with an
atomic ``os.replace``, so concurrent tasks/processes cannot corrupt or race the
file. Store ops are synchronous (fcntl); the bot calls them via
``asyncio.to_thread`` to avoid blocking the event loop.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field

COORD_FILENAME = "AGENT_COORDINATION.md"

_ACTIVE = ("queued", "running")


@dataclass
class CoordEntry:
    session_id: str
    engine: str
    model: str
    repo: str                       # canonical repo path
    branch: str = "main"
    worktree: str = ""
    summary: str = ""               # short task summary
    expected_paths: list = field(default_factory=list)
    status: str = "queued"          # queued|running|completed|failed|cancelled|stale
    eta: str = "unknown"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def canonical_repo(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


class CoordinationStore:
    def __init__(self, path: str):
        self.path = path
        self.lock_path = path + ".lock"

    # ── locked read-modify-write ──────────────────────────────────────────
    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            return doc if isinstance(doc, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _write(self, doc: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _mutate(self, fn):
        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                doc = self._read()
                doc.setdefault("entries", {})
                result = fn(doc)
                self._write(doc)
                return result
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    # ── public API ────────────────────────────────────────────────────────
    def upsert(self, entry: CoordEntry) -> None:
        entry.repo = canonical_repo(entry.repo)
        entry.updated_at = time.time()

        def _fn(doc):
            existing = doc["entries"].get(entry.session_id)
            if existing:
                entry.created_at = existing.get("created_at", entry.created_at)
            doc["entries"][entry.session_id] = asdict(entry)
        self._mutate(_fn)

    def set_status(self, session_id: str, status: str) -> None:
        def _fn(doc):
            e = doc["entries"].get(session_id)
            if e:
                e["status"] = status
                e["updated_at"] = time.time()
        self._mutate(_fn)

    def remove(self, session_id: str) -> None:
        def _fn(doc):
            doc["entries"].pop(session_id, None)
        self._mutate(_fn)

    def all_entries(self) -> list[dict]:
        return list(self._read().get("entries", {}).values())

    def entries_for_repo(self, repo: str) -> list[dict]:
        c = canonical_repo(repo)
        return [e for e in self.all_entries() if e.get("repo") == c]

    def reconcile(self, active_ids: set[str]) -> int:
        """On startup/shutdown mark entries whose session is no longer active
        (and still queued/running) as ``stale`` — but never delete active work."""
        def _fn(doc):
            n = 0
            for sid, e in doc["entries"].items():
                if sid not in active_ids and e.get("status") in _ACTIVE:
                    e["status"] = "stale"
                    e["updated_at"] = time.time()
                    n += 1
            doc["_reconciled_at"] = time.time()
            return n
        return self._mutate(_fn)

    # ── rendering ─────────────────────────────────────────────────────────
    def render_markdown(self, repo: str | None = None) -> str:
        entries = self.entries_for_repo(repo) if repo else self.all_entries()
        lines = ["# AGENT COORDINATION",
                 "",
                 "_Shared, bot-managed view of concurrent Claude/Codex agents. "
                 "Read before large edits; do not edit this file._",
                 ""]
        if not entries:
            lines.append("_No active agents recorded._")
            return "\n".join(lines) + "\n"
        by_repo: dict[str, list[dict]] = {}
        for e in entries:
            by_repo.setdefault(e.get("repo", "?"), []).append(e)
        for r, es in sorted(by_repo.items()):
            lines.append(f"## {r}")
            lines.append("")
            lines.append("| session | engine | model | branch | status | eta | task |")
            lines.append("|---|---|---|---|---|---|---|")
            for e in sorted(es, key=lambda x: x.get("updated_at", 0), reverse=True):
                summary = (e.get("summary") or "").replace("|", "/").replace("\n", " ")[:80]
                lines.append(
                    f"| {e.get('session_id','?')} | {e.get('engine','?')} | "
                    f"{e.get('model') or 'default'} | {e.get('branch','?')} | "
                    f"{e.get('status','?')} | {e.get('eta','?')} | {summary} |")
            lines.append("")
        return "\n".join(lines) + "\n"

    def link_into_worktree(self, worktree_root: str, repo: str | None = None) -> str | None:
        """Write a fresh copy of the coordination view into the worktree root.

        Path-safe: refuses to escape ``worktree_root``. Not git-committed (the
        worktree is throwaway/detached). Returns the path written, or None.
        """
        try:
            root = os.path.realpath(worktree_root)
            if not os.path.isdir(root):
                return None
            dest = os.path.join(root, COORD_FILENAME)
            if os.path.realpath(os.path.dirname(dest)) != root:
                return None  # traversal guard
            with open(dest, "w", encoding="utf-8") as f:
                f.write(self.render_markdown(repo))
            return dest
        except Exception:
            return None
