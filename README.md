# social-growth-agent

An agentic orchestration system that grows a creator's or founder's presence on X by running a
closed loop: **research → generate → critique → human approval → publish → analyze → update strategy**.

> Status: **Phase 2, real LLM agents.** Research, content and critic agents run on OpenAI
> structured outputs, behind a provider abstraction, while the graph keeps authority over routing,
> retries, review and failure. X is not integrated yet: research uses supplied sample posts.
> See [docs/ROADMAP.md](docs/ROADMAP.md).

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
  policies/       content policy (hard rules), critic gate, human edit policy
  graph/          state, nodes, pure routing, builder, checkpoint serialization
  agents/         research, content, critic, prompts/*.md, deterministic fakes
  providers/      LLM + social-platform protocols, OpenAI adapter, mocks/
  services/       WorkflowService, factory, trace printer, demo, live_demo
  api/            FastAPI app factory
  evaluation/     critic evaluation harness
  observability/  structured logging, timing
tests/
docs/             ARCHITECTURE.md, ROADMAP.md
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.
