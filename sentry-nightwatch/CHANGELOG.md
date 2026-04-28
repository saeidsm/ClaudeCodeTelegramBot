# Changelog

## [Unreleased] — Phase 1 skeleton (2026-04-25)

### Added

- **collector.py** — Sentry REST client with rate limiting (30 req/min token bucket),
  exponential backoff on 429/5xx, and pagination via `Link` header. Token from
  `SENTRY_AUTH_TOKEN` env var, never logged.
- **normalizer.py** — Converts raw Sentry issue dicts to a stable `NormalizedIssue`
  TypedDict (sha-based `stack_signature` for fingerprint dedup) + cross-project
  clustering by time window + shared route.
- **analyzer.py** — Rule-based `score_issue` and `detect_decision_required` that
  flag release-correlated bursts (rollback review hints) and cross-project hotspot
  endpoints. Weights and thresholds loaded from `configs/rules.yml`.
- **redactor.py** — Aggressive PII scrubber covering email, JWT, Stripe IDs,
  Stripe keys, GitHub tokens, AWS pre-signed URL signatures, IPv4/IPv6 (public-only),
  Iranian phone (multiple formats), Iranian national ID (with checksum), UUIDs
  (hash-truncated), session hex (hash-truncated). Authorization/Cookie/Set-Cookie
  values replaced wholesale at both dict and string-form layers. Test fixture
  whitelist preserved.
- **builder.py** — Idempotent snapshot writer producing
  `summary.json`, `issues.csv`, `top_issues.json`, `clusters.json`, `decisions.json`,
  `analysis.md`, `prompt.md`, `evidence/`, and `report.zip`. All outputs routed
  through `redactor.redact()` in `snapshot` mode (evidence files use `evidence` mode).
- **config.py** — Pydantic v2 models for YAML + env config, with `lru_cache` loader.
- **main.py** — Click CLI with `run` / `validate-redactor` / `prune` commands.
  `--dry-run` flag uses fixtures instead of hitting Sentry. Exit codes:
  `0` ok · `1` generic · `2` Sentry unreachable (stub snapshot written) ·
  `3` redactor self-test failed.
- **systemd units** — `nightwatch-daily.service` (oneshot, hardened) and
  `nightwatch-daily.timer` (daily 02:00 Asia/Tehran).
- **scripts/** — Idempotent `install.sh`, clean `uninstall.sh`, and `prune-snapshots.sh`.
- **tests/** — 91 pytest cases (66 redactor, 12 analyzer/normalizer, 8 builder, 5 corpus
  edge). Redactor coverage 92% (above 90% requirement). PII corpus has 32 entries
  covering all redactor patterns.
- **fixtures/** — `pii_corpus.json` (32 entries), `sentry_issues_sample.json`
  (25 issues with fatal+regression+release-correlated+cluster+noisy mix),
  `sentry_event_full.json` (full event with PII for end-to-end redactor verification).

### Notes

- **No systemd units installed.** `install.sh` symlinks them but `enable --now`
  is left to the operator after manual review.
- **No bot integration.** Phase 1 deliberately stops at the snapshot directory;
  Phase 2 will read from there.
- **No LLM calls.** `prompt.md` is an empty placeholder for Phase 3.
