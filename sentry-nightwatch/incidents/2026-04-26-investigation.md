# Incident Investigation — 2026-04-26 (NightWatch CRITICAL)

**Investigator:** Claude (DevOps agent, devops.shahrzad.ai)
**Time of investigation:** 2026-04-27 ~18:50 UTC
**Scope:** investigation only — no fixes applied, no containers restarted, no code/config changed.
**Production status at conclusion:** **HEALTHY** (1.6 GiB free, no swap, all containers Up & healthy 9h, all queues empty).

---

## TL;DR

1. **Issue #1 (817× "event loop is already running") is already fixed in code and the fix is now running on prod.** Root cause was concurrent `loop.run_until_complete` from multiple threads when `celery-worker` ran under the `threads` pool. PR #82 (switch to `prefork`) + PR #84 (drop concurrency 4→2) eliminate the race. Last event for this fingerprint: **06:45 UTC**, before the prefork container started at **10:20:30 UTC**. Action: mark resolved in Sentry; verify next NightWatch shows zero new occurrences.

2. **Issues #2 + #3 (10× SIGKILL ForkPoolWorker, UP-timeout, WorkerLostError) are deploy-time noise from today's PR #82+#84 rollout, not a steady-state regression.** Two distinct OOM mechanisms fired during the 07:18→10:20 UTC deploy window: (a) **cgroup OOM** on `celery-worker` (512 MiB limit too tight for `prefork -c 2` during boot/imports), and (b) **global system OOM** while `docker buildx` ran on the 4 GiB VPS alongside running services. Since the final container start at 10:20:30 UTC, **zero new SIGKILL events for 8+ hours**.

3. **Safe fix is non-urgent.** The minimal change is to raise `celery-worker` mem limit 512 MiB → 768 MiB (one-line compose edit, redeploy). The deeper fix is moving image builds off the prod VPS so future deploys don't OOM the host. **Do not restart anything now** — the system is quiet and stable.

---

## Evidence summary

### STEP 1 — OOM theory: CONFIRMED (high confidence)

`journalctl -k` on prod shows **dozens of OOM-killer events in the 48 h window**, clustered tightly around two deploys:

```
Apr 27 07:18:16 ... global_oom ... task=uvicorn pid=2390348 anon-rss:390040kB     # deploy attempt #1 starts
Apr 27 07:18:21 ... CONSTRAINT_MEMCG ... cgroup=docker-203da69e... task=celery anon-rss:137416kB
Apr 27 07:18:25 ... CONSTRAINT_MEMCG ... cgroup=docker-203da69e... task=celery anon-rss:138640kB
Apr 27 07:23:36 ... global_oom (docker-buildx) ... task=uvicorn anon-rss:299844kB
Apr 27 07:23:40 ... CONSTRAINT_MEMCG ... task=celery anon-rss:136628kB           # → Sentry SIGKILL (#115490790)
Apr 27 07:24:00 ... CONSTRAINT_MEMCG ... task=celery anon-rss:134032kB
Apr 27 07:25:12 ... CONSTRAINT_MEMCG ... task=celery anon-rss:136060kB
Apr 27 07:29:21 ... global_oom (docker-compose) ... task=mongod anon-rss:152824kB # mongo also killed!
Apr 27 07:29:25 ... CONSTRAINT_MEMCG ... task=celery anon-rss:138324kB           # → Sentry WorkerLostError (#115492220)
Apr 27 10:18:06 ... global_oom ... cgroup=docker-6900eafd... task=celery anon-rss:140172kB  # → SIGKILL (#115490790) + UP-timeout (#115544899)
```

Two distinct cgroups appear:
- `docker-203da69e...` = the 07:18 deploy's celery-worker (CGROUP-bounded OOMs at ~135 MiB anon-rss per process; cgroup ceiling is 512 MiB and master+2 children + Python imports tip it over).
- `docker-6900eafd...` = the 10:18 deploy's celery-worker (killed by **global** OOM during the next docker-buildx run, not by its own cgroup).

`mongod` was also OOM-killed once (Apr 27 07:29:21) — same global-OOM event window. mongo container has been **Up 11 hours** = it restarted and recovered cleanly.

**Verdict: SIGKILL was OOM-killer, not liveness probe, not explicit signal.**

### STEP 2 — Memory pressure (live snapshot, taken 18:49 UTC)

Host (prod, 138.197.76.197):
```
Mem: total 3.8Gi  used 2.0Gi  free 310Mi  available 1.6Gi
Swap: 0B  (no swap configured anywhere)
Load avg: 0.51 0.51 0.55   uptime 2d 23h
```

Per-container (`docker stats --no-stream`):

| Container                 | RSS / Limit       | Mem % |
|---------------------------|-------------------|-------|
| celery-worker (general)   | **268.9 MiB / 512 MiB** | **52.5 %** ← tight |
| celery-worker-stories     | 153.5 MiB / 1.5 GiB    | 10.0 % |
| celery-beat               | 140.9 MiB / 3.82 GiB   | 3.6 % |
| backend                   | 411.5 MiB / 3.82 GiB   | 10.5 % |
| mongodb                   | 238.1 MiB / 3.82 GiB   | 6.1 % |
| frontend                  | 87.7 MiB / 3.82 GiB    | 2.2 % |
| redis                     | 10.1 MiB / 3.82 GiB    | 0.3 % |
| nginx                     | 8.6 MiB / 3.82 GiB     | 0.2 % |

Compose limits set explicitly only on the two celery workers (lines 140 and 186 of docker-compose.yml). All other containers inherit the host limit (3.82 GiB). **Steady-state celery-worker is sitting at 52 % of its 512 MiB cap with one general-queue process idle** — under prefork c=2 with active tasks the headroom is small but currently sufficient. Memory matches project memory note "stories bumped 512M→1536M".

### STEP 3 — Event-loop error code-path: ROOT CAUSE FOUND

Sentry issue **110471755** (817 events, first_seen 2026-04-08, **last_seen 2026-04-27 06:45**). Stack:

```
celery/app/trace.py:760  __protected_call__       (Celery prefork wrapper)
workers/story_tasks.py:90 cancel_timed_out_story_jobs_task
workers/story_tasks.py:25 _run_async   →  loop.run_until_complete(coro)
asyncio/base_events.py:630 run_until_complete  →  self._check_running()
RuntimeError: This event loop is already running
```

`workers/celery_app.py:207-220` keeps a **module-global persistent loop**, created in `worker_process_init` (per-process signal). Code:

```python
_worker_loop: asyncio.AbstractEventLoop | None = None
def get_worker_loop() -> asyncio.AbstractEventLoop:
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
        ...
```

This is **safe under prefork** (one loop per child process, one task at a time per child). It is **unsafe under threads** (multiple threads in one process share the global loop; thread B calls `run_until_complete` while thread A is still inside one → asyncio raises "event loop already running"). The Sentry event tag confirms threading: `data.thread.name = "ThreadPoolExecutor-1_1"`.

**Smoking-gun cross-reference:** PR #82 (`fix(celery): switch general worker pool from threads to prefork`, merged ~2026-04-25/26) and PR #84 (`drop general worker concurrency from 4 to 2 (OOM mitigation)`, merged today). Current prod compose command line for celery-worker is:

```
celery -A workers.celery_app worker -l info -c 2 -P prefork -Q default,...,pipeline.finalize -n general@%h
```

So the threading bug is **already fixed in code AND deployed**. The 817 count is *cumulative since 2026-04-08*; the issue has not fired since the prefork container started.

Three other "event loop already running" Sentry issues (114662654=376, 115133143=181, 114740961=3, 115231385=1, 115008598=1) are the same root cause grouped on different sub-stacks — all should also stop.

### STEP 4 — Worker UP-timeout (#115544899) analysis

Single event at **2026-04-27 10:18:10.645 UTC** on container `6900eafd17fc`. Logger: `celery.concurrency.asynpool`. Message: `Timed out waiting for UP message from <ForkProcess(ForkPoolWorker-3, started daemon)>`. This is the master prefork process saying a freshly-forked child never ack'd its readiness handshake.

Cross-reference with kernel log: `[Mon Apr 27 10:18:06 2026] Out of memory: Killed process 2507899 (celery)` on cgroup `docker-6900eafd...` — same container, **4 seconds earlier**. The fork child was OOM-killed before it could send UP. Issue #115544899 is the **observable face** of issue #115490790 (SIGKILL) on the worker-master side. Both extinct since 10:20 redeploy.

### STEP 5 — Deploy correlation

`git log` on prod, last 72 h (in deploy order, oldest at bottom):

```
6d05056  polish(research): i18n migration ... (#77)        ← current HEAD, deployed 11:49 +0330 (08:19 UTC)
0d14975  fix(celery): drop general worker concurrency 4→2  (#84)
99d45ad  docs: ... + deploy guide (#83)
8b738ae  fix(celery): switch general worker pool from threads to prefork (#82)
f74c89c  feat(pipeline): add review_lifecycle ... (#81)
5664f55  feat(pipeline): declare 10 per-step Celery queues (#80)
```

Container start time of all five service containers: **2026-04-27T10:20:30 UTC**. So today there were at least two attempted deploys (07:18-07:30 and 10:18-10:20), both during periods of OOM activity. The 10:20 deploy succeeded.

Mapping new Sentry issues to commits:
- **#82 (threads→prefork)** *eliminates* issue #110471755. ✅
- **#82 + #84 (prefork c=2 in 512 MiB cgroup)** *causes* issues #115490790 (SIGKILL) and #115544899 (UP-timeout). The new container shape is tight enough that boot/import memory + 2 forked children pushes past the cgroup limit when there is *also* concurrent host-level memory pressure from `docker buildx` of the new image.

### STEP 6 — Frontend top issue (#103452892, 230× spike, "Cannot read properties of null (reading 'message')")

- Runtime: **Node** (server-side), not browser.
- Culprit: `POST /` on `https://138.197.76.197/` (raw IP, not domain — bot/scanner traffic).
- Stack is entirely inside `next-server/app-page.runtime.prod.js` (no in-app frames).
- First seen 2026-03-15; last seen 2026-04-27 12:20 — ongoing background noise, not correlated time-wise with the backend OOM cascade.

Conclusion: **separate problem.** It is a Next.js SSR error path triggered by junk POSTs to the IP. Worth filing but not part of this incident.

### STEP 7 — Celery queue health

```
celery=2  default=0  stories=0
pipeline.orchestrator=0  pipeline.outline=0  pipeline.writing=0
pipeline.phonetics=0     pipeline.parent_guide=0
pipeline.images=0  pipeline.audio=0  pipeline.video=0  pipeline.pdf=0  pipeline.finalize=0
notifications=0  cleanup=0
```

`celery -A workers.celery_app inspect active` and `inspect reserved` on both general@ and stories@ workers: empty. **No backlog. Nothing pending. Nothing stuck.** The "celery=2" key is the Celery housekeeping default queue, not work in progress.

---

## Root cause hypothesis

**Confidence: HIGH** for both ranked hypotheses.

1. **Sentry issue #1 (#110471755, "event loop already running" × 817):** thread-pool concurrent access to the per-process persistent `asyncio` loop at `workers/story_tasks.py:25` (`loop.run_until_complete(coro)`). Already fixed by PRs #82 (prefork) and #84 (c=2). No new events for this fingerprint since the prefork container started.

2. **Sentry issues #2 + #3 (SIGKILL × 10 + UP-timeout × 1 + WorkerLostError × 1):** OOM-killer fired during the 07:18 and 10:18 deploy windows. Two flavours:
   - **Cgroup OOM** of `celery-worker`: the 512 MiB `mem_limit` is marginal for `prefork -c 2` on a Python image that imports Sentry SDK + Motor + Celery + the (lazy-loaded) pipeline modules. Each ForkPoolWorker drifts to ~135-140 MiB anon-rss; master + 2 children + page-cache pressure tips past 512 MiB during boot or under bursty load.
   - **Global system OOM** on the 4 GiB VPS while `docker buildx` is consuming ~1-2 GiB on top of the existing ~1.3 GiB committed runtime. Anything with a non-negative `oom_score_adj` is fair game; OOM-killer chose the largest uvicorn / celery process.

These two flavours co-occurred today only because PRs #82+#84+#77 were all deployed in the same window.

---

## Proposed fix (NOT to be applied — Saeid decides)

**Minimal change (addresses #2/#3):** Raise the `celery-worker` mem_limit from 512 MiB to **768 MiB** in `docker-compose.yml` (line 140 area).

```yaml
# docker-compose.yml — celery-worker service
deploy:
  resources:
    limits:
      memory: 768M     # was 512M; tight for prefork -c 2 + Python imports
```

- **Blast radius:** small. One container's cgroup ceiling moves up by 256 MiB. With the *other* services' steady-state ~1.0 GiB, host still has >2 GiB free at rest. Does not affect any code path; pure resource budgeting.
- **Rollback:** revert the line, redeploy the worker.
- **Precondition:** none. Queues are empty, no in-flight tasks. Standard `docker compose up -d --build celery-worker` (per project memory: "deploy rebuild ALL services" — must rebuild backend + frontend + celery-worker + celery-beat together, otherwise celery boots on stale code; build during a low-traffic window because the host hits global OOM under buildx).
- **Sentry hygiene after deploy:** mark issue #110471755 (817×) and the related event-loop fingerprints (#114662654, #115133143, #114740961, #115231385, #115008598) as Resolved. Mark #115490790, #115544899, #115492220 as Resolved-in-next-release. NightWatch will then re-rank tomorrow and the CRITICAL verdict will clear.

**Defer (separate ticket):** Move container builds OFF the prod VPS — build on devops.shahrzad.ai (this server), push to a registry, `docker compose pull && up -d` on prod. This is the real fix for the global-OOM-during-deploy class of incidents and is the largest single risk-reducer. Does not need to ship today.

---

## Alternative fixes considered

1. **Drop `celery-worker` concurrency 2 → 1.** Would fit easily in 512 MiB. Halves general-queue throughput, but general queue handles cleanup, notifications, beat-scheduled sweeps and the per-step pipeline queues — all low-rate. Acceptable, free, smaller diff than (a). Why I ranked the mem-bump above this: PR #84 just *moved* concurrency 4→2 explicitly. Going to 1 contradicts the design intent of having parallel beat sweepers + notification fan-out, and silently bottlenecks Phase 3+ pipeline experiments.

2. **Add swap to the host.** 1-2 GiB of swap would let global-OOM events grind instead of kill. Mitigates symptom but not cause; swap on a non-SSD VPS adds latency. Worth considering as defense-in-depth alongside the real fix (registry-based deploys).

3. **"Just restart the celery-worker container."** **WRONG** — and worth saying out loud per the consult warning. Evidence ruling this out:
   - There is *nothing* to restart. The container has been Up 9 hours, healthy, and the SIGKILL fingerprint has zero new events in that window.
   - Restarting WOULD trigger the same boot-time memory pressure that caused the original cgroup OOM (prefork forks + Python imports). On a host with only 310 MiB free + 1.3 GiB available, a redeploy now is the single most likely action to *reproduce* the incident.
   - The host's OOM-killer history (`global_oom × 16+ in 48 h`, all triggered by `docker-buildx` or `docker-compose`) is exactly the cycle the consult warned would reboot-loop us if we naively restarted.

4. **Increase `worker_concurrency` env back to 4 and rely on the cmd-line `-c 2` to override.** Would just be cosmetic — the env var is ignored when `-c` is passed. Not a fix.

---

## What's deferred

- **Frontend issue #103452892** (TypeError × 230): real but unrelated. Filing it as its own ticket: SSR error on POST / served from the raw IP. Cost of waiting: ongoing 230 events/window of noise inflating the frontend severity score, but no user impact (browser tag is bot UA `Chrome 60.0.3112` from 2017).
- **The other 134 backend issues** in the digest: not in the top-3 and not investigated. Cost of waiting: low. None crossed the threshold individually; they are background.
- **Move builds off prod (registry-based deploy):** the structural fix to the global-OOM-during-deploy pattern. Should land this sprint regardless of today's hot-fix outcome. Cost of waiting one cycle: another deploy *will* OOM the VPS — likely killing uvicorn or mongod again. Recovery has been clean so far, but each recovery is a Russian-roulette pull on whether Mongo decides the WiredTiger journal is dirty.
- **Add a NightWatch rule to deduplicate "RuntimeError: event loop is already running" across the 6+ Sentry fingerprints**: it inflates total backend issue count and the cumulative-count threshold. Not urgent.

---

## Surprises

1. **mongod was OOM-killed once** (Apr 27 07:29:21, anon-rss 152 MiB, global OOM). It restarted clean (`Up 11 hours` now), but the digest never surfaced this because mongo doesn't ship to Sentry. Worth a NightWatch enhancement: cross-reference kernel OOM events with Sentry issues to catch cases where the killed process isn't the one Sentry sees.
2. **The 817-count framing is misleading.** It is a *lifetime* count over 19 days, not a 24 h spike. NightWatch's CRITICAL verdict was driven by the cumulative threshold crossing, not by a fresh deterioration. The truly new things this cycle are the SIGKILL/UP-timeout/WorkerLost cluster — which are the deploy of *the very fix that resolves issue #1*. NightWatch saw symptoms of a fix and labelled it a CRITICAL.
3. **The 10:18 OOM cgroup is different from the 07:18 OOM cgroup**, meaning `celery-worker` survived/recovered between the two deploy attempts. The container that died at 10:18 was actually a healthy mid-life container — killed by the *next* deploy's `docker buildx`, not by its own resource ceiling.
4. **`celery-worker-stories` is on `-P threads -c 1`** (one thread, threads pool). It is NOT affected by the threading-loop bug because c=1 ⇒ no concurrent loop access. It also has 1.5 GiB headroom. Project memory confirms a deliberate 512M→1.5G bump on this container in the past — exactly the class of fix this report recommends for `celery-worker`.

---

## Appendix — commands run (read-only)

Locally on devops.shahrzad.ai:
- `ls /opt/sentry-nightwatch/snapshots/2026-04-26/`
- `cat summary.json | top_issues.json | analysis.md | evidence/110471755.json`
- `grep` Sentry env keys
- `Read /opt/shahrzad-devops/repos/ZigguratKids4/backend/workers/{story_tasks.py,celery_app.py}`
- `curl https://sentry.io/api/0/organizations/.../issues/{115490790,115544899,115492220,103452892}/events/latest/`

On prod (138.197.76.197) via SSH key:
- `uptime; free -h; nproc`
- `journalctl -k --since '48 hours ago'  | grep -i oom|kill`
- `dmesg -T | tail`
- `docker stats --no-stream`
- `docker inspect ... --format '{{.HostConfig.Memory}} ... {{.RestartCount}}'`
- `docker ps -a`
- `git log --since '72 hours ago' --oneline` in `/root/ZigguratKids4`
- `grep -nE 'mem_limit|memory:' docker-compose*.yml`
- `redis-cli LLEN <queue>` × 15 queues
- `celery -A workers.celery_app inspect active|reserved` on both workers

No write commands, no restarts, no kills, no compose up/down/restart, no git operations beyond `log`, no edits.
