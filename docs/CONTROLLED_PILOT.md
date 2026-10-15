# Controlled pilot: discovery to measured publication

## Scope and authorization

This guide uses Phases 1–9, explicit primary-source enrichment and main-message
clarity review. It adds no agent, migration or background service. Commands are
instructions for a later authorized operation, not actions performed by reading
or preparing this guide. Each live stage needs its own explicit allowance:
selected account, exact inputs, paid requests, database writes and stopping point.
Keep account configuration, execution records and host schedules local.

The [handoff](CODEX_HANDOFF.md) describes the architecture and known limitations.
The [registry example](../discovery.pilot.example.toml) and
[request example](../examples/pilot/discovery.request.example.json) contain public
sources and obvious example identifiers, not an account binding or credentials.

## 1. Prepare a safe local environment

Use Python 3.12+, uv and PostgreSQL 16. Run commands from the repository root.
Install locked dependencies with `uv sync --locked`. The Compose database binds
to loopback and uses local trust authentication; protect host access and backups.
Inspect the actual migration revision before operation; the repository head is
`0007`. Database creation/migration and API startup require authorization. Never
reset an operational database to prepare a pilot.

Use setting names in [the environment example](../.env.example) and `config.py`.
The operator supplies secrets privately; do not print, source, search or read
credential files during audits, enable shell tracing or dump the environment.
For offline inspection, `AppSettings(_env_file=None)` disables file loading but
still reads process environment settings; use an isolated process without live
credentials/provider overrides. The [handoff runner](CODEX_HANDOFF.md#verification-and-next-steps)
shows how to disable `.env` loading for tests.

Create local copies only if they do not already exist:

```bash
mkdir -p pilot
cp -n discovery.pilot.example.toml discovery.pilot.toml
cp -n examples/pilot/discovery.request.example.json pilot/discovery.request.json
```

Preserve existing copies and review them separately. Replace `example_account`
and `example_operator` privately with the intended internal account id and
reviewer. Keep account id, strategy account id and discovery request consistent;
never reuse a demo identity. Configure the X username without its prefix, but
leave the numeric user id unset until verified. Presence of publishing tokens
and a configured username does not establish OAuth ownership.

Before enabling publication, separately verify the user-context identity with an
appropriate official endpoint and confirm Read and Write app permissions and the
access token's issuance after any permission changes. Identity verification is
an external operation requiring its own authorization; an app-only bearer token
does not identify the posting user. Do not derive identity from token text.

Use these explicit offline defaults before live stages:

```bash
export LLM_PROVIDER=fake RESEARCH_PROVIDER=mock DISCOVERY_SOURCES=mock
export PUBLISHER_PROVIDER=mock ANALYTICS_PROVIDER=mock
export PUBLISHER_EMBEDDED_WORKER=false STRATEGY_PROPOSER=disabled
export STRATEGY_REVIEW_ALLOW_UNAUTHENTICATED=false APP_ENVIRONMENT=production
export DISCOVERY_REGISTRY_FILE=discovery.pilot.toml
```

Mock publication and metrics are synthetic records, not a dry run against real
posts. Do not enqueue or collect mock outcomes for a real publication. At each
live stage, the approved process alone enables the necessary real provider:

| Stage | Provider selection |
|---|---|
| Discovery | `DISCOVERY_SOURCES=live`, `RESEARCH_PROVIDER=x`; keep LLM fake |
| Selected-opportunity generation | `LLM_PROVIDER=openai`; stored evidence replay supplies research |
| Verified manual registration/analytics | `ANALYTICS_PROVIDER=x` for the approved scoped invocation |
| API publication | `PUBLISHER_PROVIDER=x` only for the reviewed publication-specific invocation |

Keep embedded publishing disabled throughout. Check model access and structured
output support before authorizing generation; set `LLM_TEMPERATURE_ENABLED=false`
if the chosen model rejects temperature. Do not assume model visibility proves
feature support. Do not change an existing service's provider settings or restart
it without separate approval.

If no API is running and startup is approved, launch exactly one process:

```bash
uv run --locked uvicorn social_growth_agent.api.app:create_app --factory \
  --host 127.0.0.1 --port 8000 --workers 1
```

Startup can mark queued/running runs stalled globally. Check port ownership first;
no reload, additional process, public host, tunnel or public deployment. Core
run/review/publication routes lack authentication; the review token covers only
selected routes. `APP_ENVIRONMENT=production` does not secure the whole API.
Require database `ok` in `/health`, not just top-level status. Preparing this guide
does not authorize startup or disturb separately managed collection schedules.

## 2. Plan and run bounded discovery

The example selects two feeds, one GitHub releases source, one HN search and one
X recent-search source. Public endpoints are configured in the registry. The
registry caps the shortlist at five opportunities; it does not enforce request
counts on its own. The JSON request explicitly caps:

| Resource | Maximum per session |
|---|---:|
| X search requests | 1 |
| X posts | 10 |
| Expanded X users | 10 |
| Feed sources / items per feed | 2 / 10 |
| GitHub requests / returned items | 1 / 10 |
| HN requests / returned items | 1 / 10 |
| Returned opportunities | 5, from registry policy |

Set `X_MAX_RESULTS_PER_QUERY=10` and `X_MAX_QUERIES_PER_RUN=1` for the authorized
live-discovery process as additional bounds. Discovery providers do not paginate
or retry. X expands author objects in the single search response without a second
user lookup. Non-X redirects and conditional cache validation can add raw HTTP
exchanges; source-call counts are not a universal HTTP-request count. A warm cache
can reduce usage. Inspect synthetic flags to prevent mock/live mixtures.

With the authorized localhost API already running, planning makes no source/model
requests or discovery-session writes; the API runtime still needs its configured DB:

```bash
curl --fail-with-body -sS http://127.0.0.1:8000/discovery/plan \
  --json @pilot/discovery.request.json
```

Review exact sources, bounds and missing-price warnings. The CLI `plan` command
uses its default request limits; it does not consume this JSON request. Authorize
the exact JSON plan rather than inferring these limits from a CLI default.

Only after explicit approval for one live session and its database writes, supply
the privately configured review token through `SGA_REVIEW_TOKEN`. Supply headers
on stdin so their values do not enter curl's command-line arguments:

```bash
set +x
printf 'Authorization: Bearer %s\n' "$SGA_REVIEW_TOKEN" | \
  curl --fail-with-body -sS --header @- \
    --json @pilot/discovery.request.json \
    http://127.0.0.1:8000/discovery/sessions
```

Save the returned session id privately. Do not automatically repeat the POST on
an error or lost response: discovery creation has no client idempotency key.
Inspect session/fetch ledgers first. Report failed sources and missing evidence;
fewer than five useful opportunities is an acceptable outcome.

## 3. Select an opportunity and inspect evidence

Read `/discovery/sessions/{session_id}/opportunities`, then
`/discovery/opportunities/{opportunity_id}/items`. Select one suggested ordinary
intent and one topic. Check publisher, date, actual prose, canonical links,
synthetic/purged status, score components and freshness. Distinguish primary
self-reports from independent corroboration and real discussion. Do not invent
consensus, disagreement, specifications or performance claims from a headline.

Discovery uses active-strategy context when available. Without an active strategy,
ranking uses registry vocabulary; supplying an inline strategy later does not
retroactively rerank discovery. Registration and activation are separate approvals.

## 4. Enrich a primary source when evidence is insufficient

Stop before model generation when evidence is title-only or otherwise inadequate.
Obtain authorization to fetch the selected item's exact canonical article URL.
The review-token-protected API is
`POST /discovery/opportunities/{opportunity_id}/enrich`, with `item_id`, `url` and
`requested_by`; optional literal `excerpts` and explicit `refresh` are supported.
An authorized CLI equivalent, using privately selected variables, is:

```bash
uv run --locked sga-discovery enrich "$OPPORTUNITY_ID" \
  --item "$ITEM_ID" --url "$SOURCE_URL" --by "$REVIEWER"
```

The CLI requires `SGA_REVIEW_TOKEN`. Enrichment fetches real public HTML even when
ordinary discovery is mocked. It reads robots policy first, validates HTTPS/public
DNS and same-host redirects, limits time/size and performs no automatic retry.
An ordinary fetch is two GETs (robots plus article); up to three allowed redirects
can raise this to five. Access failures, storage opt-outs, incomplete HTML and
insufficient recognizable static prose fail without invented evidence.

Inspect the compact excerpts, dates, attribution and digests. Claims remain
self-reported unless independently supported. Failed or unfinished attempts are
audited and repeated identical requests do not fetch again. Explicit refresh can
request another completed fetch; it does not retry an unfinished attempt.

Successful enrichment returns a new opportunity and content-addressed item through
a separate audited session. Original evidence and runs stay intact. Use the new
opportunity for generation; replay links finding evidence to its item without
another article fetch or X query. A purged/deleted source cannot be restored by
refresh. Enrichment does not manufacture discussion or Grok eligibility.

## 5. Generate candidates and review clarity

After evidence approval, authorize one bounded paid workflow for the selected
opportunity. Prepare a private `StartRunFromOpportunity` body for
`POST /discovery/opportunities/{opportunity_id}/runs`:

- `account.id` must equal the opportunity's internal account id.
- Choose an `intent` present in its suggested intents.
- Use the approved inline `strategy` with matching account id when there is no
  active version. An active version takes precedence; verify which is used.
- Set `max_generation_attempts: 1` and `candidates_per_attempt: 3`.
- Set `reviewer` privately. Do not register or activate a strategy as a workaround.

The CLI `start-run` requires an active strategy; it cannot supply this inline
fallback. Stored discovery replay supplies the Research Agent's inputs with zero
new X requests. Then the existing Research, Content and Critic Agents run, with
at most three candidates requested (fewer may be returned).

A single generation cycle normally means three model calls, but default transient
node retries allow up to nine provider attempts. `max_generation_attempts: 1`
does not disable those retries. If the authorization permits only one provider
attempt per node, require a reviewed invocation supplying a one-attempt graph
retry policy before spending; no ordinary request field provides that setting.
Do not silently rerun a failed workflow or broaden its budget. Checkpoint resumption
and human edits/regeneration can incur additional calls and need explicit allowance.

Stop at `awaiting_review`. Read exact text, finding/source references, attribution,
critic recommendation, final verdict, issues and policy violations. A high score
cannot compensate for `unclear`: application code prevents PASS. The existing
critic assesses one understandable main message for the strategy audience, without
an extra clarity call. A technical post can keep necessary terms, but should make
its takeaway and relevance clear on first reading. Check that the declared hook
matches the text and that no unsupported benefits were added to make it clearer.

## 6. Human approval and mentions

Human review must confirm evidence, clarity, intended audience, hook and the
280-character application limit. The length check uses Python code points, not
X's weighted counting; manually inspect URLs/emoji/non-Latin text in the platform.
Approve only the selected passing candidate through the existing review action.
An edit creates a new candidate and must pass `critique_edit`; it does not replace
historical candidate metadata or critiques. Approval alone never publishes.

Ordinary drafts prohibit mentions. Every mention is manual-only at publishing,
including full-width variants. An optional `grok_question` requires genuine
supporting discussion and evidence-specific eligibility, a separate allowance,
one candidate and a useful specific question. It allows exactly one `@grok`, no
other mention. Manual export does not override grounding, critic or mention policy.

## 7. Explicit publication confirmation

Before a separate final publishing confirmation, verify the exact approved text,
latest passing critique, ownership/write scope, account identity, existing intents,
schedules and prior publication. Inspect unrelated intents without processing them.
Freeze the candidate and content hash; never alter its text after confirmation.

For manual publication, export the approved candidate first using the existing
Phase 9 export service/CLI. After the human confirms and posts the exact text from
the verified account, separately authorize manual registration. Registration makes
an X read with author expansion, checks ownership/text/time and records the
publication plus analytics jobs. Do not combine this with an API publication.

For API publication without mentions, use the existing Phase 5 publication service
through a separately reviewed invocation scoped to exactly the approved intent.
Recheck before execution, allow one POST attempt, disable automatic retries and
stop on mismatch. The general publisher CLI/`--once` cycle is not intent-specific;
this guide supplies no portable single-intent execution command. Keep embedded
publishing disabled and do not start a general worker for a one-post allowance.
If no safe scoped invocation is available, stop before posting. An `unknown`
outcome requires investigation on X before any retry or resolution.

Record confirmed provider id/URL, attempt outcome, usage and created analytics
jobs. Never treat a mock publication as confirmation that a real post exists.

## 8. Collect only the approved publication's analytics

Inspect the publication and existing 1h/24h/72h jobs before collecting anything.
Authorize exact job ids, due times and request budget. Use the real read-only
bearer-token analytics provider and official post lookup with `public_metrics`
and `created_at`; do not perform extra identity lookups. Leave publishing off.

A reviewed publication-specific one-shot invocation must enforce the job allowlist,
check actual time/status, use durable reservation and existing Phase 6 request/
attempt ledgers, permit at most one GET per scheduled snapshot, and refuse repeat
execution after its approved attempt. Preserve any already registered host jobs;
do not duplicate, replace or reschedule them as part of following this guide.

The general analytics CLI cannot collect a single publication by flag. Its
`backfill --publication` option schedules jobs. The Python worker's
`run_once(publication_id=...)` limits claims but still sweeps expired collecting
jobs globally. Neither is sufficient for an allowance forbidding unrelated job
mutations. Stop if the scoped invocation cannot meet those limits.

Collect only when due. Store actual collection time, provider creation time,
post age and capture delay; missing values remain NULL, never zero. Keep likes,
reposts, replies, quotes, bookmarks and impressions only when actually returned.
If several jobs are overdue, assess the permitted reads and usefulness first;
late snapshots cannot recreate earlier engagement. Never pin a fake historical
clock for live collection.

Host/Docker/PostgreSQL outages may delay or prevent capture. Durable attempt
reservation can intentionally consume an allowance on an ambiguous crash; do not
retry automatically. Review ledgers and captured results before requesting any
additional read. Keep collection schedules independent of API lifecycle and do
not start an unbounded worker or create recurring jobs through this guide.

## 9. Retention, deletion, costs and authentication

Retention sweeps and deletion requests change database/checkpoint contents.
Authorize them separately and review their scope. `sga-discovery purge-expired`
affects expired X evidence across the database, not just this pilot. Explicit
`sga-discovery purge` supports a platform/provider identity; use it for notified
deletions rather than waiting for expiry. Feed enrichment versions share the
original identity, so purges/tombstones cover versions together. Replay and
checkpoints must respect purged evidence. Derived prose and backups need separate
assessment; do not claim universal deletion of all copies.

Inspect `pricing.toml` before each spending approval. Unknown prices mean unknown
totals, not free calls. Do not infer rates from historical comments or modify
billing/prices without approval. Discovery's X allowance is at most
`10 * post_read_rate + 10 * user_read_rate`; generation, registration, publishing
and analytics have separate costs. Expanded users can be billed resources even
when returned in the same HTTP request. Model input/output prices and token usage
must be measured; output-token caps do not establish a total-dollar ceiling when
input is not hard-capped. A provider spending cap is an external safeguard, not
an application estimate or universal cross-provider budget.

Selected writes require the configured review token; CLI operations use
`SGA_REVIEW_TOKEN`. Reviewer strings do not establish personal identity, and a
review token does not protect unauthenticated core routes. Never log tokens or
place them in command-line arguments. Stop at each stage's agreed review point;
no likes, follows, replies, strategy activation or unapproved external operations
are part of this workflow.
