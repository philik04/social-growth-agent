# Analytics (Phase 6)

Phase 6 collects **observed public metrics** of the posts this application published, at
fixed ages after each post (by default 1h, 24h and 72h), and joins every observation back
to the research, candidate, critique, approval and strategy that produced the post. It
does not interpret the numbers and does not change any strategy: that is Phase 7.

No LLM is involved anywhere here. Whether and when a snapshot is taken is decided by
the job table and a deterministic retry policy; reading metrics is a single HTTP call.

## What is read, with which credential

| | |
|---|---|
| Endpoint | `GET /2/tweets?ids=<up to 100 ids>&tweet.fields=created_at,public_metrics` |
| Credential | the app-only `X_BEARER_TOKEN` (the same read-only token as research). The publishing keys (`X_PUBLISH_*`) are never loaded for analytics |
| Fields stored | `like_count` → `likes`, `retweet_count` → `reposts`, `reply_count` → `replies`, `quote_count` → `quotes`, `bookmark_count` → `bookmarks`, `impression_count` → `impressions`, and `created_at` → `provider_created_at` |
| Scope | `metrics_scope = "public"` on every request and snapshot |
| Not requested | `non_public_metrics`, `organic_metrics`, `promoted_metrics` (user-context auth on your own posts, last 30 days), media `view_count` (we only post text) |

**Availability is an assumption until it is validated with our credentials.** X documents
`public_metrics` as readable with an app-only token, including `impression_count` and
`bookmark_count`; this project has not yet run a live read with its own token. The live
demo and the opt-in live test below exist to check exactly that. Any count X does not
return is stored as `NULL` (never `0`); a `0` is stored only when X returned `0`.
Derived rates are `NULL` when an input is `NULL` or when impressions are `0`.

The X docs name this parameter `post.fields` on some pages; the code sends
`tweet.fields`, the name the v2 endpoint accepts in practice (the research provider uses
the same convention). If X ever rejects it, the error is a `bad_request` and nothing is
retried automatically.

### Designed for private metrics later, not built now

The provider protocol (`SocialAnalyticsProvider`) carries a `metrics_scope` and returns
counts by name, so a second, user-context provider can later add private metrics
(`non_public_metrics`, `organic_metrics`) with its own credentials and its own
`metrics_scope` without changing the tables, the worker or the API. It is deliberately
not implemented in Phase 6.

## Timestamps: recorded time vs creation time

| Field | Meaning |
|---|---|
| `publications.published_at` | when **this application recorded** the post as published (worker commit, or a human resolve). It can be much later than the real post for a resolved `unknown` publication |
| `publications.provider_created_at` | X's own `created_at`, set the first time a read returns it; never inferred |
| `post_metrics.captured_at` | when this snapshot was actually read |
| `post_metrics.scheduled_for` | when it should have been read |

Jobs are planned from `provider_created_at` when it is known, otherwise from the
recorded `published_at`; each job stores its `schedule_basis` and `basis_at`.

**Reconciliation.** The first snapshot that returns a creation time does exactly this,
in the same transaction:

1. sets `publications.provider_created_at` if it is still `NULL` (never overwritten);
2. moves only jobs that are `scheduled`, not leased, and planned from the recorded time to
   `provider_created_at + age`, keeping the old time in `original_scheduled_for` and
   switching `schedule_basis` to `provider_created_at`.

It never creates, deletes or re-opens a job and never touches a collected snapshot or a
job another worker is collecting, so it cannot produce duplicates or lose observations.
Running it again changes nothing.

**Honest labels.** Every snapshot keeps its *target* label (`1h`) and also reports:
`capture_delay_seconds` (`captured_at - scheduled_for`), `actual_age_seconds` (from
`provider_created_at` when known, else from the recorded time, named in `age_basis`) and
`on_target` (actual age within `max(120 s, 10% of the target)`). A 1h snapshot taken
after a ten-minute rate limit says `on_target: false` and shows its real age; nothing
presents it as a one-hour measurement.

## State model

```
              publish / resolve-as-published / explicit backfill
                                  |
                                  v
   +---------------------->  scheduled  --(cancel: post not found)-->  cancelled
   |                             |
   | retryable failure           | due: scheduled_for <= now, retry_not_before <= now,
   | (retry_not_before set)      |      claimed with FOR UPDATE SKIP LOCKED + lease
   | or expired lease            v
   +------------------------  collecting  --(snapshot stored)-->  collected
                                 |
                                 | auth, bad_request, not_found, or attempts used up
                                 v
                               failed  --(POST /analytics/jobs/{id}/retry)-->  scheduled
```

Uniqueness: one job per (publication, age) and one snapshot per (publication, age), both
enforced by the database (`uq_analytics_jobs_target`, `uq_post_metrics_target`,
`uq_post_metrics_job_id`).

## One worker cycle

1. **Sweep:** `collecting` jobs whose lease expired go back to `scheduled` (after the
   backoff) or, out of attempts, to `failed`; their open attempt rows close as
   `failed/lease_expired`.
2. **Claim:** due jobs, ordered by `scheduled_for`, up to `ANALYTICS_BATCH_SIZE`, with
   `FOR UPDATE SKIP LOCKED`; set the lease.
3. **Begin (committed before any call):** jobs → `collecting`, one `analytics_requests`
   row (`started`) and one `analytics_attempts` row per job.
4. **One provider call** for the distinct post ids of the batch.
5. **Finish (one transaction):** close the request (`succeeded`, `partial` or `failed`),
   store a snapshot for each returned post, apply the retry policy to every other job;
   a `not_found` post also cancels its later `scheduled` jobs.

A metrics read has no side effect, so the only ambiguity publishing has (did it post?)
does not exist here. A worker that dies after step 3 leaves an unfinished request row
(visible forever) and `collecting` jobs; the sweep returns them and the read is simply
repeated. If a slow worker finishes after its job was swept and collected by another, it
stores nothing (`skipped`). The worker never creates jobs, so **starting it never
backfills anything**.

```bash
DATABASE_URL=postgresql://... uv run sga-analytics-worker            # poll forever
uv run sga-analytics-worker --once                                   # one cycle
uv run sga-analytics-worker --dry-run                                # count due jobs; reads nothing
uv run sga-analytics-worker backfill --publication pub_...           # explicit, idempotent
uv run sga-analytics-worker backfill --all-published --dry-run
```

## Retry policy

`delay(attempt) = min(ANALYTICS_BACKOFF_BASE_SECONDS * 2^(attempt-1), ANALYTICS_BACKOFF_MAX_SECONDS)`
(60 s, 120 s, 240 s, … up to 1 h), persisted as `retry_not_before`. Nothing sleeps.

| Failure | Category | Automatic | Manual retry |
|---|---|---|---|
| 401 / 403 | `auth` | no, `failed` | yes, after fixing the token |
| 429 with reset | `rate_limited` | yes, at `max(backoff, reset)` | when attempts are used up |
| 429 without reset | `rate_limited` | yes, backoff (a read is idempotent) | when attempts are used up |
| timeout | `timeout` | yes, backoff | when attempts are used up |
| network error, 5xx | `transient_server` | yes, backoff | when attempts are used up |
| 400 (or >100 ids) | `bad_request` | no, `failed` | no (409) |
| post deleted, protected or suspended | `not_found` | no, `failed`; later jobs `cancelled` | no (409) |
| post missing from a 200 response, bad JSON or shape | `malformed_response` | yes, backoff | when attempts are used up |
| unexpected per-post error, other status, provider bug | `unknown` | yes, backoff | when attempts are used up |
| worker died mid-collection | `lease_expired` | yes, backoff | when attempts are used up |

Automatic attempts are bounded by `ANALYTICS_MAX_ATTEMPTS` (default 5). A manual retry
sets `attempt_base = attempt_count`, granting a fresh automatic budget while
`attempt_count` keeps the full history.

## API

| Method | Path | |
|---|---|---|
| GET | `/publications/{id}/metrics` | snapshots ordered by target age, with timing fields and observed derived rates |
| GET | `/publications/{id}/lineage` | publication → candidate (critiques) → revision chain → approval → findings → cited source posts → run strategy and account → metrics |
| POST | `/publications/{id}/analytics/backfill?dry_run=` | create missing jobs (202); idempotent; 409 unless published; reads nothing |
| GET | `/posts?since=&until=&account_id=&status=&limit=` | published posts, newest first, with their latest snapshot; `status` = a job status; datetimes need a timezone |
| GET | `/analytics/jobs?status=&publication_id=&limit=` | jobs with their attempts |
| GET | `/analytics/jobs/{id}` | one job |
| POST | `/analytics/jobs/{id}/retry` | `failed` with a retryable category → `scheduled`; otherwise 409 |
| GET | `/publications/{id}`, `/runs/{id}` | publications now carry `provider_created_at` and an `analytics` summary |
| GET | `/runs/{id}/usage` | gains `usage.analytics` and `analytics_post_reads` cost lines |

No endpoint calls a platform. Raw provider JSON is never stored or returned.

## Usage and cost

Usage per run (`usage.analytics`): `requests` (distinct requests that touched the run's
posts; a batch spanning runs counts for each, so not additive), `post_reads` (posts X
returned to a job of this run), `snapshots`, `failed_requests`, `unfinished_requests`.

Cost lines price `post_reads × x.post_read` from `pricing.toml` (same unit as research
reads), under both `at_run_pricing` and `at_current_pricing`. Prices are blank by
default and never in code. Per X's pricing page at the time of writing: a post read is
listed at $0.005, and reads are deduplicated within a UTC day (a "soft guarantee"), so
estimates that count every read are an **upper bound**. X also lists a lower "owned
reads" rate for some reads of your own data; whether metric lookups by id qualify has
**not** been verified against billing, so it is not modelled. With the defaults, three
snapshots per post are about three post reads (≈ $0.015 per post at $0.005), before
deduplication and excluding retries of failed requests (which return no posts).

## Lineage

No lineage is copied into the analytics tables; the foreign keys carry it:
`post_metrics → analytics_jobs → publications → content_candidates → critiques /
candidate_findings → research_findings → finding_evidence → source_posts`, plus the
run's strategy snapshot (`runs.strategy`, `strategy_id`, `strategy_version`) and the
approving `review_decisions` row. `GET /publications/{id}/lineage` assembles the chain;
Phase 7 builds on `persistence.lineage.publication_lineage`.

## Configuration

```
ANALYTICS_PROVIDER=mock               # mock | x; never a silent fallback
ANALYTICS_SNAPSHOT_AGES=1h,24h,72h    # 1m..30d, at most 10, no duplicates
ANALYTICS_ENQUEUE_ON_PUBLISH=true
ANALYTICS_POLL_SECONDS=30
ANALYTICS_LEASE_SECONDS=60            # at least 2 x X_TIMEOUT_SECONDS + 5
ANALYTICS_MAX_ATTEMPTS=5
ANALYTICS_BATCH_SIZE=50               # 1..100 (X's limit per lookup)
ANALYTICS_BACKOFF_BASE_SECONDS=60
ANALYTICS_BACKOFF_MAX_SECONDS=3600
RUN_LIVE_X_ANALYTICS_TESTS=0
```

## Demos

Offline and free (mock everything; the clock is stepped, nothing sleeps):

```bash
DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.analytics_demo
```

It prints the worker trace and the retry/backoff trace: publishing creates jobs; a
worker started early reads nothing; a rate limit waits for its reset; a deleted post
fails and cancels its later jobs; the creation time reschedules waiting jobs; a timeout
backs off; a worker dies mid-read and another recovers it with one snapshot.

Live, opt-in and read-only (one `GET` for one post **this application published**):

```bash
RUN_LIVE_X_ANALYTICS_TESTS=1 X_BEARER_TOKEN=... DATABASE_URL=postgresql://... \
  uv run python -m social_growth_agent.services.x_analytics_demo <post-id> [--collect-due]
```

It refuses unless the gate is set, the database is at the current revision, and the
post id belongs to a `published` publication in that database. By default it is a probe:
the read is recorded in `analytics_requests`, the normalized counts (and which ones X
did not return) are printed, and no snapshot or job changes. `--collect-due` instead runs
the normal worker path for that publication's due jobs only. The optional live test
(`RUN_LIVE_X_ANALYTICS_TESTS=1 X_BEARER_TOKEN=... X_ANALYTICS_LIVE_POST_ID=<id> uv run
pytest -m live tests/live/test_x_analytics_live.py`) makes the same single read without a
database.
