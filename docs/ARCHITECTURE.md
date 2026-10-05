# Architecture

## 1. Product purpose

social-growth-agent helps creators, technical founders and small businesses grow on X. It
researches what is working in their niche, drafts posts that fit their strategy, critiques the
drafts, asks a human to approve them, publishes, measures the results, and feeds what it learns
back into the next round. The engineering goal is a **closed, measurable learning loop** with
clear control flow, not a pile of prompts.

## 2. Deterministic orchestration, probabilistic agents

The central design rule, added in Phase 2 when real LLMs arrived:

> **LLMs produce typed judgements. Deterministic code decides what happens next.**

| Decided by the LLM (probabilistic) | Decided by application code (deterministic) |
|---|---|
| Research themes, opportunities, confidence, claim type (observation or hypothesis) | Which posts the research is based on, and where they came from (`ResearchSource`); that every cited evidence id was supplied; limitations for sample size, missing impressions and synthetic data |
| Post text, hook, rationale, which earlier candidate it revises | Ids, lineage, attempt numbers, strategy version |
| Critic recommendation, score, risks, soft issues | Hard policy (length, format, blank content) and the **final verdict** |
| | Whether the batch is good enough for review (`CriticGate`) |
| | Whether to retry, how many times, and when to fail (`RunConfig`, routing) |
| | Human review, the edit invariant, and approval eligibility |

The model never returns a routing instruction, and nothing in its output is used as one. Routing
functions (`graph/routing.py`) read only validated state. A model that misbehaves can at worst
produce output that fails validation, and then the run fails with a recorded error.

## 3. The learning loop

```
Research -> Content generation -> Critic -> Human approval -> Publishing
    ^                                                              |
    |                                                              v
Strategy update  <-----------------  Analytics  <----------  Metrics collection
```

Phases 1 to 3 implement research (from mock fixtures or live X) through human approval. The right half of the loop exists in
the types (`PublishState`, `PostMetrics`, `PerformanceInsight`, `Experiment`) but has no nodes yet.

## 4. Agents and their contracts

Every agent is a small class. It renders a prompt, calls the provider through
`agents/base.call_llm`, then converts the **LLM output schema** into **domain objects** inside
`validating_output`. The two schemas are deliberately separate. The model never invents ids or
lineage, and every cross-reference is checked. A violation raises `AgentOutputError` carrying
the `LLMCall` record.

| Agent | LLM output schema | Validated into | Checks |
|---|---|---|---|
| Research | `ResearchReport{topic, summary, findings[theme, summary, claim_type, evidence_source_ids, signal_strength], content_opportunities[angle, rationale, finding_indexes], confidence, limitations}` | `list[ResearchFinding]`, `ResearchBrief` | evidence ids ⊆ supplied `source_id`s; opportunity indexes valid; ≥1 finding; deterministic limitations appended by code (§8a) |
| Content | `CandidateBatch{candidates[content, topic, hook_type, format, target_audience, research_finding_ids, rationale, revises_candidate_id]}` | `list[ContentCandidate]` | 1..count candidates; finding ids known; `revises_candidate_id` must be one of the previous attempt's critiqued candidates |
| Critic | `CriticReport{evaluations[candidate_id, recommendation, score, tone_match, factual_risk, originality_risk, issues[category, detail], suggested_revision]}` | `list[Critique]` | exactly one evaluation per candidate; score 0..1; then hard policy |

Prompts live in `agents/prompts/*.md`. Each has Role, Objective, Available inputs, Output
contract, and Rules/Limitations. The research prompt states that the material is supplied by the
application and that the model must not claim to have browsed X or any other source. It also
separates observation, hypothesis and causal claim, forbids causal claims about engagement, and
lists the limitations to state (small sample, incomplete metrics, missing impressions, mixed
audiences, unrepresentative sample). `ClaimType` has no `causal` value, so the schema itself
cannot express one.

LLM output schemas contain no defaults, so every field is required. A test checks each one
against OpenAI's strict-mode schema converter (`tests/test_llm_contracts.py`).

## 5. Critic: hard policy vs. LLM judgement

```
candidate --> LLM evaluation (recommendation, score, risks, soft issues)
          --> policies.find_violations(candidate, ContentPolicy)   # hard rules, code only
          --> policies.final_verdict(recommendation, violations)   # violation => never PASS
          --> Critique{verdict, recommended_verdict, assessment, issues, policy_violations}
```

- **Soft issues** come from the model: `weak_hook`, `unclear`, `low_usefulness`, `boring`,
  `off_brand`, `off_strategy`, `unsupported_claim`, `not_suited_to_x`, `other`.
- **Hard violations** come from code: `max_post_length`, `allowed_format`, `non_blank_content`.
  `ContentPolicy(max_post_length=280, allowed_formats={single})` is configured per run through
  `RunConfig.content_policy`. A new rule is one small `HardRule` class added to `DEFAULT_RULES`.
- A violation downgrades the model's `pass` to `revise`, so the retry loop can fix it. The model's
  `reject` is never upgraded. Both `recommended_verdict` and `verdict` are stored, so disagreement
  between the model and the policy is measurable.

### Critic gate

`policies.CriticGate` is the only place that decides whether a batch may go to human review. The
Phase 2 default is at least one passing candidate. It already supports `min_passing_candidates`
and `min_best_score`, and aggregate or per-dimension thresholds would go here too. Routing
(`route_after_critique`) and `request_review` both ask the gate; neither reimplements it.

## 6. Graph

```mermaid
graph TD;
  START([start]) --> retrieve
  retrieve -.-> research
  retrieve -.-> failed
  research -.-> generate
  research -.-> failed
  generate -.-> critic
  generate -.-> failed
  critic -.->|gate open| request_review
  critic -.->|gate closed, attempts < max| generate
  critic -.->|gate closed, attempts = max| failed
  request_review --> human_review
  human_review -.->|approve / reject / regenerate| END([end])
  human_review -.->|edit| critique_edit
  critique_edit -.-> request_review
  critique_edit -.-> failed
  failed --> END
```

`retrieve` (Phase 3) is the only node that talks to a social platform; `research` only
interprets what `retrieve` stored. The split keeps retrieval and interpretation apart, and it
means a retried or failed research LLM call never re-fetches (and re-bills) platform data.

### Retry feedback loop

When the gate is closed and attempts remain, `generate` runs again with
`GraphState.revision_feedback()`: every non-passing candidate of the last attempt paired with its
critique (verdict, soft issues, hard violations, suggested revision). The Content Agent is told to
revise those posts, not start over, and each new candidate records `revises_candidate_id`. The
chain attempt 1 → critique → attempt 2 is therefore explicit in state. The number of attempts is
`RunConfig.max_generation_attempts` (default 3, hard ceiling 10). The model has no say in it.

### Human edit invariant

> Materially edited content must pass critique again before it can be approved.

- An edit never mutates a candidate. `policies.edited_candidate` creates a new candidate with
  `origin=human_edit` and `revises_candidate_id=<original>`. Whitespace-only edits are refused as
  non-material.
- `human_review --edit--> critique_edit`. That node runs the full critic (LLM plus hard policy) on
  the edited candidate, then returns to `request_review`.
- `review.candidate_ids` only ever holds candidates whose latest critique passed. Approval is
  checked against it, and against `latest_critique(candidate).passed` as defence in depth. An edit
  that fails is shown to the reviewer as `ReviewRequest.rejected_edit` and cannot be approved.
- Edits are bounded by `RunConfig.max_edit_rounds` (default 3).

The unsafe path, critic passes → human edits → publish, does not exist in the graph.

### Failure handling

| Failure | Mechanism | Outcome |
|---|---|---|
| Connection error, 5xx, timeout (`TransientProviderError`); LLM rate limits | LangGraph `RetryPolicy` on provider-calling nodes | retried up to 3 attempts (`retrieve`: capped by the provider's request budget); then as below |
| X rate limit (`RateLimitedError`, HTTP 429) | not transient: never retried, never slept on | reset time recorded in the error and in `research_fetches`; run fails |
| Any provider error left after retries, or a non-retryable one (auth, 4xx, malformed response) | node `error_handler` (`provider_error_handler`) | `RunError` recorded, `status=failed`, `goto failed`. `start_run` returns normally. |
| Invalid, truncated or filtered output, empty result, refusal, contract violation (`RunAbortError`) | `instrument` wrapper | `RunError` recorded, routed to `failed`, not retried |
| Empty research results | `InsufficientSignalError` from `retrieve` | run fails before any LLM call; the fetch is recorded |
| Missing API key or bearer token | `ConfigurationError` at startup | no run is created |
| Bad review decision | `InvalidReviewError` to caller | run stays paused |

There is no silent fallback to fake content. `LLM_PROVIDER=fake` and `RESEARCH_PROVIDER=mock`
must be chosen explicitly (mock is the research default; X is opt-in). A failing X call is never
replaced by fixture posts.

## 7. State model

`GraphState` (`graph/state.py`) is a Pydantic model:

| Group | Fields |
|---|---|
| Identity and inputs | `run_id`, `started_at`, `account`, `strategy`, `config` (limits, `content_policy`, `critic_gate`, optional `research_query`) |
| Work products | `source_posts`, `research_source`, `research`, `research_brief`, `candidates` (append), `critiques` (append) |
| Decisions and results | `review` (status, candidate ids, decision, edit rounds, pending/rejected edit), `publish`, `metrics`, `insights` |
| Control | `status`, `research_attempts`, `generation_attempts`, `errors` (append), `events` (append) |
| LLM trace | `llm_calls` (append): agent, task, provider, model, attempt, outcome, latency, token usage, error type |
| Research trace | `research_fetches` (append): provider, original and effective query, requests, posts and users returned, latency, outcome, error category, rate-limit remaining/reset |

Candidates and critiques accumulate across attempts and edits. Helpers derive the views:
`current_candidates()` (generated, latest attempt), `reviewable_critiques()` (via the gate),
`revision_feedback()` and `latest_critique(id)`. Nodes return partial `StateUpdate`s, and domain
objects are frozen.

### Traceability chain

```
SourcePost.source_id ("x_<post id>")  <-  ResearchFinding.evidence_source_ids ; ResearchBrief.source.source_ids
ResearchFinding.id  <-  ContentCandidate.research_finding_ids ; ContentOpportunity.finding_ids
ContentStrategy.id/version  <-  ContentCandidate.strategy_id/strategy_version
ContentCandidate.id  <-  ContentCandidate.revises_candidate_id (model retry or human edit)
ContentCandidate.id  <-  Critique.candidate_id  <-  ReviewDecision.candidate_id
ContentCandidate.id  <-  PublishedPost.candidate_id             (Phase 5)
PublishedPost.platform_post_id  <-  PostMetrics                 (Phase 6)
```

## 8. Provider boundary

```
agents  --LLMRequest-->  LLMProvider (Protocol)  --LLMResponse[T]-->  agents
                           |-- OpenAIProvider   (providers/openai_provider.py, the only SDK import)
                           |-- ScriptedLLMProvider (tests, demo)
```

- `LLMProvider.generate_structured(request, schema) -> LLMResponse[T]` (output, model, token usage).
- `LLMRequest` carries `agent`, `task`, `system`, `prompt`, a structured `payload` (used by fakes
  and evals, ignored by real adapters), `generation_attempt`, and `LLMSettings` (provider, model,
  temperature or none, max output tokens, structured-output strategy).
- `OpenAIProvider` uses the Responses API, `responses.parse(text_format=schema)`, so the schema
  is enforced server-side and validated again client-side. SDK retries are disabled so the
  graph's `RetryPolicy` is the only retry budget. It maps SDK exceptions onto the domain hierarchy
  in `errors.py`.
- Configuration (`config.AppSettings`, env or `.env`): `LLM_PROVIDER`, `OPENAI_API_KEY` (a
  `SecretStr`), `OPENAI_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_TEMPERATURE_ENABLED`; since Phase 3
  `RESEARCH_PROVIDER`, `X_BEARER_TOKEN` (a `SecretStr`), `X_MAX_RESULTS_PER_QUERY`,
  `X_MAX_QUERIES_PER_RUN`, `X_TIMEOUT_SECONDS`.
  `services.factory.build_dependencies` is the only place that turns settings into dependencies.
- **Per-agent configuration.** `AgentSettings` holds one `LLMSettings` per agent, and
  `Dependencies.agent_llms` can give any agent its own provider (for example a cheaper critic).
  Graph code is unaffected.

Social platforms sit behind `SocialResearchProvider` (which also declares `source_name`,
`synthetic` and `max_requests_per_run`), `SocialPublisher` and `SocialAnalyticsProvider`, each
with deterministic mocks. Research has a real adapter since Phase 3 (§8a).

## 8a. X research provider (Phase 3)

### Retrieval vs. interpretation

| Layer | Owns | Never does |
|---|---|---|
| `XResearchProvider` (`providers/x/`) | HTTP, auth, query translation, normalization, error mapping, fetch metadata | call an LLM, interpret trends, generate content, route |
| `retrieve` node | choose the query (`RunConfig.research_query` or the strategy pillars), call the provider, store posts and `ResearchSource` | interpret |
| Research Agent | interpret the supplied posts into findings with evidence ids | fetch, browse |
| `policies.research_policy` | default query, deterministic limitations | call anything |
| graph | retries within the request budget, routing to `failed` | inspect content |

```
user / RunConfig.research_query  ->  ResearchQuery (original text, kept verbatim)
  -> XResearchProvider.search
       build_search_request: effective query = original + "-is:retweet" (+ lang:/from:),
                             OR-queries parenthesized first; max_results capped
       XApiClient.get  GET https://api.x.com/2/tweets/search/recent   (one request)
       normalize_search_response: data[] + includes.users[] -> SourcePost[]
  -> SearchResult{posts, fetch: ResearchFetch}
  -> GraphState.source_posts / research_source / research_fetches
  -> Research Agent -> findings (evidence_source_ids ⊆ supplied ids) -> Content -> Critic -> review
```

`providers/x/` is the only code that knows X's URL, parameters, JSON shape or headers. It uses
`httpx` directly (no SDK, no scraping, no browser automation).

### Authentication

App-only Bearer token (`X_BEARER_TOKEN`), the minimum credential for read-only recent search.
It is a `SecretStr` in `AppSettings`, unwrapped once into the `XApiClient`'s default headers, and
exists nowhere else: not in `GraphState`, checkpoints, `LLMRequest`s, `ResearchFetch`, logs or
exception messages. Transport exceptions are not chained onto domain errors (`__context__` is
`None`), because httpx request objects carry the headers. Tests assert each of these.

### SourcePost

`source_id` (`x_<id>`), `platform`, `author_id`, `author_username` (from the `author_id`
expansion, `None` if absent), `text`, `created_at`, `lang`, `likes`, `reposts`, `replies`,
`quotes`, `impressions` (all optional, from `public_metrics`), `query` (the original query) and
`retrieved_at`. Missing metrics stay `None`; they are never defaulted to 0. Raw X JSON is parsed
by private models in `normalize.py` and never leaves that module.

Requested fields only: `tweet.fields=created_at,author_id,lang,public_metrics`,
`expansions=author_id`, `user.fields=username`, `sort_order=relevancy`.

### Provenance guarantees

1. Every `SourcePost` gets its `source_id` from the X post id, in code.
2. The Research Agent receives posts with their `source_id`s and must cite them in
   `evidence_source_ids`.
3. `_to_domain` rejects any finding citing an id not in the supplied set
   (`AgentOutputError`); the run fails with a recorded error. Nothing is dropped silently.
4. `ResearchBrief.source` records provider, supplied ids, synthetic flag, original and effective
   query and retrieval time, set by the application.
5. Content candidates can only cite known finding ids (Phase 2 check), so every candidate traces
   back to real post ids.

### Cost control

| Control | Default | Where enforced |
|---|---|---|
| `X_MAX_RESULTS_PER_QUERY` | 10 (API minimum; range 10..100) | `build_search_request` caps `max_results` |
| `X_MAX_QUERIES_PER_RUN` | 1 (range 1..5) | `retrieval_retry_policy` caps `retrieve` attempts (first try + retries) |
| Pagination | none | one request per `search`; `next_token` is ignored |
| Rate limits | never retried | `RateLimitedError` is not transient |
| Empty results | no LLM call | `retrieve` fails the run |

`ResearchFetch` records `requests_made`, `posts_fetched` (post reads) and `users_fetched` (user
objects from the expansion). These are resource counts only; no prices exist in code.

### Failure handling (X)

| X / transport | Domain error | Category | Retried |
|---|---|---|---|
| 401, 403 | `ProviderError` | `auth` | no |
| 429 | `RateLimitedError` (`reset_at` from `x-rate-limit-reset`) | `rate_limited` | no |
| 400, or 200 with only `errors` | `ProviderError` (with X's title/detail) | `bad_request` | no |
| other 4xx | `ProviderError` | `unexpected_status` | no |
| 5xx | `TransientProviderError` | `server_error` | within budget |
| timeout | `ProviderTimeoutError` | `timeout` | within budget |
| connection error | `TransientProviderError` | `network` | within budget |
| non-JSON or wrong shape | `ProviderError` | `malformed_response` | no |
| `result_count: 0` | empty `SearchResult` → `InsufficientSignalError` in `retrieve` | n/a | no |

Each failing `search` attaches its `ResearchFetch` to the exception, so the run records what
was attempted. One structured log line per fetch (`research fetch`) carries provider, query,
effective query, requests, posts, latency, outcome and error category; never credentials.

### Deterministic limitations

`policies.deterministic_limitations` appends, regardless of what the model wrote: a small-sample
limitation below 20 posts, a missing-impressions limitation when any post lacks impressions, and
the synthetic-data limitation for mock data.

## 9. Layering

```
api  ->  services  ->  graph  ->  agents  ->  providers (protocols)
                         \          \-> policies
                          \-> policies
                     models  <- everything      config -> services.factory, providers.factory
```

## 10. Evaluation and observability

- `evaluation/critic_eval.py` scores final critic verdicts (model plus policy) against labelled
  cases. With real models it is the regression gate for prompt and model changes.
- Every node appends a `NodeEvent`, every research retrieval appends a `ResearchFetch`, and
  every LLM call appends an `LLMCall` (success or failure,
  latency, tokens). `services/trace.TracePrinter` renders both as a readable run trace. Structured
  JSON logging is in `observability/`. OpenTelemetry replaces the internals in Phase 10.
- Checkpoints deserialize only an explicit allowlist of domain and policy types.

## 11. Future phases

See [ROADMAP.md](ROADMAP.md). Next structural additions:

- the `research → enough_signal? → broaden → research` loop, bounded by `max_research_attempts`;
- `publish → wait_for_metrics → analyze → update_strategy` after approval;
- a Postgres checkpointer and SQLAlchemy tables for runs, candidates, critiques, decisions,
  LLM calls, posts and metrics.
