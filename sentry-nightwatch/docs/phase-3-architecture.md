# NightWatch Phase 3 — Architecture (Discovery)

> **Status:** Design proposal. Not yet implemented.
> **Author:** Claude Code (DevOps), 2026-04-27.
> **Audience:** Saeid, then Gemini Pro / GPT-5 / Grok / DeepSeek for cold critique.
> **Phase 1+2 reference:** snapshot pipeline, IPC publisher, token-gated reports — all live.

---

## 1. Executive Summary

Phase 3 adds a **Brain** between NightWatch's sensor pipeline and Saeid. The Brain
is a two-tier Gemini reasoning loop (Flash for log triage, Pro for decisions)
that turns nightly snapshots into a *decision package*: a small list of proposed
actions, each tagged with confidence, reversibility class, and a Telegram-ready
prompt. The existing Telegram-Claude-Code bot remains the only **Arm** — Brain
never executes shell or touches code, it composes prompts and the bot spawns
Claude Code as it does today. A persistent MongoDB **incident memory** lets the
Brain recognise repeat failures and reuse past resolutions. Hard ceiling: $1/day
Gemini spend, monitored by the Brain itself. Rollout is phased: propose-only →
ask-first → reversibility-gated autonomy.

---

## 2. System Topology

```
SENSOR LAYER (existing, read-only)
   Sentry → collector → normalizer → redactor → analyzer → builder
   → /opt/sentry-nightwatch/snapshots/YYYY-MM-DD/
       summary.json  top_issues.json  issues.csv  decisions.json
       clusters.json analysis.md  evidence/
   (Phase 3b adds: spike-alerter → spike.json)
                         │ filesystem read (JSON)
                         ▼
BRAIN (new, /opt/sentry-brain/)
   Tier 1 — Gemini 2.5 Flash:
     cluster + dedup findings, match incident_memory, draft candidate actions
                         │ escalate? (heuristic, see §4)
                         ▼
   Tier 2 — Gemini 2.5 Pro:
     rank actions, assign reversibility class, write Claude-Code prompt,
     emit decision_package.json
   MongoDB localhost: incidents · actions · daily_budget
                         │ HMAC POST /brain_inject
                         ▼
ARM (existing claude-telegram-bot.py)
   spawns Claude Code (re-uses MAX_SESSIONS=4, fix-session, ACTIVE_PROCS)
   Claude Code → SSH/edit/commit/PR/restart on devops or prod
   bot writes outcome callback file → Brain reads on next wake-up
                         │ Telegram message + inline buttons
                         ▼
                       SAEID (approve · decline · pause)
```

**Boundaries.**

| From → To              | Protocol        | Auth                    | Payload          |
|------------------------|-----------------|-------------------------|------------------|
| Sensor → Brain         | filesystem      | unix perms              | snapshot dir     |
| Brain → MongoDB        | mongo driver    | localhost only          | BSON             |
| Brain → Bot            | HTTP POST       | HMAC-SHA256 (new secret)| `decision_package` JSON |
| Bot → Claude Code      | subprocess_exec | already trusted         | prompt + cwd     |
| Bot → Saeid            | Telegram API    | bot token + chat_ids    | HTML + inline kb |
| Saeid → Bot (callback) | Telegram        | chat_id allowlist       | callback_data    |

The HMAC secret for `/brain_inject` is **distinct** from the existing
`BOT_NIGHTWATCH_HMAC_SECRET` (which gates digest delivery). Different secret =
different blast radius if leaked.

---

## 3. The Three-Tier Action Model

Every proposed action carries a `tier` ∈ {`auto`, `ask`, `manual`}.

| Tier      | Reversibility       | Brain confidence | Decider         | Notification     |
|-----------|---------------------|-----------------:|-----------------|------------------|
| `auto`    | Trivially reversible| ≥ 0.85           | Brain           | Post-fact report |
| `ask`     | Reversible w/ effort| ≥ 0.60           | Saeid (button)  | Inline approve   |
| `manual`  | Irreversible        | any              | Saeid (typed)   | Plain notification, no button |

**Anchor example.** DevOps VPS has ~3.9 GB RAM, ~2.5 GB free as of 2026-04-27,
hosting shahrzad-caddy, n8n, several alumglass containers. Saeid's first
agentic primitive: stop heavy non-critical containers to free RAM.

Seven concrete actions:

1. `docker stop n8n` — **auto**. Reversible (`docker start n8n`). Trigger:
   free RAM < 400 MB **and** n8n CPU idle 30 min. Brain has an explicit
   allowlist of stoppable containers.
2. `docker start rag` once a free-RAM scare passes — **auto**, mirror of (1).
3. Restart `claude-telegram-bot.service` — **ask**. Reversible but interrupts
   active Claude sessions. Brain reports active count before asking.
4. Apply a 1-line hotfix on `nightwatch-fix/<slug>` and open a PR — **ask**.
   The PR is the reversibility; nothing merges automatically.
5. Rotate `SENTRY_AUTH_TOKEN` after expiry signals — **ask**. Reversible
   but cross-service.
6. Force-merge a PR to main — **manual**. Brain refuses and explains.
7. `DROP TABLE`, restore-from-backup, delete a Docker volume — **manual**.
   Brain proposes the *plan* (a checklist) but never the keystroke.

**Opinion.** The hardest part of this tier system isn't the rules — it's
preventing *creep*. The auto-allowlist must live in code, not prompts.
Adding an `auto` template requires a human-reviewed PR to Brain's config.

---

## 4. Brain Internals

### Trigger model

| Phase | Trigger                          | Source                                |
|-------|----------------------------------|---------------------------------------|
| 3a    | Post-NightWatch nightly run only | Brain wakes after publisher returns 202 |
| 3b    | + spike alerts                   | New `spike-alerter` posts to Brain    |
| 3c    | + manual `/brain_now`            | Bot command                           |

In every phase, Brain runs at most once per trigger. No clocks, no daemons,
no polling — it's a script invoked by an event.

### Inputs

- Latest snapshot dir (path read from `snapshots/last-digest.txt`, already
  written by Phase 2).
- Last 30 days of `incidents` collection (filtered to candidates whose
  `pattern_hash` matches anything in tonight's snapshot).
- Current system probe output: a small JSON the bot can produce on demand
  via a Claude-Code-less helper (`docker stats --no-stream`, `df -h`,
  `systemctl is-active`).
- Today's `daily_budget` document (so Brain knows what it has left to spend).

### Two-tier reasoning loop

```
flash_in  = snapshot.compact() + memory.candidates() + system_probe
flash_out = { findings:[...], candidate_actions:[...],
              needs_pro: bool, tokens_used_{in,out} }

if not flash_out.needs_pro and all confidences low:
    emit no-action decision_package, exit
else:
    pro_in  = flash_out + full_evidence_for_top_findings
    pro_out = decision_package
```

**Escalation heuristic.** Flash sets `needs_pro: true` iff *any* of:
candidate action `confidence_hint ≥ 0.6` · spike count ≥ 5 ·
cross-project cluster present · `release-correlated` flag from analyzer ·
incident_memory match score ≥ 0.7 (probable repeat — Pro should consult
history before acting). Otherwise stop at Flash. **Most nights should stop
at Flash.** A "noisy but boring" night that triggers Pro means the heuristic
is wrong, not the night.

### Output — `decision_package.json`

```json
{ "brain_session_id": "brain-2026-04-27-1735",
  "snapshot_dir":     "/opt/sentry-nightwatch/snapshots/2026-04-27",
  "tier_summary": { "auto": 1, "ask": 2, "manual": 0 },
  "spend_so_far_usd": 0.082,
  "actions": [
    { "id": "act-001", "tier": "auto", "reversibility": "trivial",
      "confidence": 0.91,
      "title": "Restart shahrzad-mongodb — DNS flapping",
      "rationale_short": "12 ServerSelectionTimeoutError, last 03:14 UTC, recovered",
      "claude_prompt": "ssh root@138.197.76.197 'docker restart shahrzad-mongodb' …",
      "expected_outcome": "errors stop within 5 min",
      "rollback": "docker restart again (idempotent)",
      "memory_refs": ["incident-2026-03-19-mongo-flap"] }
  ] }
```

The bot routes by tier (auto: run immediately, ask: button, manual: plain
warning).

---

## 5. Memory Schema (MongoDB)

Database: `nightwatch_brain`, MongoDB on the devops VPS bound to localhost
only, no auth in v1 (network-isolated).

**`incidents`** — pattern library.
```
{ _id, pattern_hash, first_seen, last_seen, occurrences,
  example_issue_ids: [...] (capped 10),
  resolutions: [ { action_id, outcome, duration_s, brain_session_id } ],
  success_rate, related_incident_ids: [...], pinned: bool,
  notes_from_saeid: "..." }
```
Indexes: `pattern_hash` (unique), `last_seen` (TTL 180d unless `pinned`),
`success_rate` desc.

**`actions`** — every action Brain proposed.
```
{ _id, ts, brain_session_id, tier,
  prompt_to_claude_code, claude_session_label,
  outcome ∈ {delivered, ran, failed, reverted, saeid_declined},
  reverted: bool, saeid_approved: bool|null, resulting_pr_url }
```
Indexes: `ts` desc, `brain_session_id`, `claude_session_label`. TTL 90d.

**`daily_budget`** — one doc per UTC date.
```
{ _id: "2026-04-27", spent_usd, calls_flash, calls_pro,
  tokens_in: {flash, pro}, tokens_out: {flash, pro}, ceiling_breached: bool }
```
Indexes: `_id`. TTL 365d (cheap; useful for trends).

---

## 6. Cost Model

> **Pricing as of 2026-04-27 (verify before locking ceiling).**
> Gemini 2.5 Flash: ~$0.30/M input, ~$2.50/M output (text).
> Gemini 2.5 Pro: ~$1.25/M input (≤200K ctx), ~$10/M output (≤200K ctx).
> Gemini 2.5 Flash-Lite: ~$0.10/M input, ~$0.40/M output.

### Realistic per-night estimate

A typical snapshot is ~60 KB compact JSON ≈ 15K tokens. Plus memory candidates
(~3K tokens) and system probe (~1K tokens) → ~19K tokens to Flash. Flash output
~3K tokens. Pro escalation (only on "interesting" nights) reads the same input
plus full evidence for top 3 findings (~30K tokens) and writes a 3K decision.

| Path                          | Input tok | Output tok | Cost (Flash) | Cost (Pro) | Total   |
|-------------------------------|-----------|------------|--------------|------------|---------|
| Quiet night (Flash only)      | 19K       | 3K         | $0.013       | —          | $0.013  |
| Noisy night (Flash + Pro)     | 19K + 30K | 3K + 3K    | $0.013       | $0.067     | $0.080  |
| Pathological night (×3 Pro)   | 19K + 90K | 3K + 9K    | $0.013       | $0.20      | $0.21   |

At one run/night we use $0.01–$0.21/day. **The $1 ceiling is comfortable** —
we only blow it if the heuristic for escalating to Pro is broken. That makes
"calls to Pro per day" the real budget watchdog, not raw dollars.

### What happens at budget thresholds

- **80% (`$0.80`)**: Brain logs a warning, pins itself to Flash-only for the
  remainder of the day. No Pro escalations until the date rolls.
- **100% (`$1.00`)**: Brain emits a *no-action* decision package with reason
  `daily_budget_exhausted`, telegrams Saeid: "Budget hit, paused until UTC
  midnight." No further Gemini calls until tomorrow.
- **120%**: Should be impossible — we check before each call. If it ever
  happens, Brain panics (loud TG message: "BUDGET OVERRUN, here's the audit
  log"), opens an incident, exits.

### Failure modes

- **Gemini API unreachable.** Brain marks the night `degraded`, posts the
  raw NightWatch digest as if Phase 2, and writes an `actions` doc with
  `outcome: "skipped_no_brain"`. Saeid is not blocked; the system degrades
  to Phase 2.
- **Rate-limited.** Exponential backoff up to 3 retries (1, 4, 16 s). Then
  same fallback as unreachable.
- **Quota exceeded mid-night.** Same as 100% threshold.

---

## 7. Safety Model

### How Brain proves an action is reversible

Reversibility is **declared** by the action template, not inferred by Brain.
Each `auto`-eligible action has a hand-written entry in Brain config:

```yaml
- id: docker_restart_container
  template: "docker restart {container}"
  reversibility: trivial
  rollback_template: "docker restart {container}"  # idempotent
  allowed_containers: [ shahrzad-mongodb, shahrzad-redis, n8n, rag-api ]
  forbidden_containers: [ shahrzad-caddy, claude-telegram-bot.service ]
```

If Brain's prompt renders to anything not matching a template, tier
auto-promotes to `manual`. Brain cannot invent new auto actions.

### Audit trail

Every action writes:

1. A Mongo `actions` document.
2. A line to `/var/log/nightwatch-brain/brain.jsonl` (structlog).
3. A bridged `summary.txt` in `/opt/shahrzad-devops/reports/brain-<date>-<slug>/`
   (token-gated webroot — same surface as Claude Code reports).
4. A Telegram message (every action — even `auto` ones get a post-fact
   notification).

### Rollback paths

| Tier    | Rollback                                                      |
|---------|---------------------------------------------------------------|
| auto    | `rollback_template` from action config, runs without asking.  |
| ask     | If declined, no-op. If approved then bad, Saeid types `/undo <action_id>` and bot runs `rollback_template`. |
| manual  | Saeid runs by hand (no Brain involvement).                    |

### Stop button

Two paths:

1. **Soft pause via Telegram inline button** — every Brain message includes
   a "⏸ PAUSE BRAIN" button. Tapping it writes `/var/lib/nightwatch-brain/PAUSED`
   (a file). On every wake-up, Brain checks for that file; if present,
   it exits with no Gemini calls. Saeid resumes via `/brain_resume`.

2. **Hard kill** — `systemctl stop nightwatch-brain.timer` plus `pkill -f
   nightwatch_brain`. Brain has no daemon; the worst it can do is finish
   its current invocation and write nothing more.

The pause flag is checked *both* in Brain and in the bot's `/brain_inject`
handler. Even if a stale Brain run delivers a decision package after pause,
the bot refuses it with `409 brain_paused`.

---

## 8. Phased Rollout

### Phase 3a — Propose-only (target: 2 weeks after kickoff)

- Brain runs nightly, writes `decision_package.json` to disk.
- Bot reads the package and **renders it as a Telegram report**, no actions
  taken. Every action shows tier badge but no buttons.
- Saeid eyeballs decisions for ~10 days.
- **Graduation criterion:** ≥ 80% of proposed actions are ones Saeid says
  "I would have done that." 0% are obviously dangerous. False-positive
  rate < 20%.

### Phase 3b — Ask-first (target: 4 weeks after kickoff)

- `ask` tier gains real inline buttons: APPROVE, DECLINE, EXPLAIN.
- APPROVE → bot spawns Claude Code via `/brain_inject`. Result reported back.
- `auto` tier still renders without acting (still in observation).
- **Graduation criterion:** Saeid's approve rate on `ask` actions ≥ 70%. Of
  approved + executed, ≥ 90% succeed without rollback.

### Phase 3c — Reversibility-gated autonomy (target: 8 weeks after kickoff)

- `auto` tier executes immediately, post-fact notification.
- `auto`-allowed action templates are added one at a time, each with its own
  burn-in (must be `ask` for 14 days first with ≥ 95% success).
- `manual` tier never auto-promotes. Forever.

---

## 9. Integration With Existing Systems

**Bot IPC: new endpoint, not a flag on `/inject`.** Existing `/inject` is
digest delivery (HTML to chat_ids, no Claude spawn). Overloading it with
`from_brain` doubles the bug surface. New `/brain_inject` with its own HMAC
secret = blast-radius isolation, and easy to tear out if Phase 3 fails.

`/brain_inject` body:
```json
{ "brain_session_id": "...",
  "actions": [ { "id": "act-001", "tier": "auto",
                 "claude_prompt": "...", "rollback_template": "...",
                 "expected_outcome": "..." } ],
  "summary_html": "...",
  "report_dir": "/opt/shahrzad-devops/reports/brain-2026-04-27-1735/" }
```
Bot logic: verify HMAC → check pause flag (409 if set) → for each action,
route by tier (auto: spawn Claude; ask: TG message with
APPROVE/DECLINE/EXPLAIN inline buttons + stash under `BRAIN_PENDING[cb_id]`;
manual: plain TG message, no buttons).

**Reading NightWatch outputs.** Brain reads snapshot dirs directly off disk
via `snapshots/last-digest.txt`. No new API. NightWatch stays oblivious of
Brain — strict one-way coupling.

**MongoDB.** Direct PyMongo from a one-shot Brain process under systemd
timer. No service in front. No Brain HTTP listener.

**Claude-Code completion callback.** When a brain-spawned session exits, the
bot writes `/var/lib/nightwatch-brain/inbox/<action_id>.json` with
`{action_id, outcome, claude_session_label, exit_code, git_branch, pr_url}`.
Brain consumes and deletes on next wake. **Opinion:** file-drop beats a new
HTTP listener — no new daemon, no port, simple to inspect. Lossy by design;
if the file is missing, the action stays `delivered` until next wake-up
reconciles from the bot's logs.

---

## 10. Open Questions / Requirements Gaps

1. **Auto allowlist contents.** Which exact containers/services are safe to
   restart unattended? §3 sketches it; the final list is load-bearing.
   *(Needs Saeid.)*
2. **Spike-alerter thresholds.** Sentry event count > N in M minutes —
   N, M = ? *(Needs Saeid + experimentation.)*
3. **Memory cold-start.** First 2 weeks `incidents` is empty. Be more
   conservative, or back-fill from past 30 days of snapshots?
   *(Recommend: back-fill on first run.)*
4. **Saeid response-latency for `ask`.** Action proposed 23:01 UTC, Saeid
   asleep until 06:00 — auto-decline at +6h, or hold forever? *(Needs Saeid.)*
5. **Re-running on same snapshot.** If Saeid `/brain_now` re-runs the same
   date, recharge budget or cache? *(Recommend: cache Flash output, Pro
   re-runs cost money.)*
6. **Prod SSH scope.** The bot already SSHes prod as root. Should
   Brain-spawned Claude Code use that, or a narrower user? *(Recommend:
   read-only SSH user by default, write only for explicit allowlist actions.)*
7. **Telegram rate limits.** Pathological night → 10+ findings × 3 msgs.
   Bundle vs. flood. *(Needs experimentation.)*
8. **Mongo backup.** 6 months of `incidents` is load-bearing. Same DO
   Spaces bucket as snapshots? *(Needs Saeid.)*
9. **Multi-LLM consultation in production.** Should Brain itself consult
   GPT-5/Grok/DeepSeek for high-stakes calls, or stay Gemini-only?
   *(Open; out of scope for MVP.)*
10. **Brain federation.** Proposal pins Brain to devops-VPS. Prod could
    eventually host its own Brain. Out of scope for Phase 3 (see §11).

---

## 11. Explicit Non-Goals

- **Brain self-modification.** No editing its own prompts, configs, allowlist.
  PR-gated only.
- **Predictive maintenance / anomaly forecasting.** Brain reacts to what
  NightWatch sees. No prediction of next week's incidents.
- **Multi-tenant.** Brain manages Shahrzad services for Saeid. No per-customer
  config.
- **Direct Brain → prod execution.** Every action via bot → Claude Code.
  Brain holds no credentials beyond the Gemini API key.
- **Brain rewriting Claude Code's results.** Brain records outcomes, never
  patches Claude's diffs or pushes commits Claude didn't make.
- **Replacing Saeid's decision-making.** Brain proposes; Saeid disposes.
  `auto` tier exists only because Saeid allowlisted those templates.
- **Always-on Brain daemon.** Triggered by timer or event. No live agent loop.

---

## 12. Consultation Prompts

Each prompt assumes the model has been given this entire document as attached
context. Same doc, different framing — to surface different blind spots.

### 12.1 Gemini 2.5 Pro — "the model that will run this"

> You are Gemini 2.5 Pro. The attached *NightWatch Phase 3 Architecture*
> proposes that you (and Flash) be the reasoning tier of an SRE agent for
> a one-person startup, hard-capped at $1/day across all Gemini calls.
> Answer:
>
> 1. Is the cost model in §6 realistic against current pricing? Where will
>    I overshoot?
> 2. The Flash→Pro escalation heuristic in §4 — well-defined, or does it
>    leak subjective judgement into Flash output?
> 3. The `decision_package.json` shape in §4 — what's missing that you'd
>    need to make a high-confidence call?
> 4. How would you constrain Pro further (system-prompt boundaries,
>    schema-only output, refusal patterns) so I don't hallucinate actions
>    outside the allowlist?
>
> Be terse. Bullet-point critique. List specific gaps; don't rewrite the doc.

### 12.2 GPT-5 — "the SRE expert"

> You are an SRE staff engineer reviewing a junior team's design. They run
> a tiny stack (~3 GB RAM VPS, one Telegram-driven CLI agent, no PagerDuty,
> single operator) and want to add an LLM "Brain" between observability
> and human action. Read the attached *NightWatch Phase 3 Architecture*
> and answer:
>
> 1. Highest-impact missing safety control beyond §7?
> 2. The §3 three-tier model — does the `auto`/`ask` boundary survive
>    contact with a real production incident? Where does it break?
> 3. Is the §7 audit trail enough to *post-mortem* a bad Brain decision
>    30 days later? Can you reconstruct what Brain saw at the time?
> 4. The §8 graduation criteria — measurable enough to actually graduate?
>    Where would a paranoid SRE add a hard gate?
>
> Push on operational reality, not architectural elegance.

### 12.3 Grok — "the contrarian"

> You are the loyal opposition. The attached *NightWatch Phase 3
> Architecture* adds an LLM "Brain" between observability and a single
> human on a one-person startup VPS. Read it, then answer:
>
> 1. What's *fundamentally wrong* with this approach? Should this system
>    exist at all, or is the operator over-engineering instead of just
>    reading their Sentry digest themselves?
> 2. Two-tier Flash/Pro — actually saving money, or creative accounting
>    that hides cost in complexity?
> 3. The "$1/day budget" — real constraint or theatre? What's the doc
>    *really* trying to constrain?
> 4. Pick the section you find most embarrassing and say why.
>
> Be sharp. The author can take it. Don't soften.

### 12.4 DeepSeek — "the implementation realist"

> You are reviewing the attached *NightWatch Phase 3 Architecture* with a
> focus on technical implementation. The team will build on a 2.8K-line
> aiohttp Telegram bot, a NightWatch JSON-snapshot pipeline, and a
> localhost MongoDB. Answer:
>
> 1. §5 MongoDB schema — what bites at scale? Indexes, TTLs, document
>    growth, anything else?
> 2. §9 Bot↔Brain integration — `/brain_inject` is HTTP but the callback
>    is a file-drop. Is that inconsistency a problem? What would you choose?
> 3. §7 pause-flag — race conditions if the file is created mid-Brain-run?
> 4. §10 #3 cold-start back-fill of 30 days of past snapshots — actual
>    cost, and where does the schema not yet support it?
>
> Code-level critique. Concrete failure modes, not abstractions.

---

*End of document.*
