# Social Growth Agent

A stateful Python system for researching, drafting and reviewing content for X,
with separate publishing, analytics and strategy-review services.

**Python 3.12+ · LangGraph · FastAPI · PostgreSQL · Pydantic**

**Portfolio focus:** LLM application engineering with typed contracts, bounded
revisions, durable state and explicit human control over external side effects.
Research evidence, candidates, critiques, approvals and later metrics remain
traceable. Synthetic demonstrations establish workflow behavior; they do not
measure real audience or engagement growth.

## Review in two minutes

| Explore | Evidence |
| --- | --- |
| Run the credential-free demo | [Quickstart](#quickstart): fake agents, mock research and in-memory checkpoints |
| Inspect workflow control | [Graph construction](src/social_growth_agent/graph/builder.py), [routing](src/social_growth_agent/graph/routing.py) and [architecture](docs/ARCHITECTURE.md) |
| Review policy boundaries | [Content rules](src/social_growth_agent/policies/content_policy.py), [critic gate](src/social_growth_agent/policies/critic_gate.py) and [publication policy](src/social_growth_agent/policies/publishing.py) |
| Follow persistence and side effects | [Database design](docs/DATABASE.md), [publishing](docs/PUBLISHING.md) and [analytics lineage](docs/ANALYTICS.md) |
| Assess evaluation and testing | [Critic evaluation harness](src/social_growth_agent/evaluation/critic_eval.py), [tests](tests) and [CI workflow](.github/workflows/ci.yml) |

## Engineering highlights

- **Typed agent contracts.** Pydantic validates workflow state and structured
  research, content and critique results before they move downstream.
- **Deterministic control.** Code owns graph routing, hard content rules, retry
  policy and revision budgets. The critic recommends; policy determines whether
  a candidate can reach review.
- **Explicit review boundaries.** The graph pauses at human review. Edits create
  new candidates and require a new critique; approval alone never publishes.
- **Durable publication.** A separate intent and lease-based worker handle
  publishing. Ambiguous outcomes become `unknown` and require human resolution;
  exactly-once delivery is not guaranteed. All mentions are manual-only.
- **Evidence and reviewed learning.** [Discovery](docs/CONTENT_DISCOVERY.md) ranks
  stored evidence; [strategy evaluation](docs/STRATEGY_EVALUATION.md) describes
  age-matched outcomes. A reviewed proposal creates an inactive strategy version;
  activation is separate. [Experiments](docs/EXPERIMENTS.md) preserve preregistration
  and masked outcomes, and synthetic results cannot be adopted.
- **Testable external boundaries.** Provider protocols, deterministic fakes, mock
  transports and a labelled critic-evaluation harness support checks without
  paid model calls or live X activity.

This is a local, operator-facing portfolio system. Core API routes lack general
authentication; keep them bound to localhost. Retention redacts source evidence
and checkpoints; derived findings, drafts and backups need separate review.
See [operational boundaries](docs/CODEX_HANDOFF.md#security-configuration-and-known-limitations)
and the [controlled pilot guide](docs/CONTROLLED_PILOT.md) before live use.
Development history and phase details are in the [roadmap](docs/ROADMAP.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

Use a fresh checkout without a `.env` file for this offline walkthrough:

```bash
uv sync --locked
uv run python -m social_growth_agent.services.demo
```

The demo injects fake agents and mock research, revises synthetic drafts,
pauses at review, then supplies a scripted approval decision. It uses in-memory
checkpoints, requires no database or API key, and publishes nothing.

Expected progression (candidate IDs and draft text vary):

```text
attempt 1 → critic: revise (unsupported_claim)
attempt 2 → critic: pass → awaiting_review
scripted demo approval → approved
```

For verification without database or live tests:

```bash
uv run pytest -m "not db and not live" --ignore=tests/db --ignore=tests/live
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

In an existing configured checkout, use the
[isolated offline runner](docs/CODEX_HANDOFF.md#verification-and-next-steps) to
disable `.env` loading. Full PostgreSQL checks and offline database demos are
separate from this in-memory walkthrough.

### Persistent API (PostgreSQL)

The operator examples below create database state. Use a disposable environment
for demonstrations and tests; follow the [database guide](docs/DATABASE.md) and
keep the API bound to `127.0.0.1`. Database tests require an explicitly authorized
disposable PostgreSQL server with `CREATEDB`; never point them at real account data.

```bash
docker compose up -d                                   # PostgreSQL 16 on 127.0.0.1:5433
export DATABASE_URL=postgresql://sga@localhost:5433/sga LLM_PROVIDER=fake
uv run sga-db upgrade                                  # migrations + checkpoint tables
uv run uvicorn social_growth_agent.api.app:create_app --factory --port 8000
uv run python -m social_growth_agent.services.restart_demo   # kill/restart/resume, offline
TEST_DATABASE_URL=$DATABASE_URL uv run pytest          # full suite incl. DB tests (-m db: only those)
```

| Method | Path | |
|---|---|---|
| `POST` | `/runs` | start a run (202); body: `account`, `strategy`, optional `config` |
| `GET` | `/runs?status=&limit=` | list runs |
| `GET` | `/runs/{id}` | status, current node, research summary, posts, candidates with critiques, review state, usage, estimated cost, failure |
| `GET` | `/runs/{id}/usage` | usage counts with `at_run_pricing` and `at_current_pricing` estimates |
| `GET` | `/reviews/pending` | runs awaiting review with their candidates and critiques |
| `POST` | `/runs/{id}/review` | `approve` / `reject` / `edit` / `regenerate` (+ `note`), 202 |
| `POST` | `/runs/{id}/resume` | continue a `stalled` run from its checkpoint (never automatic) |
| `POST` | `/runs/{id}/publish` | record a publication intent for an approved candidate (202); optional `scheduled_for`. Never posts here |
| `GET` | `/publications/{id}` | publication status, schedule, attempts, post id and URL |
| `GET` | `/publications?status=&run_id=` | list publications |
| `POST` | `/publications/{id}/retry` | queue a `failed` publication again, where the category allows it |
| `POST` | `/publications/{id}/resolve` | close an `unknown` publication with what a human established |
| `POST` | `/publications/{id}/cancel` | withdraw an unclaimed `scheduled`/`ready` publication |
| `GET` | `/publications/{id}/metrics` | metric snapshots by target age, with capture delay, actual age and on-target flag |
| `GET` | `/publications/{id}/lineage` | publication → candidate, critiques, approval → findings → source posts → strategy, and metrics |
| `POST` | `/publications/{id}/analytics/backfill` | create missing snapshot jobs for a published post (202; idempotent; `?dry_run=true`) |
| `GET` | `/posts?since=&until=&account_id=&status=&limit=` | published posts with their latest snapshot |
| `GET` | `/analytics/jobs?status=&publication_id=` | snapshot jobs and their attempts; `GET /analytics/jobs/{id}` for one |
| `POST` | `/analytics/jobs/{id}/retry` | queue a `failed` job again where the category allows it |
| `GET` | `/health` | app and database status |

```bash
curl -s -X POST localhost:8000/runs -H 'content-type: application/json' -d '{
  "account": {"id": "acct_demo", "handle": "@agentic_builder", "niche": "AI engineering"},
  "strategy": {"account_id": "acct_demo", "pillars": ["agent engineering", "llm evaluation"],
               "tone": "practical", "target_audience": "engineers shipping LLM features"}}'
curl -s localhost:8000/reviews/pending
curl -s -X POST localhost:8000/runs/$RUN/review -H 'content-type: application/json' \
  -d '{"action": "regenerate", "note": "Lead with a concrete number.", "reviewer": "me"}'
curl -s -X POST localhost:8000/runs/$RUN/review -H 'content-type: application/json' \
  -d '{"action": "approve", "candidate_id": "'$CAND'", "reviewer": "me"}'
curl -s localhost:8000/runs/$RUN/usage
```

Errors: 404 unknown run, publication or candidate, 409 invalid state (e.g. reviewing a run that
is not awaiting review, an unknown candidate, a spent regeneration budget, a publication that
already exists), 422 invalid payload or an out-of-bounds schedule, 503 database or provider
unavailable. Details: [docs/DATABASE.md](docs/DATABASE.md).

### Publishing approved content

Approval is content approval only: it never posts. Publishing is a separate explicit request
that records one durable intent, and a worker makes the single platform call.

```bash
# the whole lifecycle offline and free (mock publisher: nothing is posted anywhere)
DATABASE_URL=postgresql://sga@localhost:5433/sga \
  uv run python -m social_growth_agent.services.publish_demo

curl -s -X POST localhost:8000/runs/$RUN/publish -H 'content-type: application/json' \
  -d '{"candidate_id": "'$CAND'", "requested_by": "me"}'        # 202, status "ready"
uv run sga-publisher-worker --once                               # the only call to the platform
curl -s localhost:8000/publications/$PUB                         # published, with the post URL
```

`sga-publisher-worker` polls every `PUBLISHER_POLL_SECONDS`, claims due publications with
`FOR UPDATE SKIP LOCKED` and a lease (so two workers never publish the same row), and writes a
started attempt row before each call. Outcomes X cannot confirm (a timeout after sending, a
5xx, a worker dying mid-call) become `unknown` and are **never** retried automatically; a human
resolves them. A rate limit (429) with a reset time goes back to `ready` behind
`retry_not_before`: the worker skips it until the reset and retries it then, without sleeping,
bounded by `PUBLISH_MAX_ATTEMPTS`. Auth failures wait for a manual retry. Exactly-once delivery
is not claimed: `POST /2/tweets` has no idempotency key.

### Collecting metrics

```bash
# the whole analytics lifecycle offline and free (mock provider, stepped clock)
DATABASE_URL=postgresql://sga@localhost:5433/sga \
  uv run python -m social_growth_agent.services.analytics_demo

uv run sga-analytics-worker --once                               # collect due snapshots
uv run sga-analytics-worker backfill --publication $PUB          # explicit; never on startup
curl -s localhost:8000/publications/$PUB/metrics
```

`ANALYTICS_PROVIDER=x` reads `GET /2/tweets?ids=` with the read-only `X_BEARER_TOKEN` (each
returned post is a billed post read). The live check is opt-in and read-only:
`RUN_LIVE_X_ANALYTICS_TESTS=1 ... python -m social_growth_agent.services.x_analytics_demo <post-id>`,
and it only reads posts this application published. See [docs/ANALYTICS.md](docs/ANALYTICS.md).

### Evaluating performance and updating strategy

```bash
# the whole Phase 7 loop offline and free (synthetic snapshots, fake Strategy Agent)
DATABASE_URL=postgresql://sga@localhost:5433/sga_scratch \
  uv run python -m social_growth_agent.services.strategy_demo

curl -s -XPOST localhost:8000/evaluations \
  -d '{"account_id": "acct_1", "snapshot_age": "24h"}' -H 'content-type: application/json'
curl -s -XPOST localhost:8000/evaluations/$EVAL/proposals -d '{}' -H 'content-type: application/json'
curl -s -XPOST localhost:8000/strategy-proposals/$PROP/approve -H "Authorization: Bearer $STRATEGY_REVIEW_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"reviewer": "me", "summary": "try it", "proposal_sha256": "...", "evaluation_digest": "..."}'
curl -s -XPOST localhost:8000/strategies/$STRAT/activate -H "Authorization: Bearer $STRATEGY_REVIEW_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"version": 2, "expected_active_version_id": "sv_...", "reviewer": "me"}'
```

Evaluations never call a model or X. Proposals are off by default (`STRATEGY_PROPOSER`);
approval and activation need `STRATEGY_REVIEW_TOKEN`. New runs pick up the activated
version with `"use_active_strategy": true`. Strategies used by earlier runs can be
registered explicitly with `uv run sga-strategy import-runs --dry-run` (then without
`--dry-run`); conflicting bodies are reported, never merged. See
[docs/STRATEGY_EVALUATION.md](docs/STRATEGY_EVALUATION.md).

### Running an experiment

```bash
# six offline scenarios on SYNTHETIC data (positive, null, insufficient data, attrition,
# missing impressions, early unblinding); nothing posted, nothing adoptable
DATABASE_URL=postgresql://sga@localhost:5433/sga_scratch \
  uv run python -m social_growth_agent.services.experiment_demo

curl -s -XPOST localhost:8000/experiments/feasibility -H 'content-type: application/json' \
  -d @design.json        # duration and power tables for a design; stores nothing
curl -s -XPOST localhost:8000/experiments -H "Authorization: Bearer $STRATEGY_REVIEW_TOKEN" \
  -H 'content-type: application/json' -d @experiment.json   # full pre-registration incl. design
curl -s -XPOST localhost:8000/experiments/$EXP/runs -H "Authorization: Bearer $STRATEGY_REVIEW_TOKEN" \
  -H 'content-type: application/json' -d '{"account": {...}, "reviewer": "me"}'
curl -s localhost:8000/experiments/$EXP/analysis              # funnel; outcomes masked while open
```

Every write needs `STRATEGY_REVIEW_TOKEN`, because a run can call the LLM and X. Runs still
pause for human review and publish only on request; conclusion and adoption are explicit.
Plan for months at personal-account volume. See [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

### Finding topics (content discovery)

```bash
# discovery to a reviewed draft, one manual-only @grok question exported and registered,
# then a deletion request; fixtures, fake agents, mock verifier: nothing posted, free
DATABASE_URL=postgresql://sga@localhost:5433/sga_scratch \
  uv run python -m social_growth_agent.services.discovery_demo

uv run sga-discovery plan --account acct_1           # planned calls and max X read cost
SGA_REVIEW_TOKEN=$STRATEGY_REVIEW_TOKEN uv run sga-discovery run --account acct_1 --by me
uv run sga-discovery opportunities $SESSION           # ranked, with reasons and intents
curl -s -XPOST localhost:8000/discovery/opportunities/$OPP/runs \
  -H "Authorization: Bearer $STRATEGY_REVIEW_TOKEN" -H 'content-type: application/json' \
  -d '{"account": {...}, "intent": "news_commentary", "reviewer": "me"}'
uv run sga-discovery purge-expired                    # daily: X text past its retention
```

Sessions use bundled fixtures until `DISCOVERY_SOURCES=live`. Mentions are never posted by
the system; set `X_ACCOUNT_USER_ID` to register posts you published by hand. See
[docs/CONTENT_DISCOVERY.md](docs/CONTENT_DISCOVERY.md).

### Live publishing (opt-in, creates ONE real post)

```bash
RUN_LIVE_X_PUBLISH_TESTS=1 CONFIRM_LIVE_X_PUBLISH=YES PUBLISHER_PROVIDER=x \
  X_PUBLISH_API_KEY=... X_PUBLISH_API_SECRET=... \
  X_PUBLISH_ACCESS_TOKEN=... X_PUBLISH_ACCESS_TOKEN_SECRET=... \
  DATABASE_URL=postgresql://sga@localhost:5433/sga \
  uv run python -m social_growth_agent.services.x_publish_demo
```

Both gates plus a typed `publish` on the terminal are required; credentials being configured is
never enough, ordinary tests never post, and nothing is deleted afterwards. These four
user-context credentials are separate from the read-only `X_BEARER_TOKEN` used for research —
an app-only token cannot post. See [docs/PUBLISHING.md](docs/PUBLISHING.md).

### Real LLM workflow (opt-in, costs money)

```bash
export OPENAI_API_KEY=sk-...           # or put it in .env
export OPENAI_MODEL=gpt-4.1-mini       # optional
RUN_LIVE_LLM_TESTS=1 uv run python -m social_growth_agent.services.live_demo
RUN_LIVE_LLM_TESTS=1 uv run pytest -m live
```

The live demo runs research → 3 candidates → critique → retry if needed → pause at human review,
and prints a trace like this (from the deterministic demo; ids and text vary):

```
[generate] attempt=1
    candidate cand_e07d… [number, 87 chars]: … This is guaranteed to double your reach.
[critic]
    critique cand_e07d…: revise (model said revise, score=0.40, factual_risk=high) issues=['unsupported_claim']
  route: critic -> generate
[generate] attempt=2
    candidate cand_35cb… [contrarian, 66 chars] revises=cand_e07d…: … Here's what worked.
[critic]
    critique cand_35cb…: pass (model said pass, score=0.80, factual_risk=low) issues=[] violations=[]
  route: critic -> request_review
[request_review]
    status: awaiting_review
  route: request_review -> PAUSED for human review
```

### Live X research (opt-in, consumes X API usage)

Research uses synthetic fixture posts unless `RESEARCH_PROVIDER=x` is set. The X provider calls
the official X API v2 recent-search endpoint with an app-only Bearer token (read-only), fetches
one small page (`X_MAX_RESULTS_PER_QUERY`, default 10) and never paginates. Retrieval attempts
per run are capped by `X_MAX_QUERIES_PER_RUN` (default 1); with a higher budget, a thin sample
(fewer than 5 posts) is retried with a deterministically broadened query. A rate limit (HTTP 429) fails the run
with the reset time recorded; nothing sleeps or retries automatically. Your query is kept
verbatim; `-is:retweet` is appended to a separate effective query.

```bash
export X_BEARER_TOKEN=...              # or put it in .env (git-ignored)
RUN_LIVE_X_TESTS=1 uv run python -m social_growth_agent.services.x_research_demo "AI agents lang:en"
RUN_LIVE_X_TESTS=1 uv run pytest -m live tests/live/test_x_live.py
```

The demo prints the query, request count, posts fetched, a one-line summary per post, findings
with their evidence ids (`x_<post id>`), and continues through generation and critique (OpenAI if
`OPENAI_API_KEY` is set, otherwise the deterministic fake agents, and it says which). No test in
the normal suite needs credentials or network access.

In code:

```python
from social_growth_agent.config import AppSettings
from social_growth_agent.services import WorkflowService
from social_growth_agent.services.factory import build_dependencies

service = WorkflowService(build_dependencies(AppSettings()))  # reads OPENAI_* from env
run = service.start_run(account, strategy)  # pauses at human review
run = service.submit_review(run.state.run_id, decision)  # approve / reject / edit / regenerate
```

## Layout

```
src/social_growth_agent/
  config.py       settings from env / .env
  models/         domain objects (Pydantic, immutable)
  discovery/      source registry, feed parsing, clustering, ranking, claims, audience (pure, deterministic)
  policies/       content policy (hard rules incl. mentions, @grok, grounding), critic gate, human edit policy, research + publish rules, strategy changes
  graph/          state, nodes, pure routing, builder, checkpoint serialization
  agents/         research, content, critic, strategy (+ evidence digest), prompts/*.md, deterministic fakes
  providers/      LLM + social-platform protocols, OpenAI adapter, x/ (research, publishing, analytics, post lookup), discovery sources (feeds, GitHub, HN, X), mocks/
  services/       WorkflowService, RunService, PublicationService, PublisherWorker, AnalyticsWorker, Evaluation/StrategyService, ExperimentService + synthetic seed, DiscoveryService + evidence replay + sga-discovery, review auth, runtime, demos
  persistence/    SQLAlchemy tables, Alembic migrations, recorder, ledgers, publications, analytics, lineage, evaluations, strategies, experiments, discovery, retention, checkpointer, sga-db
  analytics/      observed derived metrics, strategy evaluations and experiment analysis (pure, deterministic)
  accounting/     price list (pricing.toml) and cost estimates
  api/            FastAPI app: run, review, resume, usage, publication, analytics, evaluation, strategy, experiment and discovery endpoints
  evaluation/     critic evaluation harness
  observability/  structured logging, timing
tests/
docs/             ARCHITECTURE.md, DATABASE.md, PUBLISHING.md, ANALYTICS.md, STRATEGY_EVALUATION.md, EXPERIMENTS.md, CONTENT_DISCOVERY.md, API.md, ROADMAP.md
discovery.toml    content discovery source registry (sources, vocabulary, weights, windows)
pricing.toml      prices for cost estimates (blank by default; never in code)
docker-compose.yml  local PostgreSQL only
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.
