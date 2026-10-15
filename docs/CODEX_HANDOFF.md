# Codex handoff: operate the existing system

## Baseline and scope

Reviewed baseline: `main` at `e3325a4`, repository `philik04/social-growth-agent`.
GitHub CI is green at this commit according to the operator; this documentation
update did not query GitHub. Phases 1–9, main-message clarity review (`dbb00a0`)
and explicit primary-source enrichment (`e3325a4`) are implemented. Repository
Alembic head remains `0007`; enrichment and clarity required no migration.
Verify the actual checkout and database revision before authorized operation.

The priority is using the existing discovery → review → publication → measurement
loop. Phase 10 is outside scope. This is reusable project guidance, not a record
of a particular account, credential setup, publication or host schedule. An
approved strategy may be an inline pilot input; registration and activation are
separate actions and are not prerequisites for the inline API flow.

The enrichment commit was independently validated against the preceding HEAD:
736 non-live tests passed, including PostgreSQL integration tests; five live tests
were deselected. Ruff, formatting, strict mypy and the offline lockfile check
passed. This records prior validation, not tests rerun during this documentation
update. It does not establish credential validity, current balances or source uptime.

## Architecture

Python 3.12+, uv and a `src/` package layout; reproduce dependencies with
`uv sync --locked`. PostgreSQL 16 stores domain projections, immutable versions,
usage, operation ledgers, queues and LangGraph checkpoints. FastAPI dispatches
runs to an executor; PostgreSQL leases and row locks support separate workers.
There is no Redis/Celery requirement and no additional enrichment or clarity agent.

```text
discovery → ranked opportunity → optional explicit enrichment → selected opportunity
                                                             ↓ stored evidence replay
retrieve → research → generate → critic → request_review → human_review
                         ↑ bounded revisions                   ├─ approve/reject → end
                         └─ human regeneration                 └─ edit → critique_edit → review

approved candidate → separately requested publication intent → publisher → publication
approved candidate → manual export → human posts → verified registration → publication
publication → analytics jobs → collection → descriptive evaluation → reviewed proposal
                                                                    ↓ inactive version
                                                          separate strategy activation
```

LLMs return typed judgments; deterministic code owns routing, budgets, hard rules
and approval eligibility. The agents are application classes over a provider
protocol and the OpenAI Responses adapter, not an OpenAI Agents SDK workflow.
SDK retries are disabled; LangGraph normally permits up to three attempts per
transiently failing model node. A generation-cycle limit does not replace that
retry policy. Checkpoint resumption can repeat a call interrupted before durability.

| Area | Main locations under `src/social_growth_agent/` |
|---|---|
| Contracts and agents | `models/`, `agents/`, `agents/prompts/` |
| Workflow and state | `graph/`, `services/runs.py`, `services/workflow.py` |
| Runtime/configuration | `config.py`, `providers/factory.py`, `services/runtime.py` |
| X and OpenAI boundaries | `providers/x/`, `providers/openai_provider.py` |
| Discovery and replay | `discovery/`, `providers/discovery.py`, `services/discovery.py`, `services/replay.py` |
| Article enrichment | `providers/article.py`, `discovery/article.py`, `services/enrichment.py` |
| Persistence and deletion | `persistence/`, especially `discovery.py` and `retention.py` |
| Publication and metrics | `services/publications.py`, `publisher.py`, `analytics.py`, `analytics_worker.py` |
| Learning and experiments | `analytics/`, `services/evaluations.py`, `strategies.py`, `experiments.py` |
| HTTP and usage estimates | `api/`, `accounting/`; root `pricing.toml` and `discovery.toml` |

## Phase 9 evidence and primary-source enrichment

On-demand discovery reads configured RSS/Atom feeds, GitHub releases, HN Algolia
and official X search/account queries. Ranking, topic clustering, novelty,
shortlist diversity, claim hints and audience suggestions are deterministic.
Sources come from the registry; retrieved text cannot add sources or actions.
Discovery does not call models or publish. Request limits and registry limits
are distinct: use both the [bounded registry](../discovery.pilot.example.toml)
and [request example](../examples/pilot/discovery.request.example.json).

Non-X live discovery and live X research are independent settings. Selecting
live feeds while leaving X research mocked can produce synthetic sessions.
Providers do not silently fall back. Inspect fetch outcomes, usable evidence and
synthetic flags before selecting an opportunity. A primary publisher stating a
claim is not independent verification; title-only evidence cannot support detailed
specifications. Discussion and disagreement must be supported by actual evidence.

`POST /discovery/opportunities/{opportunity_id}/enrich` and `sga-discovery enrich`
require explicit authorization and review authentication. They fetch one selected
real, unpurged primary feed article whose canonical URL matches the linked item.
The fetch is real even when normal discovery providers are mock. It has no model
or X calls, no automatic crawl and no automatic retry.

The fetcher requires HTTPS, exact-host redirects (at most three), public DNS
addresses pinned into connections with the original TLS name, no environment
proxies, HTTP timeouts, a transfer time budget and a 2 MB decoded HTML limit.
A bounded robots-policy GET precedes the article; unavailable/disallowing policy
or storage opt-outs fail safely. OS DNS resolution and an in-progress HTTP read
are not strictly preempted by the elapsed-budget check. Extraction retains up to
12 literal technical excerpts within a compact 4,000-character evidence budget;
it does not archive the entire article or execute scripts.

Canonical URL, publisher, author/date when available, retrieval time, response and
content digests, and self-reported attribution remain traceable. Optional selected
excerpts must match literal source prose. A separate audited discovery session,
content-addressed item version and derived opportunity preserve the original item,
session and historical runs. Repeated requests reuse the prior audit, including
failed or unfinished attempts; only explicit refresh can request another completed
fetch. Select the new opportunity for generation. Existing evidence replay and
finding-evidence lineage use the new item without another X search or article GET.

All versions retain their platform/provider-item identity. Identity locks,
tombstones, replay guards and checkpoint redaction preserve deletion behavior,
including deletion during a fetch. Never restore purged evidence through enrichment
or silently rewrite a completed evidence record.

## Content, clarity and human review

Research validates supplied source references. Content cites validated findings
and states its intended main message in the first sentence of `rationale`.
The existing critic evaluates whether the post itself communicates that message
to the strategy's intended audience, across domains. The rationale or an article
cannot supply an explanation missing from the post.

An explicit `unclear` issue prevents PASS even with a high score; REJECT is never
upgraded. The original model recommendation and score remain recorded. This
check uses the existing critic call, with no separate clarity call. Clear
specialist terminology is allowed when appropriate for the intended audience.

Hard rules enforce format, length, mentions, and discovery URL/number grounding.
These surface checks do not prove every semantic claim. Human review must inspect
attribution, qualifications, hook labels and the actual takeaway. The length
check uses Python `len()` with a default 280 limit, not X's weighted URL/Unicode
counting. Keep posts conservative and verify platform presentation manually.

Approval requires a passing critique and never publishes. An edit creates a new
candidate and goes through `critique_edit`; regeneration reuses research. Preserve
old candidate metadata, critiques, checkpoints and published content. New prompts
and clarity policy do not retroactively regrade historical critiques.

## Publication, analytics and reviewed learning

Publication is a Phase 5 service outside the graph. Separate intents freeze the
approved text/hash and approval lineage. The publisher checks policy again before
a call. Ambiguous outcomes become `unknown` and must not be retried without human
evidence from X; exactly-once delivery is not guaranteed. Any mention, including
full-width variants and eligible `@grok` questions, is manual-only. Ordinary drafts
forbid mentions; export/verified registration does not waive content policy.

Successful publication or verified manual registration normally creates 1h/24h/72h
analytics jobs. Phase 6 stores request/attempt ledgers, raw available public counts,
provider creation time and actual capture time/age. Missing metrics remain NULL.
Late observations cannot become historical on-time measurements through backfill.

A general worker cycle can touch unrelated records. The analytics Python method
`run_once(publication_id=...)` filters claims but still globally sweeps expired
collecting jobs. Its CLI has no publication-filtered collection flag; the
`backfill --publication` option creates jobs, not an isolated collection run.
A strictly bounded pilot needs a separately reviewed invocation allowing only
its publication and approved due jobs, with durable attempt limits and no global
sweep. Host-specific scripts/schedules are local operational artifacts, not a
portable feature supplied by these examples. Preserve existing schedules.

Evaluations are descriptive, not causal. Strategy proposals may change only
`preferred_hooks` and `length_guidance`; adoption creates an inactive immutable
version, and activation is separate. A/B experiments preserve preregistration,
random assignment, masked outcomes, irreversible unblinding and synthetic-adoption
refusal. Wait for sufficient real age-matched evidence before learning from results.

## Security, configuration and known limitations

Core run/review/publication routes lack authentication. The review token protects
selected strategy/discovery/experiment actions, not the entire API.
`APP_ENVIRONMENT=production` does not add general authentication. Use one process
bound to `127.0.0.1`; no public tunnel or deployment. Startup can mark queued/running
runs stalled globally. Embedded publishing stays disabled; do not restart an API
as part of a read-only inspection.

The default LLM is OpenAI, not fake. Research, discovery, publishing and analytics
providers are independently selected. Mock publication/metrics are synthetic;
never use them to represent real publication or engagement. Configure secrets
privately using names in [the settings example](../.env.example); never read or
print credential files for inspection. Prices in `pricing.toml` are estimates,
not billing controls. Missing rates mean unknown cost, not zero. Recheck the chosen
model, entitlements, identity, write scope, account-specific rates and provider
spending caps before live work; this handoff makes no claim about their current state.

Retention and explicit source deletion mutate evidence/checkpoints and store
tombstones. The default X text retention window is 30 days; the repository does
not install a sweep or deletion-monitor schedule. Prompt deletion handling is
separate from expiry. Purges do not comprehensively erase all derived findings,
drafts or backups; assess those separately. Local Compose uses loopback trust
authentication; backup/restore and host availability remain operator responsibilities.

## Verification and next steps

Read [AGENTS.md](../AGENTS.md) and [Controlled pilot](CONTROLLED_PILOT.md), followed
by [discovery](CONTENT_DISCOVERY.md), [publishing](PUBLISHING.md),
[analytics](ANALYTICS.md), [database](DATABASE.md), [API](API.md),
[strategy evaluation](STRATEGY_EVALUATION.md) and [experiments](EXPERIMENTS.md).
The root `pilot/` directory, live pilot registry and original account-specific
notes are ignored. Public templates contain no credentials, account bindings,
execution records or host schedules. Do not overwrite existing local inputs.

For offline tests without `.env` loading, use the existing environment:

```bash
.venv/bin/python - <<'PY'
import os
import pytest
from social_growth_agent.config import AppSettings
for name in tuple(os.environ):
    if name.lower() in AppSettings.model_fields or name.startswith(
        ('RUN_LIVE_', 'CONFIRM_LIVE_', 'TEST_DATABASE_URL', 'SGA_REVIEW_TOKEN')
    ):
        os.environ.pop(name, None)
AppSettings.model_config['env_file'] = None
raise SystemExit(pytest.main([
    '--ignore=tests/db', '--ignore=tests/live',
    '-m', 'not db and not live', '-rs',
]))
PY
```

When authorized, run Ruff, formatting, strict mypy and `uv lock --check` as well.
PostgreSQL tests require separate authorization: fixtures create, migrate,
truncate and drop disposable databases and may create roles. Live tests and
publishing demos are never routine verification.

Next steps are operational: review a bounded discovery request, inspect or enrich
its evidence, authorize one generation workflow, review its clarity and facts,
confirm one publication separately, and collect only approved snapshots. Do not
activate strategies, create background jobs or start a new product phase merely
to follow this guide.
