# sentry-nightwatch

> **Phase 1 — offline pipeline only.** No bot integration, no Telegram, no LLM.
>
> Pulls Sentry issues for two projects (`shahrzad-backend`, `shahrzad-frontend`),
> normalizes + redacts (PII), scores them with simple rules, and writes a
> daily snapshot directory under `snapshots/YYYY-MM-DD/` plus a bundled `report.zip`.

## Layout

```
app/
  collector.py     Sentry REST client (HTTP only, no business logic)
  normalizer.py    Schema unification + cross-project clustering
  analyzer.py      Rule-based scoring (loads weights from configs/rules.yml)
  redactor.py      PII scrubbing — security boundary
  builder.py       Snapshot writer (JSON + Markdown + ZIP)
  config.py        YAML + env loader
  main.py          CLI: run / validate-redactor / prune
configs/
  projects.yml     Sentry org + project map
  rules.yml        Scoring weights and thresholds
tests/             pytest suite (>= 90% coverage on redactor.py)
systemd/           Unit files (NOT installed yet — install.sh handles that)
scripts/           install.sh, uninstall.sh, prune-snapshots.sh
snapshots/         Runtime output, gitignored, retained 30 days by default
```

## Quick start

```bash
# 1. Install
cd /opt/sentry-nightwatch
./scripts/install.sh

# 2. Configure
cp .env.example .env
# Fill in SENTRY_AUTH_TOKEN and SENTRY_ORG_SLUG.

# 3. Smoke test (no network)
venv/bin/python -m app.main run --dry-run --date 2026-04-24

# 4. Run for real
venv/bin/python -m app.main run --verbose

# 5. Enable daily timer (after dry-run works)
sudo systemctl enable --now nightwatch-daily.timer
```

## Snapshot anatomy

For each date, the pipeline writes:

```
snapshots/2026-04-24/
  summary.json        Top-level stats (counts by severity, top 10 ids)
  issues.csv          All issues with full normalized fields
  top_issues.json     Top 20 by severity_score with scoring breakdown
  clusters.json       Cross-project cluster info
  decisions.json      Action items (rollback hints, hotspot endpoints)
  analysis.md         Human-readable Markdown digest (no LLM)
  prompt.md           Phase-3 placeholder
  evidence/           Full event JSON for top 10 issues, redacted in 'evidence' mode
  report.zip          Everything above bundled
```

All output passes through `redactor.redact()` in **snapshot** mode; the `evidence/`
subdirectory uses **evidence** mode (slightly more permissive — keeps debug-relevant
context like public IPs, but secrets are still scrubbed).

## Smoke tests (5 deterministic checks)

> Each test has a single-line PASS criterion. Saeid runs each one and confirms PASS.

### TEST 1 — Module imports

```bash
cd /opt/sentry-nightwatch && venv/bin/python -c \
  "from app import collector, normalizer, analyzer, redactor, builder, config, main"
```

**PASS:** Exit 0, no `ImportError`.

### TEST 2 — Redactor self-defense

```bash
cd /opt/sentry-nightwatch && venv/bin/python -m app.redactor
```

**PASS:** Output ends with `REDACTOR OK: 0 leaks across N patterns` where `N >= 30`.

### TEST 3 — Pytest suite

```bash
cd /opt/sentry-nightwatch && venv/bin/pytest tests/ -v
```

**PASS:** All collected tests pass; `coverage of redactor.py >= 90%`. Coverage check:
`venv/bin/pytest tests/ --cov=app.redactor --cov-fail-under=90`.

### TEST 4 — Dry-run snapshot

```bash
cd /opt/sentry-nightwatch && venv/bin/python -m app.main run --dry-run --date 2026-04-24
```

**PASS:** Exit 0; `snapshots/2026-04-24/` exists with `summary.json`, `issues.csv`,
`top_issues.json`, `clusters.json`, `decisions.json`, `analysis.md`, `prompt.md`,
`evidence/`, `report.zip`. No file in the snapshot contains the literal string
`admin@shahrzad.ai` or any value from `pii_corpus.json` marked `"secret": true`.

### TEST 5 — Systemd timer dry-load

```bash
sudo systemctl daemon-reload && \
  systemd-analyze verify /opt/sentry-nightwatch/systemd/nightwatch-daily.{service,timer}
```

**PASS:** Zero errors or warnings printed.

## Operational notes

- **Idempotency.** Re-running for the same date overwrites the snapshot cleanly.
- **Failure mode.** If Sentry is unreachable, exit code is `2` and a stub snapshot
  is still written (status=`sentry_unreachable`) so downstream phases find a directory.
- **Memory.** Pagination is streamed; the whole pipeline stays well under 200 MB.
- **Logs.** All logs go to stderr in JSON via structlog. The token is never logged.
- **Retention.** `prune-snapshots.sh` (or `python -m app.main prune --days N`) deletes
  snapshot directories older than `N` days (default 30).

## CLI reference

```
python -m app.main run [--dry-run] [--date YYYY-MM-DD] [--verbose]
python -m app.main validate-redactor
python -m app.main prune [--days N]
```

Exit codes: `0` ok · `1` generic · `2` Sentry unreachable · `3` redactor self-test failed.
