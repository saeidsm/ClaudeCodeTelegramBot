#!/usr/bin/env python3
"""Agent-invokable coordination claim publisher (Phase 2 §H).

Run from inside your worktree to publish your task claim / expected paths / ETA
into the SHARED, flock-protected coordination store so sibling Claude/Codex
agents can see what you are working on:

    python3 scripts/coord_publish.py --summary "refactor auth" \
        --paths src/auth.py,tests/test_auth.py --eta 15m

Authorization: the target session id and the store path come ONLY from the
environment the bot injected (AGENT_COORD_SESSION / AGENT_COORD_STORE). You can
update ONLY your own already-registered entry — never another session's.
Field lengths/counts are bounded by the store. Exits non-zero on any problem
but never raises to the caller's shell in a way that blocks the agent.
"""
from __future__ import annotations

import argparse
import os
import sys

# coordination.py sits at the worktree root (parent of this scripts/ dir).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> int:
    store = os.environ.get("AGENT_COORD_STORE", "").strip()
    session_id = os.environ.get("AGENT_COORD_SESSION", "").strip()
    if not store or not session_id:
        print("coord_publish: not running under bot coordination (env unset); nothing to do.")
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default=None, help="short task summary")
    ap.add_argument("--paths", default=None, help="comma-separated expected paths")
    ap.add_argument("--eta", default=None, help="human ETA e.g. 10m / unknown")
    args = ap.parse_args()

    try:
        import coordination
    except Exception as e:
        print(f"coord_publish: coordination module unavailable: {e}")
        return 1

    paths = None
    if args.paths is not None:
        paths = [p.strip() for p in args.paths.split(",") if p.strip()]

    ok = coordination.CoordinationStore(store).update_claim(
        session_id, summary=args.summary, expected_paths=paths, eta=args.eta)
    if not ok:
        print("coord_publish: no matching active entry for this session (nothing updated).")
        return 2
    print("coord_publish: claim updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
