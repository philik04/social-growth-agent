# API index

Primary source enrichment: `POST /discovery/opportunities/{id}/enrich` (review
token), body `{item_id, url, requested_by, excerpts?, refresh?}`. Returns a separate
audited session and derived opportunity, or a failed session with no new evidence.
Duplicates return the existing result without a fetch. It never starts a run,
calls a model/social API or overwrites the original evidence. See
[content discovery](CONTENT_DISCOVERY.md#explicit-primary-source-enrichment).

All endpoints of `social_growth_agent.api.app`, by phase. The OpenAPI document at
`/docs` (or `/openapi.json`) has the request and response schemas.

**Token** means `Authorization: Bearer <STRATEGY_REVIEW_TOKEN>`: 401 without it, 403
with a wrong one, 503 when no token is configured (unless unauthenticated review is
explicitly enabled for local development). It proves possession of the shared token, not
identity; the submitted name is recorded next to the auth method.

Conventions: `GET` endpoints never call a model or a platform and never write. Errors
are `{"detail": ...}` with 404 (unknown), 409 (refused in this state), 422 (invalid
request) or 503 (not configured). Secrets never appear in responses.

## Runs and review (Phases 1–4)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | | service version and database reachability |
| POST | `/runs` | | start a run (202); stops at human review |
| GET | `/runs`, `/runs/{id}` | | summaries; detail with evidence, candidates, review, usage, publications |
| GET | `/runs/{id}/usage` | | usage and cost at the run's pricing |
| POST | `/runs/{id}/review` | | approve, edit, reject or regenerate (202) |
| POST | `/runs/{id}/resume` | | resume a run stopped by a crash or restart (202) |
| GET | `/reviews/pending` | | runs waiting for review |

## Publishing (Phase 5)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/runs/{id}/publish` | | record a publication intent for an approved candidate (202); the worker posts. Refuses any `@mention` (manual only) and exported candidates |
| GET | `/publications`, `/publications/{id}` | | |
| POST | `/publications/{id}/retry`, `/resolve`, `/cancel` | | operator actions (202) |

## Analytics (Phase 6)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/publications/{id}/metrics` | | snapshots |
| GET | `/publications/{id}/lineage` | | publication back to candidate, approval, findings, evidence, strategy; Phase 9 adds `discovery` (opportunity, evidence, export, manual registration) |
| POST | `/publications/{id}/analytics/backfill` | | create missing snapshot jobs |
| GET | `/posts` | | published posts with metrics |
| GET | `/analytics/jobs`, `/analytics/jobs/{id}` | | |
| POST | `/analytics/jobs/{id}/retry` | | |

## Strategy evaluation (Phase 7)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/evaluations` | | deterministic evaluation (201) |
| GET | `/evaluations`, `/evaluations/{id}`, `/evaluations/{id}/observations` | | |
| POST | `/evaluations/{id}/proposals` | | ask the Strategy Agent for a proposal (201) |
| GET | `/strategy-proposals`, `/strategy-proposals/{id}` | | |
| POST | `/strategy-proposals/{id}/retry` | | |
| POST | `/strategy-proposals/{id}/approve`, `/reject` | token | approval creates a new version, not active |
| POST | `/strategies/versions` | token | register a version |
| GET | `/strategies/{id}/versions` | | history |
| POST | `/strategies/{id}/activate` | token | compare-and-set activation |
| GET | `/accounts/{id}/active-strategy` | | |

## Experiments (Phase 8)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/experiments/feasibility` | | power and duration check; writes nothing |
| POST | `/experiments` | token | create a design (201) |
| GET | `/experiments`, `/experiments/{id}` | | |
| POST | `/experiments/{id}/approve`, `/start`, `/stop`, `/cancel`, `/unblind`, `/conclude` | token | lifecycle |
| POST | `/experiments/{id}/runs` | token | an assigned run (202); refuses `grok_question` |
| GET | `/experiments/{id}/assignments`, `/analysis`, `/lineage`, `/usage` | | |
| POST | `/experiments/{id}/adopt` | token | adopt the winning arm as a new strategy version (201) |

## Content discovery (Phase 9)

See [CONTENT_DISCOVERY.md](CONTENT_DISCOVERY.md).

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/discovery/sources` | | the registry as loaded |
| POST | `/discovery/plan` | | planned calls and maximum X read cost; calls nothing |
| POST | `/discovery/sessions` | token | run one bounded session (201) |
| GET | `/discovery/sessions`, `/discovery/sessions/{id}` | | session, fetch ledger, summary |
| GET | `/discovery/sessions/{id}/opportunities` | | ranked shortlist |
| GET | `/discovery/sessions/{id}/audience` | | read-only audience shortlist |
| GET | `/discovery/opportunities/{id}`, `/discovery/opportunities/{id}/items` | | an opportunity and its evidence items (purged items keep ids and URLs only) |
| POST | `/discovery/opportunities/{id}/dismiss` | token | |
| POST | `/discovery/opportunities/{id}/runs` | token | a normal run that replays the evidence (202); body: `account`, `intent`, `reviewer`, optional `strategy` |
| POST | `/runs/{id}/candidates/{cid}/export` | token | approved text to post by hand, with an x.com compose link (201); idempotent |
| POST | `/runs/{id}/candidates/{cid}/manual-publication` | token | register the post you published (URL or id); one verification read; idempotent (201); 503 without `X_ACCOUNT_USER_ID`/`X_ACCOUNT_USERNAME` |
| POST | `/discovery/purge` | token | remove the text of deleted platform items everywhere |
