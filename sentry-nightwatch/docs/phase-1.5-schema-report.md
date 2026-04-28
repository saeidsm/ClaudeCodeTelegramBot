# Phase 1.5 — Schema Validation Report

- **Date (UTC):** 2026-04-26T07:00:23.819685+00:00
- **Sentry org:** `ziggurat-f9`
- **Projects checked:** `shahrzad-backend`, `shahrzad-frontend`

## API calls made

Total: **5 / 6** budget.

| # | Method | URL | Status | ms |
|---|--------|-----|-------:|---:|
| 1 | GET | `https://sentry.io/api/0/organizations/ziggurat-f9/projects/` | 200 | 514 |
| 2 | GET | `https://sentry.io/api/0/projects/ziggurat-f9/shahrzad-backend/issues/?limit=5&statsPeriod=24h` | 200 | 1370 |
| 3 | GET | `https://sentry.io/api/0/projects/ziggurat-f9/shahrzad-frontend/issues/?limit=5&statsPeriod=24h` | 200 | 1380 |
| 4 | GET | `https://sentry.io/api/0/organizations/ziggurat-f9/issues/110471755/events/latest/?full=true` | 200 | 798 |
| 5 | GET | `https://sentry.io/api/0/organizations/ziggurat-f9/releases/?per_page=5` | 200 | 508 |

## Schema diff

### Fields in live response but NOT in fixture (top-level, issue list)

| field path | sample type | impact |
|------------|-------------|--------|
| `annotations` | (live-only) | ignored — normalizer doesn't read it |
| `assignedTo` | (live-only) | ignored — normalizer doesn't read it |
| `hasSeen` | (live-only) | ignored — normalizer doesn't read it |
| `isBookmarked` | (live-only) | ignored — normalizer doesn't read it |
| `isPublic` | (live-only) | ignored — normalizer doesn't read it |
| `isSubscribed` | (live-only) | ignored — normalizer doesn't read it |
| `isUnhandled` | (live-only) | ignored — normalizer doesn't read it |
| `issueCategory` | (live-only) | ignored — normalizer doesn't read it |
| `issueType` | (live-only) | ignored — normalizer doesn't read it |
| `logger` | (live-only) | ignored — normalizer doesn't read it |
| `numComments` | (live-only) | ignored — normalizer doesn't read it |
| `priority` | (live-only) | ignored — normalizer doesn't read it |
| `priorityLockedAt` | (live-only) | ignored — normalizer doesn't read it |
| `seerAutofixLastTriggered` | (live-only) | ignored — normalizer doesn't read it |
| `seerExplorerAutofixLastTriggered` | (live-only) | ignored — normalizer doesn't read it |
| `seerFixabilityScore` | (live-only) | ignored — normalizer doesn't read it |
| `shareId` | (live-only) | ignored — normalizer doesn't read it |
| `stats` | (live-only) | ignored — normalizer doesn't read it |
| `subscriptionDetails` | (live-only) | ignored — normalizer doesn't read it |
| `substatus` | (live-only) | ignored — normalizer doesn't read it |
| `type` | (live-only) | ignored — normalizer doesn't read it |

### Fields expected by normalizer but MISSING in live response

_None._

### Type mismatches

_None._

## Event endpoint probe

| path (matches `collector.fetch_event_full`) | status |
|---------------------------------------------|-------:|
| `/api/0/organizations/{org}/issues/{id}/events/latest/` | **200** |

## Event schema check

- Top-level keys: `_meta`, `context`, `contexts`, `crashFile`, `culprit`, `dateCreated`, `dateReceived`, `dist`, `entries`, `errors`, `eventID`, `fingerprints`, `groupID`, `groupingConfig`, `id`, `location`, `message`, `metadata`, `nextEventID`, `occurrence`, `packages`, `platform`, `previousEventID`, `projectID`, `release`, `resolvedWith`, `sdk`, `sdkUpdates`, `size`, `tags`, `title`, `type`, `user`, `userReport`
- Has `exception` field: **False**
- Has `request` field: **False**
- All required event fields present.

## Verdict

**GREEN** — Live schema matches fixture in every field the pipeline reads.

Verdict legend:
- **GREEN** — schema matches, proceed to Phase 2 unchanged.
- **YELLOW** — minor differences; normalizer handles gracefully (no code change).
- **RED** — live schema breaks the pipeline; code change required.

## Sample of redacted live data (first 30 lines)

```json
{
  "id": "110471755",
  "shareId": null,
  "shortId": "SHAHRZAD-BACKEND-3C",
  "title": "RuntimeError: This event loop is already running",
  "culprit": "cancel_timed_out_story_jobs",
  "permalink": "https://ziggurat-f9.sentry.io/issues/110471755/",
  "logger": null,
  "level": "error",
  "status": "unresolved",
  "statusDetails": {},
  "substatus": "ongoing",
  "isPublic": false,
  "platform": "python",
  "project": {
    "id": "451[REDACTED:phone]20",
    "name": "shahrzad-backend",
    "slug": "shahrzad-backend",
    "platform": "python-fastapi"
  },
  "type": "error",
  "metadata": {
    "value": "This event loop is already running",
    "type": "RuntimeError",
    "filename": "workers/story_tasks.py",
    "function": "_run_async",
    "in_app_frame_mix": "mixed",
    "sdk": {
      "name": "sentry.python.fastapi",
      "name_normalized": "sentry.python"
```

