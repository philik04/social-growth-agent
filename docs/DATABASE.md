# Database

Phase 4 persists runs in PostgreSQL. This page covers setup, the schema, how resume works,
and how to run the database tests.

## Setup

```bash
docker compose up -d                                   # PostgreSQL 16 on 127.0.0.1:5433
export DATABASE_URL=postgresql://sga@localhost:5433/sga
uv run sga-db upgrade                                  # Alembic migrations + checkpoint tables
uv run sga-db current                                  # applied revision and table list
uv run uvicorn social_growth_agent.api.app:create_app --factory --port 8000
```

`sga-db upgrade` is idempotent. It runs the Alembic migrations for the domain tables, then
LangGraph's `PostgresSaver.setup()` for the checkpoint tables (LangGraph owns and migrates
those; Alembic ignores them). `sga-db reset --yes` drops and recreates everything, and refuses
any database that is not on localhost.

`DATABASE_URL` accepts `postgresql://` (or `postgresql+psycopg://`). It is held as a
`SecretStr`: messages and logs show only host, port and database, never the user or password.
Without `DATABASE_URL` the API still starts; `/health` reports `"database": "not_configured"`
and run endpoints return 503.

## Two stores

| Store | Written by | Purpose |
|---|---|---|
| `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` | LangGraph `PostgresSaver` after every step (`durability="sync"`) | Source of truth for execution: what a run resumes with |
| Domain tables below | `RunRecorder` (projection of graph state after every step) and `UsageLedger` (at call time) | Queryable record for the API, audits and later analytics |

Checkpoints use the allowlisted serializer from Phase 1: only our own domain and policy types
can be deserialized. Domain rows are immutable records keyed by their ids and inserted with
`ON CONFLICT DO NOTHING`, so the same state can be projected any number of times.

## Schema (revision 0003)

All child tables reference `runs.id` with `ON DELETE CASCADE`.

| Table | Key columns | Notes |
|---|---|---|
| `pricing_versions` | `id` (`<version>:<sha256[:12]>`), `version`, `as_of`, `currency`, `content` (JSONB) | Immutable price list snapshots |
| `runs` | `id`, `status`, `current_node`, `research_query`, `account`/`strategy`/`config` (JSONB snapshots), `strategy_id`, `strategy_version`, attempt counters, `edit_rounds`, `review_candidate_ids`, `failure_*`, `pricing_id` → `pricing_versions`, `created_at`, `updated_at` | Status: `queued`, `running`, `awaiting_review`, `approved`, `rejected`, `failed`, `stalled` |
| `research_fetches` | `id`, `run_id`, provider, query, effective query, `requests_made`, `posts_fetched`, `users_fetched`, outcome, `error_category`, rate-limit fields | Usage ledger; one row per retrieval attempt, including failures |
| `llm_calls` | `id`, `run_id`, agent, task, provider, model, attempt, outcome, latency, `input_tokens`, `output_tokens`, `error_type` | Usage ledger; no prompt or output text is stored |
| `source_posts` | PK (`run_id`, `source_id`) | Retrieved posts; metrics are NULL when the platform did not return them |
| `research_briefs` | `id`, `run_id`, topic, summary, confidence, limitations, opportunities, `source` (JSONB) | |
| `research_findings` | `id`, `run_id`, `brief_id`, theme, summary, `claim_type`, `signal_strength`, `position` | |
| `finding_evidence` | PK (`finding_id`, `source_id`); FK (`run_id`, `source_id`) → `source_posts` | Evidence can only cite a post the same run retrieved |
| `content_candidates` | `id`, `run_id`, attempt, `origin`, content, hook, format, `revises_candidate_id` → self, strategy id/version | |
| `candidate_findings` | (`candidate_id`, `finding_id`) | Which findings a candidate is based on |
| `critiques` | `id`, `run_id`, `candidate_id`, attempt, `verdict`, `recommended_verdict`, score, risks, `issues`, `policy_violations`, `suggested_revision` | Every critique is kept, including those of earlier cycles |
| `review_decisions` | `id`, `run_id`, `action`, `candidate_id`, `resulting_candidate_id` (edit), `edited_content`, `reviewed_candidate_ids`, `note`, `reviewer`, `decided_at` | `reviewed_candidate_ids` (0002) is the review request the decision answered; empty for decisions recorded before Phase 5 |
| `run_events` | `id`, `run_id`, node, outcome, attempt, duration | One row per node execution |
| `run_errors` | `id`, `run_id`, node, `error_type`, message | |
| `publications` (0002) | `id`, `run_id` → `runs`, (`run_id`, `candidate_id`) → `content_candidates` (`run_id`, `id`), `platform`, `idempotency_key` UNIQUE, UNIQUE (`run_id`, `candidate_id`, `platform`), `content`, `content_sha256`, `status` (CHECK), `scheduled_for`, `requested_by`, `claimed_by`, `claimed_at`, `lease_expires_at`, `attempt_count`, `provider`, `provider_post_id`, `provider_post_url`, `started_at`, `published_at`, `failure_category`, `failure_message`, `rate_limit_reset_at`, `retry_not_before` (0003), `resolved_by`, `resolution_note` | One durable publication intent; status: `scheduled`, `ready`, `publishing`, `published`, `failed`, `unknown`, `cancelled`. **No** `ON DELETE CASCADE`: the record of an external side effect must not vanish with its run |
| `publication_attempts` (0002) | `id`, `publication_id`, UNIQUE (`publication_id`, `attempt`), `worker_id`, `provider`, `started_at`, `finished_at`, `latency_ms`, `outcome`, `http_status`, `failure_category`, `failure_message` (sanitized), `rate_limit_reset_at`, `provider_post_id` | Started ledger: committed before the platform call. `outcome='started'` with no `finished_at` means the process died mid-call |
| `provider_operations` (0002) | `id`, `run_id`, `node`, `provider`, `operation`, `generation_attempt`, `started_at`, `finished_at`, `outcome`, `error_type`, `latency_ms`, `usage_records` | Started ledger for every provider-calling node attempt; unfinished rows carry no invented usage |

The provenance chain is a plain join:
`content_candidates → critiques`, `content_candidates → candidate_findings → research_findings
→ finding_evidence → source_posts`, `review_decisions → content_candidates`, and
`publications → content_candidates` (through a composite FK, so a publication's candidate
always belongs to its run).

Migrations: `0001` is never modified. `0002` adds the three publishing tables, the decision
snapshot column and `UNIQUE (content_candidates.run_id, id)`. `0003` adds
`publications.retry_not_before`, the gate a rate-limited row waits behind until its reset
(NULL for existing rows). Both apply to an existing Phase 4
database and to a fresh one (`sga-db upgrade`). A DB test upgrades a populated `0001` database
and checks the existing rows survive, and another compares the migrated schema against the
SQLAlchemy models.

## Resume

- Every graph step is checkpointed before the next one starts, so a completed X retrieval or
  generation is never lost or repeated.
- At startup the API calls `recover_stalled()`: runs left `queued` or `running` by a dead
  process become `stalled`. **Nothing else happens; no provider is called.**
- `POST /runs/{id}/resume` (only for `stalled` runs) continues from the last checkpoint. If the
  run was paused for review, it simply returns to `awaiting_review`. If the process died before
  the first checkpoint, the run starts from its stored input snapshots.
- Runs paused for review are not affected by restarts: they stay `awaiting_review` and accept a
  decision from any later process.
- `POST /runs/{id}/review` locks the run row, checks the decision against the checkpoint, sets
  the run to `running`, then resumes on a worker thread. A second decision for the same pause
  gets 409.

## Costs

Usage counts (requests, post reads, user reads, tokens per model) are canonical and come from
the ledger tables. Prices live only in `pricing.toml` (path in `PRICING_FILE`). The shipped file
leaves every price blank on purpose: fill in current values from the providers' pricing pages
and bump `version`/`as_of` when you change them. Missing prices are reported in
`missing_prices` and make the total `null`; usage from `unbilled_providers` (the mock fixtures
and fake agents) costs 0.

When a run is created, the current price list is stored in `pricing_versions` and referenced
by `runs.pricing_id`. `GET /runs/{id}/usage` returns:

- `at_run_pricing`: the estimate with the price list stored for that run (reproducible later);
- `at_current_pricing`: the same counts re-estimated with today's price list.

Both carry `estimated: true` and the price list's `id`, `version`, `as_of` and `currency`.

## Tests

Tests that need PostgreSQL live in `tests/db/` and carry the `db` marker.

```bash
uv run pytest                                   # everything; DB tests skip without TEST_DATABASE_URL
TEST_DATABASE_URL=postgresql://sga@localhost:5433/sga uv run pytest          # full suite
TEST_DATABASE_URL=postgresql://sga@localhost:5433/sga uv run pytest -m db    # only DB tests
uv run pytest -m "not db"                       # only tests that need no database
```

The DB fixtures create a throwaway `sga_test_<hex>` database on that server (the user needs
`CREATEDB`), migrate it, truncate tables between tests, and drop it afterwards. Skips always
state the reason. When `CI` is set and `TEST_DATABASE_URL` is missing, the DB tests **fail**
instead of skipping, and the GitHub Actions workflow provides a PostgreSQL service.

## Restart demo

```bash
DATABASE_URL=postgresql://sga@localhost:5433/sga \
  uv run python -m social_growth_agent.services.restart_demo
```

Three separate processes on one database, offline and free (mock research, fake agents):
process 1 is killed (`os._exit`) during the research LLM call after retrieval completed;
process 2 starts, flags the run stalled without calling anything, resumes it and shows the
retrieval ledger still holds exactly one fetch; process 3 approves a candidate.
