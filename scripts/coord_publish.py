#!/usr/bin/env python3
"""Agent-invokable coordination claim publisher / reader (Phase 2 §H).

Run it via the ABSOLUTE path the bot injects (``$AGENT_COORD_PUBLISHER``) — the
agent's cwd is a worktree of an arbitrary project that does NOT contain a
relative ``scripts/coord_publish.py``. Publish your claim / expected paths / ETA
into the SHARED, flock-protected central store so sibling Claude/Codex agents can
see what you are working on, or read the live central view:

    python3 "$AGENT_COORD_PUBLISHER" --summary "refactor auth" \
        --paths src/auth.py,tests/test_auth.py --eta 15m
    python3 "$AGENT_COORD_PUBLISHER" --show          # live central snapshot

Session identity is COOPERATIVE, not a security boundary: the session id and the
store path come from the environment the bot injected (AGENT_COORD_SESSION /
AGENT_COORD_STORE). A shell-capable agent could override these — nothing here
enforces isolation. By convention you update ONLY your own already-registered
entry; field lengths/counts are bounded by the store. Exits non-zero on any
problem but never raises to the caller's shell in a way that blocks the agent.
"""
from __future__ import annotations

import argparse
import os
import sys

# coordination.py ships next to the bot modules. Support both layouts:
# publisher in <bot>/scripts/coord_publish.py (coordination.py one level up) and
# publisher deployed alongside coordination.py in the same dir. Locating it by
# __file__ (not cwd) is what lets this run from a FOREIGN project worktree.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> int:
    store = os.environ.get("AGENT_COORD_STORE", "").strip()
    session_id = os.environ.get("AGENT_COORD_SESSION", "").strip()
    repo = os.environ.get("AGENT_COORD_REPO", "").strip() or None

    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default=None, help="short task summary")
    ap.add_argument("--paths", default=None, help="comma-separated expected paths")
    ap.add_argument("--eta", default=None, help="human ETA e.g. 10m / unknown")
    ap.add_argument("--show", action="store_true",
                    help="print the live central coordination view and exit")
    args = ap.parse_args()

    if not store:
        print("coord_publish: not running under bot coordination (AGENT_COORD_STORE unset).")
        return 0

    try:
        import coordination
    except Exception as e:
        print(f"coord_publish: coordination module unavailable: {e}")
        return 1

    cs = coordination.CoordinationStore(store)

    if args.show:
        # Live, point-in-time-now view (filtered to this repo when known). The
        # markdown copied into your worktree is a snapshot from creation time.
        sys.stdout.write(cs.render_markdown(repo))
        return 0

    if not session_id:
        print("coord_publish: AGENT_COORD_SESSION unset; nothing to publish.")
        return 0

    paths = None
    if args.paths is not None:
        paths = [p.strip() for p in args.paths.split(",") if p.strip()]

    ok = cs.update_claim(session_id, summary=args.summary,
                         expected_paths=paths, eta=args.eta)
    if not ok:
        print("coord_publish: no matching active entry for this session (nothing updated).")
        return 2
    print("coord_publish: claim updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
