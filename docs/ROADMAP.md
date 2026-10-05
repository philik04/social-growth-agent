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

## Phase 4: Human-in-the-loop persistence and resume
- Validate Phase 2/3 live paths once with real keys; fix anything real-model or real-X specific.
- Postgres checkpointer; SQLAlchemy models for runs, candidates, critiques, decisions, LLM calls and research fetches.
- API: start run, list pending reviews, submit decision, get run.
- Regenerate-with-reviewer-notes loop (edit + mandatory re-critique already exists).
- Carried over: `enough_signal?` broaden-research loop, labelled critic eval set, cost per run from `LLMCall` and `ResearchFetch` counts against a configurable price table outside business logic.
- **Exit:** a run survives a process restart between pause and resume.

## Phase 5: Publishing
- X publisher adapter; `publish` node after approval; idempotency keys to prevent double posts.
- Scheduling of approved posts into posting windows.
- **Exit:** approved posts publish exactly once, and failures are visible and retryable.

## Phase 6: Analytics collection
- `SocialAnalyticsProvider` for X; scheduled metric snapshots (e.g. 1h, 24h, 72h).
- Metrics stored per post with lineage to candidate, critique and strategy.
- **Exit:** every published post has metric snapshots joined to its full trace.

## Phase 7: Strategy-learning feedback loop
- Analytics Agent derives `PerformanceInsight`s (hook type, topic, format, length, posting window).
- Strategy Agent proposes a new `ContentStrategy` version, and a human approves the change.
- **Exit:** new runs use the updated strategy, and results are attributable to the strategy version.

## Phase 8: Experiments and A/B testing
- `Experiment` lifecycle: hypothesis, variants, assignment, significance-aware conclusion.
- **Exit:** at least one experiment runs to a conclusion that updates strategy.

## Phase 9: Dashboard
- Next.js UI: review queue, run traces, performance and experiment views.
- **Exit:** a non-technical user can review, approve and see results without the API.

## Phase 10: Production deployment and observability
- Docker, GitHub Actions CI (lint, types, tests, evals), AWS deployment.
- OpenTelemetry traces from `NodeEvent` instrumentation; alerting on failure rates.
- Auth and multi-tenant accounts.
- **Exit:** deployed, monitored, and reproducible from CI.
