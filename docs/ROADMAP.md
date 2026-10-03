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

## Phase 3: Research provider and X integration
- X API research adapter (search, account timelines) behind `SocialResearchProvider`.
- `enough_signal?` routing with a bounded broaden-research loop.
- Labelled critic eval set run against the real model; cost per run reported from `LLMCall`s.
- Rate-limit handling mapped to `TransientProviderError`.
- **Exit:** research runs against live X data with recorded fixtures for tests.

## Phase 4: Human-in-the-loop persistence and resume
- Postgres checkpointer; SQLAlchemy models for runs, candidates, critiques and decisions.
- API: start run, list pending reviews, submit decision, get run.
- Regenerate-with-reviewer-notes loop (edit + mandatory re-critique already exists).
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
