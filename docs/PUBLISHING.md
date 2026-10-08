# Publishing (Phase 5)

Approved content reaches X through a separate, explicit request, a durable publication
intent and a worker that makes exactly one platform call per attempt. Approval is
content approval only: it never posts anything.

## Two different kinds of X credentials

| | Research (Phase 3) | Publishing (Phase 5) |
|---|---|---|
| Setting | `X_BEARER_TOKEN` | `X_PUBLISH_API_KEY`, `X_PUBLISH_API_SECRET`, `X_PUBLISH_ACCESS_TOKEN`, `X_PUBLISH_ACCESS_TOKEN_SECRET` |
| Auth scheme | app-only bearer (OAuth 2.0 client credentials) | OAuth 1.0a, user context (HMAC-SHA1 request signing) |
| What it can do | `GET /2/tweets/search/recent` | `POST /2/tweets` |
| Client | `XApiClient` (GET only) | `XUserClient` inside `XPublisher` (POST `/2/tweets` only) |

An app-only bearer token **cannot** post: X requires user context for a post create.
The two sets of credentials are different settings, read by different factories
(`build_research_provider`, `build_publisher`) into different client objects, so the
research client can never post and the publish client can never read.

Getting the four publishing values: in the X developer portal, set the app's user
authentication to **Read and write**, then generate the app's API key/secret and the
access token/secret **for the account that should post**. Regenerate the access token
after changing permissions, or it keeps the old (read-only) scope.

Why OAuth 1.0a rather than OAuth 2.0 PKCE: for a single account you own it is the
minimum that can post. The four values never expire, so nothing secret ever has to be
written anywhere; signing is about 100 lines on the standard library
(`providers/x/oauth1.py`), tested against X's own documented signature example. OAuth
2.0 PKCE needs a browser consent flow and a rotating refresh token that must be
persisted, which is work for the multi-tenant phase, not for one account.

### Where secrets may appear

Only in the environment, in `AppSettings` as `SecretStr`, and inside the publisher while
it signs one request. They are never in graph state, checkpoints, database rows, LLM
prompts, API responses, logs or exceptions. Exceptions are raised outside the `httpx`
`except` blocks, so the signed request (which carries the `Authorization` header) is
never chained onto an error. `.env.example` holds placeholders only, and a DB test scans
every table, every checkpoint blob, every API response and the log output for all four
values.

## Lifecycle

```
POST /runs/{id}/publish  (policy checks, one durable intent)
        |
   scheduled_for in the future? ----yes----> scheduled --(due)--+
        |no                                                     |
        v                                                        v
      ready  <---------------------------------------------- (worker claims, lease)
        |
        v
   publishing  --(2xx with a post id)------------------------> published
        |
        +--(definite 4xx, or 429 without a reset time)------->  failed
        +--(connection failed before sending, attempts left)->  ready
        +--(429 with a reset time, attempts left)------------>  ready, not claimable
        |                                                       before retry_not_before
        +--(timeout after sending, 5xx, no post id in a 2xx,
        |   or the worker died while the lease was held)----->  unknown
        |
  failed   --POST /publications/{id}/retry (retryable category only)--> ready
  unknown  --POST /publications/{id}/resolve {published,  post id}----> published
           --POST /publications/{id}/resolve {not_published}----------> ready
  scheduled|ready --POST /publications/{id}/cancel (unclaimed only)---> cancelled
```

Publication status is separate from run status: the run stays `approved` the whole time.
The absence of a row means publishing was never requested.

## What the deterministic policy requires

`policies/publishing.py` decides from persisted facts only; no LLM is involved. All of
these must hold, when the intent is created **and** again in the worker immediately
before the call:

1. the run exists and is `approved`;
2. the candidate exists and belongs to the run (also a composite foreign key);
3. the run has exactly one approve decision and it names this candidate;
4. the candidate was in the review request that approval answered
   (`review_decisions.reviewed_candidate_ids`; empty for decisions recorded before
   Phase 5, which were validated against the pending review when they were made);
5. the candidate's latest critique passed;
6. the run's own hard content policy still passes on the exact content;
7. no publication for (run, candidate, platform) exists in any state but `cancelled`.

The worker additionally checks that the candidate's content still hashes to the
`content_sha256` stored on the intent, so an edited candidate can never be published
under an older intent.

## Idempotency, and the limits of exactly-once

- `idempotency_key = sha256("x:<run_id>:<candidate_id>")`, `UNIQUE`. `(run_id,
  candidate_id, platform)` is `UNIQUE` too.
- The intent is inserted with `ON CONFLICT DO NOTHING`, so a duplicate (or concurrent)
  request creates nothing and the API answers 409 with the existing publication's id and
  status.
- Retries, a manual retry and a resolve all reuse the same row: `attempt_count` grows,
  a new `publication_attempts` row is written, and no second intent can exist.
- A cancelled intent keeps the key: requesting again revives that row rather than
  creating a second one.

**X API v2 `POST /2/tweets` has no idempotency key** and no way to look up a post by a
client-side key, so true exactly-once delivery is not available and this design does not
claim it. What is guaranteed:

| Situation | Guarantee |
|---|---|
| Duplicate publish requests, concurrent or days apart | at most one intent, so at most one publishing pipeline |
| A crash before the call | the row is still `ready`; it is claimed again and published once |
| A failure proven to have happened before the request was sent (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) | retried, bounded by `PUBLISH_MAX_ATTEMPTS` |
| A definite 429 with a reset time | retried after the reset and never before it, bounded by `PUBLISH_MAX_ATTEMPTS` |
| A published publication | never called again, by any worker or endpoint |
| Sent, but no definite answer (read/write timeout, dropped connection, 5xx, a 2xx without a post id, the worker dying while publishing) | `unknown`: **never** retried automatically |

The remaining risk is entirely in the `unknown` state: the only way to double-post is a
human resolving an `unknown` as `not_published` when the post in fact exists. That is
why resolving is explicit, requires a reviewer name, and is recorded on the row
(`resolved_by`, `resolution_note`).

A useful backstop, not a guarantee: X refuses identical text from the same account
within a window, returning 403 with a duplicate-content detail. The publisher maps that
to `failed` with category `duplicate_content` (distinct from auth 403). If it follows an
`unknown`, that is strong evidence the earlier attempt landed. It is not documented as a
guarantee, so nothing relies on it.

### The ambiguity window

```
commit: status=publishing, attempt_count+1, attempt row (outcome=started)
   |  <-- a crash anywhere in here leaves status=publishing with an unfinished attempt
POST /2/tweets
   |  <-- and a crash here too
commit: attempt finished; status=published | failed | unknown
```

A worker that finds a `publishing` row whose lease has expired moves it to `unknown`,
never back to `ready`, and marks its unfinished attempt `unknown`. `PUBLISHER_LEASE_SECONDS`
is validated to be at least `2 x X_PUBLISH_TIMEOUT_SECONDS + 5`, so a live worker's
in-flight call is never swept.

## Retry classification

| Outcome | Category | Status | Automatic retry | Manual retry |
|---|---|---|---|---|
| 2xx with a post id | — | `published` | — | refused (409) |
| 401 / 403 permission | `auth` | `failed` | no | yes, after fixing the credentials |
| 403 duplicate content | `duplicate_content` | `failed` | no | no; resolve instead |
| 400 and other definite 4xx | `bad_request` | `failed` | no | no |
| 429 with a reset time, attempts left | `rate_limited` (+ reset time) | `ready` with `retry_not_before` = reset | yes, after the reset, up to `PUBLISH_MAX_ATTEMPTS`; nothing sleeps | not needed (it is not `failed`) |
| 429 with a reset time, attempts used up | `rate_limited` (+ reset time) | `failed` | no | yes, once the reset has passed |
| 429 without a reset time | `rate_limited` | `failed` | no | yes |
| connection failed before sending | `not_sent` | `ready` | yes, up to `PUBLISH_MAX_ATTEMPTS` | yes |
| read/write timeout after sending | `timeout` | `unknown` | **never** | no; resolve only |
| 5xx | `server_error` | `unknown` | **never** | no; resolve only |
| 2xx without a post id, or a non-JSON body | `malformed_response` | `unknown` | **never** | no; resolve only |
| the worker died while publishing | `lease_expired` | `unknown` | **never** | no; resolve only |
| the policy refused at call time | `policy` | `failed` | no | yes, after fixing the cause |

Nothing ever sleeps waiting for a rate-limit reset. A 429 is a definite rejection (X did
not create the post), so retrying it cannot double-post; the reset time comes from
`x-rate-limit-reset` / `x-user-limit-24hour-reset` (the later one). The row goes back to
`ready` with `retry_not_before` set to that time, keeps `failure_category = rate_limited`
and the reset in `rate_limit_reset_at`, and the normal claim query skips it until the
reset has passed. `retry_not_before` is cleared on the next attempt and by every other
outcome. A 429 without a usable reset header stays `failed` for a manual retry, because
there is no safe time to retry it at. `auth` stays manual-only and `unknown` is never
retried automatically.

## Scheduling and the worker

`scheduled_for` must be an ISO-8601 datetime **with a timezone** (a naive one is 422),
is stored as `timestamptz` in UTC, may be at most `PUBLISH_MAX_SCHEDULE_DAYS` (30) ahead
and at most `PUBLISH_PAST_TOLERANCE_SECONDS` (5 minutes) in the past. A slightly past or
absent value means publish now (`ready`, with `scheduled_for` cleared).

One worker cycle:

1. sweep `publishing` rows whose lease expired into `unknown`;
2. claim due `ready`/`scheduled` rows:
   `SELECT id ... WHERE status IN ('ready','scheduled') AND COALESCE(scheduled_for,
   created_at) <= now() AND (lease_expires_at IS NULL OR lease_expires_at < now())
   AND (retry_not_before IS NULL OR retry_not_before <= now())
   ORDER BY COALESCE(scheduled_for, created_at) LIMIT n FOR UPDATE SKIP LOCKED`,
   then set `claimed_by`, `claimed_at` and `lease_expires_at = now() + lease`;
3. per row: re-check the policy and the content hash, commit `publishing` plus the
   started attempt, call the publisher, commit the outcome.

`FOR UPDATE SKIP LOCKED` is why two workers never claim the same row: the second skips
what the first has locked, and an unexpired lease held by another worker is skipped too.
Step 3's transition is conditional on the row still being claimed by this worker, so a
lost lease or a cancellation between claim and call produces no platform call. There is
no Celery, Redis or Kafka: PostgreSQL is the queue.

Running the worker:

```bash
DATABASE_URL=postgresql://... uv run sga-publisher-worker            # poll forever
uv run sga-publisher-worker --once                                   # one cycle
uv run sga-publisher-worker --dry-run                                # report due work only
```

It stops gracefully on SIGINT/SIGTERM after the current cycle. `PUBLISHER_EMBEDDED_WORKER=true`
additionally runs a worker thread inside the API process; it is **off by default**,
because starting or restarting the API should not by itself trigger queued external
side effects. The standalone worker is the canonical publishing process.

## Configuration

```
PUBLISHER_PROVIDER=mock                # mock | x  (mock posts nowhere; never a silent fallback)
X_PUBLISH_API_KEY=                     # app consumer key (publishing only)
X_PUBLISH_API_SECRET=
X_PUBLISH_ACCESS_TOKEN=                # access token of the posting account (Read and write)
X_PUBLISH_ACCESS_TOKEN_SECRET=
X_PUBLISH_TIMEOUT_SECONDS=10
PUBLISHER_POLL_SECONDS=5
PUBLISHER_LEASE_SECONDS=60             # at least 2 x the publish timeout + 5
PUBLISH_MAX_ATTEMPTS=3                 # bounds not-sent retries and post-reset 429 retries
PUBLISHER_EMBEDDED_WORKER=false        # local development only
PUBLISH_MAX_SCHEDULE_DAYS=30
PUBLISH_PAST_TOLERANCE_SECONDS=300
RUN_LIVE_X_PUBLISH_TESTS=0             # both gates are needed to post for real
CONFIRM_LIVE_X_PUBLISH=
```

## API

| Method | Path | |
|---|---|---|
| POST | `/runs/{run_id}/publish` | body `{candidate_id, scheduled_for?, requested_by?}` → **202** with the publication; 404 unknown run or candidate; 409 not publishable or already requested (with `publication_id` and `status`); 422 an invalid or out-of-bounds schedule |
| GET | `/publications/{id}` | status, schedule, lease times, sanitized attempts, post id and URL |
| GET | `/publications?status=&run_id=&limit=` | list, newest first |
| POST | `/publications/{id}/retry` | `failed` with a retryable category → `ready`; otherwise 409 |
| POST | `/publications/{id}/resolve` | `unknown` only: `{outcome: "published", provider_post_id, reviewer}` or `{outcome: "not_published", reviewer}` |
| POST | `/publications/{id}/cancel` | unclaimed `scheduled`/`ready` → `cancelled`; otherwise 409 |
| GET | `/runs/{id}` | now includes `publications` |

No endpoint ever calls a platform, and no raw provider response is exposed. Failure
messages carry the HTTP status, the category and X's short error title only.

## Local setup

```bash
# 1. a database
docker compose up -d
uv run sga-db upgrade

# 2. the API (publishing requested here, never performed here)
DATABASE_URL=postgresql://sga@localhost:5433/sga uv run uvicorn \
  social_growth_agent.api.app:create_app --factory --port 8000

# 3. a worker in another terminal (PUBLISHER_PROVIDER=mock posts nowhere)
DATABASE_URL=postgresql://sga@localhost:5433/sga uv run sga-publisher-worker

# 4. the whole lifecycle, offline and free, in one command
DATABASE_URL=postgresql://sga@localhost:5433/sga \
  uv run python -m social_growth_agent.services.publish_demo
```

`curl` walkthrough, once a run is approved:

```bash
curl -s -X POST localhost:8000/runs/$RUN/publish \
  -H 'content-type: application/json' \
  -d '{"candidate_id":"'"$CAND"'","requested_by":"philipp"}'      # 202, status "ready"
curl -s localhost:8000/publications?run_id=$RUN
uv run sga-publisher-worker --once                                 # the only call to X
curl -s localhost:8000/publications/$PUB                           # published + post url
```

## Live publishing safety

Publishing for real is triple gated and never happens by accident:

1. `RUN_LIVE_X_PUBLISH_TESTS=1`;
2. `CONFIRM_LIVE_X_PUBLISH=YES`;
3. for the demo, a typed `publish` on the terminal after the exact text is printed.

```bash
RUN_LIVE_X_PUBLISH_TESTS=1 CONFIRM_LIVE_X_PUBLISH=YES PUBLISHER_PROVIDER=x \
  DATABASE_URL=postgresql://... \
  uv run python -m social_growth_agent.services.x_publish_demo
```

It creates **one** harmless, timestamped post ("Testing an approval-gated publishing
pipeline (automated test post, …)"), prints its id and URL, and then shows that a second
request for the same candidate is refused with 409. Ordinary tests never publish: the
normal suite runs `-m 'not live'`, the autouse `offline` fixture blocks real HTTP
transports, and `PUBLISHER_PROVIDER` defaults to `mock`. Nothing is ever deleted
afterwards, and nothing posts merely because credentials are configured.

## Deliberately not here

- No analytics or metrics collection (Phase 6).
- No timeline reconciliation of `unknown` publications; resolving is manual.
- No threads, media, replies or quote posts: `POST /2/tweets` is called with `{"text":
  ...}` and nothing else.
- No multi-account publishing, and so no OAuth 2.0 PKCE token store.
