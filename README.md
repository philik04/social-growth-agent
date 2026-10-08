# social-growth-agent

An agentic orchestration system that grows a creator's or founder's presence on X by running a
closed loop: **research → generate → critique → human approval → publish → analyze → update strategy**.

> Status: **Phase 5, publishing.** Approved content can be published to X, as a separate
> explicit request: one durable publication intent, a lease-based worker that makes exactly
> one platform call per attempt, scheduling, and an explicit `unknown` state for outcomes X
> cannot confirm (never retried on its own). Runs live in PostgreSQL and survive process
> restarts; a REST API starts runs, lists pending reviews and takes approve / reject / edit /
> regenerate-with-notes decisions. Research can run on live X posts (opt-in); every finding
> cites the X post ids that support it, checked in code and by database constraints. Usage is
> recorded per call and priced as labelled estimates from a config file. See
> [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/PUBLISHING.md](docs/PUBLISHING.md).

## Why this is more than a multi-agent chatbot

Most "multi-agent" demos chain prompts and hope. This project treats content generation as a
**stateful, auditable workflow**:

- **Graph, not a chain.** A LangGraph state machine with conditional routing and bounded loops.
  The critic can send drafts back for regeneration, but only `max_generation_attempts` times.
- **Typed state and typed agent contracts.** Every node reads and writes a validated Pydantic
  state. Agents return structured objects (`Critique{verdict, score, risks, issues}`), not prose.
  Output that breaks its contract fails the run loudly instead of leaking downstream.
- **LLMs judge, code decides.** The critic's LLM recommends. Deterministic policy
  (`ContentPolicy`, e.g. max 280 characters) has the final word, and a policy object
  (`CriticGate`) decides whether the batch may go to a human. The model never picks the next node.
- **Human in the loop by design.** The graph genuinely pauses before publication (`interrupt()`
  plus a checkpointer) and resumes with an approve / reject / edit / regenerate decision. A human
  edit is a new candidate that must pass critique again before it can be approved.
- **Retries where they belong.** Transient provider failures are retried by a node retry
  policy. Contract violations are not retried; they are recorded and routed to a failure state.
- **Traceability.** Every candidate records the run, attempt, strategy version and research
  findings behind it, and every critique points at its candidate. This chain will extend through
  approval, publishing and metrics, which is what makes the learning loop measurable.
- **Swappable edges.** LLMs and social platforms sit behind small protocols. Mocks make the
  whole engine testable without credentials, and a real adapter is one new class.
- **Evaluation from day one.** A critic evaluation harness already exists, so model or prompt
  changes can be gated on agreement with labelled cases.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync
cp .env.example .env                                 # optional; never commit real keys
uv run python -m social_growth_agent.services.demo   # deterministic fake agents, no key needed
uv run pytest                                        # tests
uv run ruff check . && uv run ruff format --check .  # lint + format
uv run mypy                                          # strict type check
```

### Persistent API (PostgreSQL)

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
  policies/       content policy (hard rules), critic gate, human edit policy, research + publish rules
  graph/          state, nodes, pure routing, builder, checkpoint serialization
  agents/         research, content, critic, prompts/*.md, deterministic fakes
  providers/      LLM + social-platform protocols, OpenAI adapter, x/ (research + publishing), mocks/
  services/       WorkflowService, RunService, PublicationService, PublisherWorker, runtime, demos
  persistence/    SQLAlchemy tables, Alembic migrations, recorder, usage/operation ledgers, publications, checkpointer, sga-db
  accounting/     price list (pricing.toml) and cost estimates
  api/            FastAPI app: run, review, resume, usage and publication endpoints
  evaluation/     critic evaluation harness
  observability/  structured logging, timing
tests/
docs/             ARCHITECTURE.md, DATABASE.md, PUBLISHING.md, ROADMAP.md
pricing.toml      prices for cost estimates (blank by default; never in code)
docker-compose.yml  local PostgreSQL only
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.
