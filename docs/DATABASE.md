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

## Schema (revision 0007)

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
| `publications` (0002) | `id`, `run_id` → `runs`, (`run_id`, `candidate_id`) → `content_candidates` (`run_id`, `id`), `platform`, `idempotency_key` UNIQUE, UNIQUE (`run_id`, `candidate_id`, `platform`), `content`, `content_sha256`, `status` (CHECK), `scheduled_for`, `requested_by`, `claimed_by`, `claimed_at`, `lease_expires_at`, `attempt_count`, `provider`, `provider_post_id`, `provider_post_url`, `started_at`, `published_at`, `provider_created_at` (0004), `failure_category`, `failure_message`, `rate_limit_reset_at`, `retry_not_before` (0003), `resolved_by`, `resolution_note` | One durable publication intent; status: `scheduled`, `ready`, `publishing`, `published`, `failed`, `unknown`, `cancelled`. **No** `ON DELETE CASCADE`: the record of an external side effect must not vanish with its run |
| `publication_attempts` (0002) | `id`, `publication_id`, UNIQUE (`publication_id`, `attempt`), `worker_id`, `provider`, `started_at`, `finished_at`, `latency_ms`, `outcome`, `http_status`, `failure_category`, `failure_message` (sanitized), `rate_limit_reset_at`, `provider_post_id` | Started ledger: committed before the platform call. `outcome='started'` with no `finished_at` means the process died mid-call |
| `provider_operations` (0002) | `id`, `run_id`, `node`, `provider`, `operation`, `generation_attempt`, `started_at`, `finished_at`, `outcome`, `error_type`, `latency_ms`, `usage_records` | Started ledger for every provider-calling node attempt; unfinished rows carry no invented usage |
| `analytics_jobs` (0004) | `id`, `publication_id` → `publications`, UNIQUE (`publication_id`, `snapshot_age`), `age_seconds`, `schedule_basis` (`provider_created_at` \| `recorded_published_at`), `basis_at`, `scheduled_for`, `original_scheduled_for`, `origin` (`publish` \| `backfill`), `status` (CHECK), lease columns, `attempt_count`, `attempt_base`, `retry_not_before`, `failure_category`, `failure_message`, `rate_limit_reset_at`, `collected_at` | One planned snapshot; status: `scheduled`, `collecting`, `collected`, `failed`, `cancelled`. Index on (`status`, `scheduled_for`) for the claim query |
| `analytics_requests` (0004) | `id`, `worker_id`, `provider`, `metrics_scope`, `post_ids` (JSON), `started_at`, `finished_at`, `outcome` (`started`, `succeeded`, `partial`, `failed`), `http_status`, `error_category`, `error_message`, `posts_returned`, rate-limit fields, `latency_ms` | Started ledger for every metrics read, committed before the call; may cover posts of several runs |
| `analytics_attempts` (0004) | `id`, `request_id`, `job_id`, UNIQUE (`job_id`, `attempt`), `publication_id`, `provider_post_id`, `attempt`, `outcome` (`started`, `collected`, `not_found`, `failed`), `failure_category`, `started_at`, `finished_at` | One job's part in one request |
| `strategy_versions` (0005) | `id`, `strategy_id`, UNIQUE (`strategy_id`, `version`), `account_id`, `content` (JSONB), `content_sha256`, `parent_version_id` → self, `origin` (`registered`, `imported_from_run`, `proposal`, `experiment` (0006)), `created_by`, `auth_method`, `note` | Immutable strategy versions |
| `strategy_activations` (0005) | `id`, `account_id`, `seq`, UNIQUE (`account_id`, `seq`), `strategy_version_id`, `previous_version_id` → `strategy_versions`, `activated_by`, `auth_method`, `note`, `activated_at` | Append-only history; the active version is the highest `seq`. The unique `seq` makes concurrent activations compare-and-set |
| `performance_evaluations` (0005) | `id`, `account_id`, `strategy_id`, `snapshot_age`, `published_since`/`until`, `data_cutoff`, `synthetic`, `config` (JSONB), `config_version`, `request` (JSONB), `input_digest` UNIQUE, `digest`, `status`, counts, `summary` (JSONB), `duration_ms` | Immutable evidence package; the same normalized request maps to one row |
| `evaluation_observations` (0005) | PK (`evaluation_id`, `publication_id`), `post_metric_id` → `post_metrics`, run and candidate ids, `eligible`, `exclusion_reason`, `metrics_provider`, `actual_age_seconds`, hook, origin, format, strategy id/version/digest/group, length and bucket, UTC hour and block, the six counts | One row per post considered; no content copied |
| `strategy_proposals` (0005) | `id`, `evaluation_id`, `evaluation_digest`, `account_id`, `base_version_id` → `strategy_versions`, `status` (CHECK), `outcome`, `proposer`, `provider`, `model`, `prompt_version`, `proposal` (JSONB), `proposal_sha256`, `validation_errors`, `error_type`, `error_message`, `retry_of` UNIQUE → self, `requested_by`, `pricing_id`, timing, `llm_calls`, tokens | Its own started/finished ledger. Partial UNIQUE index on `account_id` WHERE status IN (`generating`, `pending_review`): one open proposal per account |
| `strategy_proposal_decisions` (0005) | `id`, `proposal_id` UNIQUE, `decision` (`approve` \| `reject`), `reviewer`, `auth_method`, `summary`, `reviewed_proposal_sha256`, `reviewed_evaluation_digest`, `resulting_version_id` | A proposal is decided once |
| `runs.strategy_version_ref` (0005) | FK → `strategy_versions`, NULL for older runs and unregistered strategies | Which registered version a run used |
| `experiments` (0006) | `id`, `account_id`, `strategy_id`, `base_version_id` → `strategy_versions`, `factor` (`hook_type` \| `length`), `synthetic`, `design` (JSONB), `design_sha256`, `snapshot_age_seconds`, `status` (CHECK), `peeked`, `stop_reason`, `created_by`, `auth_method`, lifecycle timestamps | Status: `draft`, `approved`, `running`, `stopped`, `concluded`, `cancelled`. Partial UNIQUE index on `account_id` WHERE status IN (`approved`, `running`). Trigger `trg_experiments_design_frozen` rejects `DELETE` and any change to the pre-registered columns |
| `experiment_arms` (0006) | `id`, `experiment_id`, `label`, `position`, `is_control`, `treatment` (JSONB) | UNIQUE (`experiment_id`, `label`) and (`experiment_id`, `position`); append-only |
| `experiment_assignments` (0006) | `id`, `experiment_id`, `arm_id`, `run_id` UNIQUE → `runs`, `block_no`, `position`, `assigned_by`, `auth_method`, `assigned_at` | UNIQUE (`experiment_id`, `block_no`, `position`): one slot of a permuted block per run; append-only |
| `experiment_events` (0006) | `id`, `experiment_id`, `kind`, `actor`, `auth_method`, `reason`, `detail`, `created_at` | Audit trail of lifecycle actions, including unblinding; append-only |
| `experiment_results` (0006) | `id`, `experiment_id` UNIQUE, `verdict`, `evidence_class`, `peeked`, `cutoff`, `analysis_version`, `input_digest`, `digest`, `analysis` (JSONB), `flags`, `concluded_by`, `auth_method` | One immutable result per experiment; append-only |
| `strategy_versions.source_experiment_result_id` (0006) | FK → `experiment_results`, UNIQUE, NULL except origin `experiment` | A result is adopted at most once |
| `discovery_sessions` (0007) | `id`, `account_id`, `status` (`running`, `succeeded`, `partial`, `failed`), `synthetic`, `request`, `limits`, `registry_version`, `registry_sha256`, `strategy_version_id`, `pricing_id`, `requested_by`, `auth_method`, `summary`, `error_message`, timing | One bounded discovery sweep |
| `discovery_fetches` (0007) | `id`, `session_id`, `source_id`, `kind`, `provider`, `outcome`, sanitized `request`, `requests_made`, `items_returned`, `post_reads`, `user_reads`, `http_status`, `error_category`, `error_message`, rate-limit fields, `etag`, `last_modified`, `latency_ms`, timing | Started/finished ledger per source call (also `cached`, `not_modified`, `skipped`); no response bodies |
| `discovery_items` (0007) | `id`, `item_key` UNIQUE, `source_id`, `platform`, `kind`, `category`, `evidence_type`, `tier`, `verification_status`, `publisher`, `provider_item_id`, `canonical_url`, `title`, `text`, `author`, `author_id`, `published_at`, `first_retrieved_at`, `last_retrieved_at`, `keywords`, `claim_hints`, `metrics`, `content_sha256`, `evergreen`, `synthetic`, `first_session_id`, `purged_at`, `purge_reason` | One row per unique external item. Trigger `trg_discovery_items_guard`: no delete, fixed identity, purged text can never be restored |
| `discovery_item_sightings` (0007) | `session_id`, `item_id`, `fetch_id`, `position`, `seen_at` | Which session and fetch saw an item; keeps sessions reproducible while items dedupe; append-only |
| `discovery_opportunities` (0007) | `id`, `session_id`, `account_id`, `fingerprint`, `rank`, `score`, `keywords`, `headline`, `components`, `reasons`, `claim_hints`, `claim_summary`, `suggested_intents`, `grok`, `grok_eligible`, `audience_entry_ids`, `synthetic`, `status` (`open`, `used`, `dismissed`), `expires_at`, dismissal fields | Trigger: only `status` and the dismissal fields may change; no delete |
| `opportunity_evidence` (0007) | `opportunity_id`, `item_id`, `role` (`primary` \| `discussion`), `position` | Append-only |
| `audience_entries` (0007) | `id`, `session_id`, `kind` (`account` \| `discussion`), `ref`, `platform`, `score`, `signals`, `matched_topics`, `explanation`, `evidence_item_ids`, `curated`, `position` | Read-only suggestions; no bios, no sensitive fields; append-only |
| `manual_exports` (0007) | `id`, `run_id`, `candidate_id`, `content_sha256`, `content_intent`, `manual_only`, `exported_by`, `auth_method`, `created_at` | A candidate handed to a person to post; append-only |
| `manual_publications` (0007) | `id`, `publication_id` UNIQUE, `export_id`, `run_id`, `candidate_id`, `platform`, `provider_post_id`, UNIQUE (`platform`, `provider_post_id`), `author_id`, `author_username`, `provider_created_at`, `text_match`, `verification_provider`, `verification_post_reads`, `verification_user_reads`, `registered_by`, `auth_method` | A post published by hand, verified and linked to its `publications` row (`provider = 'manual'`); append-only |
| `content_purges` (0007) | `id`, UNIQUE (`platform`, `provider_item_id`), `reason` (`deletion_request` \| `retention`), `requested_by`, `auth_method`, `counts` | Tombstones: a purged item is never stored or replayed again; append-only |
| `runs.opportunity_id`, `content_candidates.content_intent`, `research_findings.claim_kind` / `verification` (0007) | nullable | NULL for rows before Phase 9 |
| `post_metrics` (0004) | `id`, `job_id` UNIQUE, UNIQUE (`publication_id`, `snapshot_age`), `request_id`, `platform`, `provider`, `metrics_scope`, `provider_post_id`, `snapshot_age`, `target_age_seconds`, `scheduled_for`, `captured_at`, `provider_created_at`, `recorded_published_at`, `likes`, `reposts`, `replies`, `quotes`, `bookmarks`, `impressions` | Immutable observations. A count is NULL when the platform did not return it, 0 only when it returned 0. No raw provider JSON |

The provenance chain is a plain join:
`content_candidates → critiques`, `content_candidates → candidate_findings → research_findings
→ finding_evidence → source_posts`, `review_decisions → content_candidates`, and
`runs → discovery_opportunities → opportunity_evidence → discovery_items` (Phase 9; a
replayed `source_posts.source_id` is the item id), `manual_publications → publications`, and
`publications → content_candidates` (through a composite FK, so a publication's candidate
always belongs to its run), and `post_metrics → analytics_jobs → publications`.
`GET /publications/{id}/lineage` assembles the whole chain (see [ANALYTICS.md](ANALYTICS.md)).
The analytics tables reference `publications` without `ON DELETE CASCADE`, like
`publication_attempts`.

Migrations: `0001` is never modified. `0002` adds the three publishing tables, the decision
snapshot column and `UNIQUE (content_candidates.run_id, id)`. `0003` adds
`publications.retry_not_before`, the gate a rate-limited row waits behind until its reset
(NULL for existing rows). `0004` adds the four analytics tables and
`publications.provider_created_at` (NULL for existing rows); it creates **no** jobs, so
upgrading never schedules reads: older publications get jobs only through an explicit
`POST /publications/{id}/analytics/backfill` or `sga-analytics-worker backfill`. `0005` adds the six strategy
tables and `runs.strategy_version_ref` (NULL for existing runs), and a trigger function
`sga_reject_mutation` that rejects `UPDATE` and `DELETE` on `strategy_versions`,
`strategy_activations`, `performance_evaluations`, `evaluation_observations` and
`strategy_proposal_decisions`; it registers, activates or evaluates nothing, so upgrading
changes no behaviour of existing runs. All apply to
an existing database and to a fresh one (`sga-db upgrade`). A DB test upgrades a populated `0001` database
and checks the existing rows survive, and another compares the migrated schema against the
SQLAlchemy models; a third upgrades a populated `0004` database to `0005`, and a fourth a
populated `0005` database to `0006`.

`0006` adds the five experiment tables, `strategy_versions.source_experiment_result_id`
(NULL for existing rows) and `experiment` to the allowed version origins. Arms, assignments,
events and results reuse `sga_reject_mutation`; a new function `sga_freeze_experiment_design`
protects the pre-registered columns of `experiments`. It starts no experiment and changes no
existing run. Downgrading `0006` fails while a version with origin `experiment` exists,
because the narrowed origin check would reject it; such versions must be handled by hand.

`0007` adds the ten discovery tables above, the four nullable columns and a non-unique
index `ix_publications_provider_post` on (`platform`, `provider_post_id`) for the
registration lookup (mock publishers of earlier phases may share ids, so uniqueness of a
manually registered post comes from `uq_manual_publications_post` and a locked lookup).
Sightings, evidence links, audience entries, exports, registrations and purge tombstones
reuse `sga_reject_mutation`; two new functions guard items and opportunities. It creates
no session, run or job, and changes no existing row; a DB test upgrades a populated `0006`
database and downgrades it again. Retention never deletes rows: it nulls item text,
replaces run evidence text with a placeholder, rewrites the affected LangGraph checkpoint
values in place and records a tombstone (see [CONTENT_DISCOVERY.md](CONTENT_DISCOVERY.md)).

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
