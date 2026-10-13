# Roadmap

Each phase ends with a working, tested system. Scope stays X-only until the loop is proven.

## Phase 1: Graph foundation (done)
- Typed domain models and graph state; provider protocols with deterministic mocks.
- LangGraph workflow: research → generate → critic → bounded retry → human review (interrupt) / failed.
- Retry policy for transient errors; recorded failures for contract violations.
- Critic evaluation harness, structured logging, `/health` API.
- **Exit:** tests cover success, retry-then-success, retry limit, routing, state and review resume.

## Phase 2: Real LLM agents and structured outputs (done)
- OpenAI adapter (Responses API structured outputs) behind `LLMProvider`; config from env; no silent fallback.
- Structured contracts for research, content and critic; prompts in `agents/prompts/`.
- `ContentPolicy` hard rules enforced after LLM judgement; `CriticGate` owns the review decision.
- Retry feedback loop with `revises_candidate_id` lineage; human edits re-critiqued before approval.
- Provider failures after retries recorded and routed to `failed`; `LLMCall` metadata (latency, tokens).
- Opt-in live demo and test (`RUN_LIVE_LLM_TESTS=1`).
- **Exit:** met for the deterministic suite. Still open: a labelled evaluation set and a threshold on real-model critic agreement (carried into Phase 3).

## Phase 3: Real X research with provenance (done)
- `XResearchProvider` behind `SocialResearchProvider`: X API v2 recent search over `httpx`, app-only Bearer token, minimal fields, normalized `SourcePost`s.
- Retrieval split from interpretation: a `retrieve` node feeds the Research Agent; a research LLM retry never re-fetches.
- Provenance: findings cite `evidence_source_ids` (`x_<id>`), validated against the supplied set; fabricated ids fail the run.
- Cost control: `X_MAX_RESULTS_PER_QUERY` (10), `X_MAX_QUERIES_PER_RUN` (1) caps retrieval attempts, no pagination; `ResearchFetch` records requests, post and user reads. No prices in code.
- Failures mapped to domain errors with categories; HTTP 429 is recorded with its reset time and never retried or slept on.
- Deterministic limitations (small sample, missing impressions, synthetic data); research prompt separates observation, hypothesis and causal claim.
- Opt-in live demo and test (`RUN_LIVE_X_TESTS=1`). Mock stays the default research provider.
- **Exit:** met offline (165 tests, no credentials). Still open: the first live X run, the `enough_signal?` broaden loop, account timelines, the labelled critic eval set and cost per run (moved to Phase 4 below).

## Phase 4: Persistent backend, review API and resume (done)
- PostgreSQL: LangGraph `PostgresSaver` for execution state; SQLAlchemy 2 tables (Alembic `0001`) for runs, source posts, briefs, findings with evidence, candidates, critiques, review decisions, events, errors, and a usage ledger; `sga-db` CLI; `docker-compose.yml` for local Postgres.
- Provenance enforced by foreign keys: evidence can only cite a post the same run retrieved.
- API: start run, list runs, get run, pending reviews, review (approve / reject / edit / regenerate with notes), resume, usage. Background thread pool; 202 responses; 404/409/422/503 mapping.
- Restart safety: checkpoint after every step; stalled runs are flagged at startup and resumed only explicitly, without repeating completed X retrievals or generations.
- Regenerate with reviewer notes: a bounded new generation cycle (`max_regenerations`); notes persisted and passed to the Content Agent; previous critiques kept. Reject is terminal.
- Enough-signal loop: deterministic post count after retrieval; broaden or retry within `max_research_attempts` and the X request budget; every request recorded.
- Usage and cost: canonical counts per call (X post reads and user reads separate, tokens per model); prices only in `pricing.toml`; each run stores its price list so estimates are reproducible (`at_run_pricing` vs `at_current_pricing`); always labelled estimated.
- CI workflow with a PostgreSQL service; DB tests skip locally without `TEST_DATABASE_URL` and fail in CI.
- **Exit:** met. A run survives a killed process between steps and between pause and resume (restart demo and DB tests). Still open: a labelled critic eval set, real prices in `pricing.toml`, and a live run of the broaden loop with `X_MAX_QUERIES_PER_RUN > 1`.

## Phase 5: Publishing (done)
- `XPublisher` on OAuth 1.0a user context (`POST /2/tweets`), with publishing credentials kept
  entirely separate from the read-only research bearer token.
- Publishing is a separate explicit action, never a consequence of approval and never done in
  an API endpoint: `POST /runs/{id}/publish` records one durable intent (unique idempotency key,
  composite FK to the candidate of that run), and `sga-publisher-worker` claims due work with
  `FOR UPDATE SKIP LOCKED` plus a lease and makes the single platform call.
- A deterministic publish policy (run approved, this candidate approved, latest critique passed,
  content policy still passes, no existing publication) decides eligibility; no LLM is involved.
- Scheduling (`scheduled_for`, timezone required, up to 30 days ahead) and cancellation.
- Outcomes X cannot confirm become `unknown` and are **never** retried automatically; only a
  human resolve moves them on. Only failures proven to precede the request, and rate limits once
  their reset has passed (`retry_not_before`, no sleeping), are retried automatically, bounded.
- Started/finished ledgers: `publication_attempts` for every publish call and `provider_operations`
  for every provider-calling node attempt, so an in-flight call stays visible after process death.
- **Exit:** approved posts publish at most once per intent, every failure is an explicit state,
  and live publishing is double-gated and limited to one harmless post.

Not done in Phase 5 (deliberate): no timeline reconciliation of `unknown` publications
(resolving is manual), no threads or media, no multi-account publishing (no OAuth 2.0 PKCE
token store), no analytics.

## Phase 6: Analytics collection (done)
- `SocialAnalyticsProvider` with `XAnalyticsProvider` (`GET /2/tweets?ids=`, `public_metrics`
  and `created_at`, app-only bearer token) and a scripted mock.
- `analytics_jobs` (one per publication and age, default 1h/24h/72h) created in the same
  transaction that records a post as published, or by an explicit idempotent backfill; never
  by the worker or a migration.
- `sga-analytics-worker`: `FOR UPDATE SKIP LOCKED` claims with leases, started/finished request
  and attempt ledgers committed around each call, a deterministic bounded backoff persisted in
  `retry_not_before`, a deleted post cancelling its later jobs.
- `post_metrics`: immutable snapshots, NULL when X did not return a count; recorded vs
  creation time kept apart, with capture delay, actual age and an on-target flag per snapshot.
- API: metrics, lineage, `/posts`, jobs, retry and backfill; analytics usage and cost lines.
- The Phase 5 `not_sent` retry now waits for a backoff.
- **Exit:** met for collection: every published post gets scheduled snapshots joined to its full
  trace. Still open: a live read with our own token to confirm which public metrics we receive,
  and real prices in `pricing.toml`.

Not done in Phase 6 (deliberate): private metrics (user-context auth), media view counts,
interpretation of the numbers, any automatic strategy change.

## Phase 7: Strategy evaluation and human-approved proposals (done)
- Deterministic, age-matched evaluations over `post_metrics` (`POST /evaluations`): eligible
  vs excluded posts with reasons, groups by hook type, strategy version, length, UTC hour
  block, origin and format, descriptive statistics with explicit evidence levels and
  "not causal" comparisons. Synthetic (`mock_analytics`) and real metrics never mix.
  Immutable, reproducible evidence packages with a digest; no model and no platform call.
- Opt-in Strategy Agent (`STRATEGY_PROPOSER`, default disabled) proposing changes to
  `preferred_hooks` and `length_guidance` only, each citing comparisons of the evaluation and
  validated in code (no invented evidence or numbers). Insufficient evidence gives a
  `no_change` without a model call. One open proposal per account; explicit retry of failed
  or stale proposals.
- Immutable strategy versions; all-or-nothing approval (review token, digests of what the
  reviewer saw) creates an inactive version; activation is a separate compare-and-set step;
  runs record `strategy_version_ref`. Migration `0005`. See
  [STRATEGY_EVALUATION.md](STRATEGY_EVALUATION.md).
- **Exit:** met: new runs can use the approved, activated version, and their results are
  attributable to it. Not yet shown on real data: a personal account needs weeks of posts
  before any comparison reaches the default thresholds.

Not done in Phase 7 (deliberate): no automatic strategy change or activation, no changes to
pillars, tone, audience or posting time, no significance tests or causal claims, no
reviewer identities (one shared token).

## Phase 8: Experiments and A/B testing (done)
- Pre-registered experiments (`POST /experiments`, review token): factor (hook type or
  length range), 2 or 3 arms, primary metric, planned posts per arm, snapshot age, maximum
  duration, stopping rules and attrition threshold, fixed before any assignment. The base
  strategy version is pinned; the design is frozen by a database trigger.
- Randomized assignment per run (permuted blocks, `SELECT ... FOR UPDATE`, unique slot and
  run constraints); run and assignment are written in one transaction. The arm is a hard
  rule in the run's content policy; an informational heuristic estimates whether the post
  actually uses the declared hook.
- Full funnel per arm (assigned, generated, reviewed, approved, published, eligible),
  balance and adherence; age-matched pooled-rate difference with a percentile bootstrap
  interval, median difference as a secondary estimate. Outcomes masked until conclusion;
  early unblinding is explicit, irreversible and makes the verdict exploratory. Results are
  immutable and reproducible from their input digest.
- Adoption (review token) of a confirmatory result on real data creates an inactive version
  (origin `experiment`); activation stays the Phase 7 step. Synthetic results never adopt.
- Feasibility tables (5 posts/week planning assumption), per-arm usage with labelled
  estimates and unknown prices, a compact evidence digest for the Strategy Agent
  (`strategy-v2`). Migration `0006`. See [EXPERIMENTS.md](EXPERIMENTS.md).
- **Exit:** met in code and on synthetic data: an experiment runs to a stored conclusion
  and, when confirmatory on real data, can be adopted into a strategy version. Not yet
  shown on real data: at 5 posts a week a two-arm experiment with 50 posts per arm needs
  about 25 weeks, beyond the default 120-day maximum.

Not done in Phase 8 (deliberate): no sequential or Bayesian testing, no multi-factor
designs, no automatic start, stop, adoption, activation or publishing, no posting-time
experiments, no reviewer identities (one shared token).

## Phase 9: Dashboard
- Next.js UI: review queue, run traces, performance and experiment views.
- **Exit:** a non-technical user can review, approve and see results without the API.

## Phase 10: Production deployment and observability
- Docker, GitHub Actions CI (lint, types, tests, evals), AWS deployment.
- OpenTelemetry traces from `NodeEvent` instrumentation; alerting on failure rates.
- Auth and multi-tenant accounts.
- **Exit:** deployed, monitored, and reproducible from CI.
