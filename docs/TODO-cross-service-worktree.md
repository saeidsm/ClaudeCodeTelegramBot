# TODO: Worktree isolation for claude-api and ttyd services

The Phase B PR (fix/phase-b-multibot-model-selection) implemented per-session
git worktrees in claude-telegram-bot.service. Two sibling services on the
DevOps VPS spawn claude against the same /opt/shahrzad-devops/repos/
worktrees and have the same cross-session contamination risk.

## Affected services

### claude-api.service (n8n integration)
- Spawns claude in response to n8n webhook calls
- Estimated change: ~30 lines (replace cwd with WorktreeSession)
- Priority: medium — only Saeid uses n8n today

### ttyd.service (web terminal)
- Human-driven shell — not bot-spawned claude calls
- No worktree fix needed; humans choose their own working dir
- Priority: none

## When to fix claude-api

When Saeid expands n8n usage or onboards a teammate to use n8n flows
that involve claude. Until then, the existing shared-cwd is acceptable
because Saeid is the only operator and won't have collision races.

## Reference implementation

See bot.py:
- WorktreeSession class (around line 2470)
- run_claude integration (around line 750)

The same pattern applies: per-request worktree at /tmp/claude-api-sessions/<request_id>,
remove on completion.
