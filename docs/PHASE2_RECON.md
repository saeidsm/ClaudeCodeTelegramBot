# Phase 2 — Read-only Recon Record (redacted)

Generated during Phase A. **No secrets, tokens, `.env` contents, or auth files are reproduced here.**
Only boolean presence and verifiable public capability data.

## Baseline / worktree

| Item | Value |
|------|-------|
| Deployed Phase 1 merge | `9d07c1177c065d2079e1acafabd15f1425ad02d5` |
| `9d07c11` in `origin/main` | yes (verified via `git branch --contains`) |
| Protected main HEAD (metadata worktree) | `c1fcaae [main]` (dirty checkout, untouched) |
| `scripts/worktree-gc.py` sha256 | `b8a916191601404d43d85a84ed4bd693f55e809bc0979c87c4f5e964742098df` |
| Phase 2 worktree | `/tmp/claude-sessions/phase2-engines-codex-chat` (created from `origin/main`) |
| Phase 2 branch | `claude/phase2-engines-codex-chat` |
| Python | 3.12.3 (`/usr/bin/python3`) |

## Installed CLI capabilities

| Tool | Version | Notes |
|------|---------|-------|
| claude | 2.1.211 | `--model` accepts alias (`fable`/`opus`/`sonnet`) or full ID |
| codex | codex-cli 0.144.4 | login: **ChatGPT** (verified `codex login status`, no auth read) |
| graphify | 0.9.5 | CLI-only; `explain`/`path` verified present |
| basic-memory | present (`/root/.local/bin/basic-memory`) | MCP config syntax to be verified before any write (deploy-gated) |

### Claude model catalog (account-visible, `claude models`)

> Snapshot as of Phase A. Current catalogs live in code (`engines/*.py`) and were
> refreshed 2026-08-19 to Opus 5 / Sonnet 5 (+1M), GLM 5.3, Qwen3.8 Max, Kimi K3.

| Label | ID |
|-------|-----|
| Fable 5 | `claude-fable-5` |
| Opus 4.8 | `claude-opus-4-8` |
| Sonnet 5 | `claude-sonnet-5` |
| Haiku 4.5 | `claude-haiku-4-5-20251001` |

Aliases `fable` / `opus` / `sonnet` are accepted by `--model`. Existing bot uses per-session
`Session.claude_model` + `BOT_DEFAULT_CLAUDE_MODEL`; `SONNET_1M_MODEL` default `claude-sonnet-4-6[1m]`.

### Codex model catalog (`codex debug models`, `visibility=list`, `supported_in_api=true`)

| Desired label | Verified slug |
|---------------|---------------|
| Sol | `gpt-5.6-sol` |
| Terra | `gpt-5.6-terra` |
| Luna | `gpt-5.6-luna` |
| (extra) | `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini` |

### Codex `exec` invocation & JSONL protocol (verified with one minimal read-only run)

- Non-interactive: `codex exec --json --skip-git-repo-check -s read-only -C <dir> -m <slug> <prompt>`
- `--json` prints **JSONL events** to stdout. Observed event stream:
  - `{"type":"thread.started","thread_id":"<uuid>"}`  ← **first-turn thread id to capture**
  - `{"type":"turn.started"}`
  - `{"type":"item.completed","item":{"type":"error","message":"..."}}` (non-fatal warnings)
  - `{"type":"item.completed","item":{"type":"agent_message","text":"..."}}` ← **final answer**
  - `{"type":"turn.completed","usage":{"input_tokens":..,"output_tokens":..}}`
  - Also emits a leading human line `Reading additional input from stdin...` on stderr — ignore non-JSON lines.
- Resume: `codex exec resume <thread_id_uuid> --json -m <slug> <prompt>` (UUID takes precedence over name).
- Sandbox modes: `read-only | workspace-write | danger-full-access`. `-o <file>` writes last message; `--output-schema` for structured.
- Login boolean checked via `codex login status` → "Logged in using ChatGPT" (no credentials read).

## OpenRouter

- `OPENROUTER_API_KEY` present: **yes** (env + `/opt/shahrzad-devops/.env`; value never printed).
- `GET https://openrouter.ai/api/v1/models` reachable → **344 models**.
- Resolved required favorite representatives (owner spelling → real API id):

| Owner term | Verified API id | ctx |
|------------|-----------------|-----|
| GLM 5.2 | `z-ai/glm-5.2` | 1048576 |
| MiniMax M3 | `minimax/minimax-m3` | 1048576 |
| Qwen | `qwen/qwen3.7-max` | 1000000 |

- 7 strong current alternatives selected for the 10-favorite config (all verified live):
  `anthropic/claude-opus-4.8`, `anthropic/claude-sonnet-5`, `openai/gpt-5.6-luna`,
  `google/gemini-3.1-pro-preview`, `x-ai/grok-4.5`, `deepseek/deepseek-v4-pro`,
  `meta-llama/llama-4-maverick`.
- Favorites are **owner-configured**, not an objective popularity ranking. Stored as provider ids in config; missing ids are omitted/marked unavailable, never requested.

## Constraints / notes

- Tests must never contact real Telegram/Claude/Codex/OpenRouter/memory/Graphify/GitHub/systemd/Production — use fakes + temp paths.
- basic-memory MCP config for Claude/Codex is **deploy-gated**: verify exact syntax from installed CLIs and back up before any write; implementation phase only adds docs/templates.
- Graphify is CLI-only; use `explain`/`path` (never invented `query`); missing/stale graph is non-fatal.
