# Content discovery (Phase 9)

Phase 9 finds things worth writing about, explains why, and hands a chosen topic to an
ordinary run. It adds no agent and does not change the LangGraph topology.

- A **discovery session** is a bounded, audited, on-demand sweep over the sources in
  `discovery.toml`. Nothing is scheduled, nothing paginates and nothing retries.
- The session stores normalized, deduplicated **evidence items** with provenance, a
  deterministic **opportunity shortlist** and a read-only **audience shortlist**. No
  model is called.
- A person picks an opportunity and a **content intent**. That starts a normal Phase 4
  run whose research step **replays the stored evidence** (0 external calls). Review,
  publishing, analytics and lineage work as before.
- Any draft that contains an `@mention`, including every `@grok` question, is
  **manual-only**: no code path posts it. You export it, post it yourself and register
  the post so it is measured like any other.

Offline demo (fixtures, fake agents, mock verifier; free):

```bash
DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.discovery_demo
```

## Workflow

1. `sga-discovery plan --account acct_1` (or `POST /discovery/plan`): the planned calls and
   the maximum X read cost. Calls nothing.
2. `sga-discovery run --account acct_1 --by you` (or `POST /discovery/sessions`, token):
   one bounded session.
3. `sga-discovery opportunities <session>`: the ranked shortlist with score components,
   reasons, claim hints, suggested intents and @grok eligibility.
   `GET /discovery/sessions/{id}/audience` lists accounts and discussions worth reading.
4. `POST /discovery/opportunities/{id}/runs` (token) with an intent: a normal run that
   pauses for human review. `POST .../dismiss` suppresses a topic for 30 days.
5. Review as always (approve, edit, reject, regenerate).
6. Publish through `POST /runs/{id}/publish`, or, for a manual-only draft, export it,
   post it yourself and register the post (see below).
7. Analytics, evaluations and lineage work unchanged. Intent is recorded on candidates.

## Sources

Sources are configuration, never content: retrieved text cannot add a source, change a
limit or name a recipient. The shipped registry (checked 2026-10-13):

| id | kind | tier | how |
|---|---|---|---|
| `openai-news`, `deepmind-news`, `huggingface-blog` | `feed` | official | the publisher's RSS |
| `arxiv-cs-ai` | `feed` | primary_research | arXiv category RSS |
| `langgraph-releases`, `vllm-releases` | `github_releases` | official | GitHub REST releases |
| `hn-ai-agents` | `hn_search` | community | Hacker News Algolia search |
| `x-ai-engineering` | `x_search` | community | the Phase 3 recent-search client |
| `x-curated` | `x_accounts` | community | one `from:` query over handles you list (ships empty) |

- Feeds and GitHub use conditional requests (ETag / If-Modified-Since), a timeout
  (`DISCOVERY_TIMEOUT_SECONDS`), a size cap, safe XML parsing (`defusedxml`), redirects
  only within the registry host, and never a private address.
- Feed items keep the title, link, times, author and an HTML-stripped excerpt. Control
  and bidi characters are removed from all external text.
- `DISCOVERY_SOURCES=mock` (default) serves bundled fixtures through the same provider
  classes; `live` uses HTTPS. X sources read the real X API only with
  `RESEARCH_PROVIDER=x`; otherwise the synthetic X fixtures are used and the session is
  labelled `synthetic`.
- Not included: Reddit (its API terms need review), Mastodon, Bluesky, automatic
  linked-page fetching, general crawling, or rate-limit circumvention. Explicit
  primary-article enrichment is available for one selected source (below).

## Explicit primary-source enrichment

When a feed supplies only a title, a reviewer can authorize one primary article:

```bash
LLM_PROVIDER=fake uv run sga-discovery enrich opp_... --item item_... \
  --url https://publisher.example/official-article --by reviewer
```

The CLI checks `SGA_REVIEW_TOKEN` against the private configured review token.
The equivalent token-protected API is
`POST /discovery/opportunities/{id}/enrich` with `item_id`, `url`, `requested_by`,
optional literal `excerpts` (2–12 entries, 40–500 characters each), and optional
`refresh: true`. Nothing starts a run or calls a model/social API. This explicit
command reads a real article even with ordinary discovery providers set to mock;
the selected evidence itself must be real. Offline tests inject a mock transport.

Only a real, unpurged primary feed item already linked to the selected opportunity
can be enriched. The URL must match that item's canonical URL. The fetcher uses
HTTPS, exact-host redirects (at most three), public DNS addresses pinned into the
connection with original Host/TLS SNI, no environment proxies or credentials,
10-second HTTP timeouts, a 30-second elapsed budget checked during transfer, and
at most 2 MB of decoded HTML. DNS resolution uses the operating system resolver;
an in-progress HTTP read can run until its timeout after the elapsed budget is hit.
It reads a bounded robots policy first, refuses disallowed paths, unavailable
access policy, non-200 HTML, login/paywall errors, and noarchive/nosnippet storage
restrictions. It never executes JavaScript, follows article links, paginates or retries.

Extraction retains at most 12 complete technical excerpts, at most 4000 characters
including attribution. Navigation, scripts and code blocks are excluded. Optional
reviewer-selected excerpts must match literal article prose, so submitted fact
notes cannot introduce unsupported claims. Metadata supplies author/publication
date where present; otherwise the original publisher/date are preserved. The full
HTML is discarded; its SHA-256 and the compact evidence digest are recorded.
All claims are labelled self-reported, not independently verified. The existing
Research Agent's claim validation and URL/number grounding remain unchanged.

No migration or extra tables are required. Enrichment appends a separate
`discovery_sessions` / `discovery_fetches` audit, a content-addressed
`discovery_items` version and a new opportunity. Session metadata links the new
version to the original item/opportunity and records extraction version, response
digest and final URL. Original evidence, ranking, sessions and completed runs are
untouched. The derived opportunity's score is explicitly inherited, not reranked;
no discussion or Grok eligibility is invented. Select the **new opportunity id**
for a later reviewed generation run; replay and finding evidence use its new item
id through the existing provenance chain.

Repeated enrichment returns the prior audit without another GET, including failed
or unfinished attempts. Only an explicit `refresh` requests a new fetch; it still
does not re-fetch an unfinished attempt. Unchanged content reuses the same version,
while changed content creates another version. All versions keep the original
platform/provider-item identity, so existing purges/tombstones cover them together.
Ingestion and purge share identity locks to protect against concurrent deletion.
No copies of extracted prose are placed in request/audit JSON outside the normal
purgeable evidence storage. Extraction requires recognizable static article prose;
unsupported/dynamic-only pages fail safely without fabricating evidence.

### Mock and live data in one database

Synthetic (fixture) and real data can share a database; neither is ever served as the
other.

- The fetch cache and conditional-request validators (ETag, Last-Modified) belong to the
  provider that made the fetch: `mock_feed`, `mock_github`, `mock_hn` and `mock_fixtures`
  never satisfy, and never send validators for, a `feed`, `github`, `hn_algolia` or `x`
  fetch, and the reverse.
- Item identity includes the mode: synthetic items are keyed `synthetic|<key>`, real
  items by the plain key. A real item whose plain key is held by a synthetic row stored
  before this rule gets `live|<key>` instead, so legacy rows are never taken over.
- Cached and 304 reuse only replays items of the session's own mode; novelty and the
  14-day suppression compare only with opportunities of the same mode, and own-post
  overlap ignores the mock publisher's posts in real sessions.
- Backstop: a session that sees any synthetic item is labelled `synthetic` (with a
  warning log). Synthetic evidence is never upgraded to real.

### Domain configuration

Nothing in ranking is specific to technology. The registry carries the domain: sources,
`vocabulary`, `generic_terms` (subjects too broad for a Grok question, criterion 3),
weights and policy. Another domain (fashion, politics, culture) is another registry
file; the code stays the same.

Every item stores its source, platform, provider item id, canonical URL (tracking
parameters removed), title or excerpt, author or publisher, published time, first and
last retrieval time, matched vocabulary terms, evidence type, verification status, public
metrics when returned (NULL, never 0, otherwise), a content hash and the fetches that saw
it.

## Evidence semantics

**Credibility is not correctness.** The registry tier describes the publisher, never
whether a claim is true.

- `evidence_type`: `official_announcement`, `release`, `paper`, `documentation`,
  `engineering_blog`, `discussion_post`, `discussion_thread`.
- Item `verification_status`: `primary_source` for an item published by the subject
  itself (a vendor's release notes, a paper), `unverified` for everything else. A
  popular X post or HN thread is always `unverified`: engagement never raises it.
- Findings in a run carry a **claim kind** from the Research Agent (`reported_fact`,
  `opinion`, `prediction`, `open_question`, `disputed`), validated in code against the
  cited evidence, and a **verification** computed in code:
  - `corroborated` only when the same specific claim (its anchor terms: names,
    versions, numbers) appears in primary items from **at least two different
    publishers**. Two primary sources merely discussing the same subject are not
    corroboration.
  - `primary_source` when a cited primary item supports it, or when the claim has no
    checkable anchor.
  - `unverified` otherwise.
- The Content Agent states as fact only reported facts, attributed to their source;
  opinions stay opinions; a disagreement is represented, never created.
- **Grounding rule** (hard, discovery runs): every link in a draft must be a cited
  item's URL, and every number or percentage must appear in the evidence (years and
  list numbering are exempt).

## Opportunities

Items are clustered deterministically (canonical URL, then token-set Jaccard on titles
and excerpts; no embeddings, no vector database). Each opportunity's score is a weighted
sum of components in [0, 1]; the weights and windows live in `discovery.toml` and every
component is stored with the opportunity.

| Component | Basis |
|---|---|
| topic relevance | vocabulary and strategy pillars; `avoid_topics` excludes |
| timeliness | age of the newest primary item, 72 h half-life |
| source credibility | best registry tier |
| evidence quality | independent primary sources and their status |
| novelty | 1 − similarity to opportunities of the last 30 days |
| discussion potential | distinct threads and questions (low weight) |
| strategy fit | the active strategy version's pillars |
| own-post overlap | penalty; Jaccard ≥ 0.6 with your own post of the last 30 days suppresses |

- Outrage, reply ratios and "controversy" are never positive signals.
- A topic with only discussion sources is capped at 0.5 and is not offered for
  `news_commentary` or `research_summary`.
- A fingerprint (keywords plus primary URLs) is suppressed for 14 days unless new primary
  evidence appears; dismissed topics stay suppressed for 30 days; unused opportunities
  expire after 7 days.
- Ranking fields are immutable in the database; only status and dismissal change.

### Shortlist diversity

After sorting by score, at most `max_per_project_in_shortlist` (default 2) opportunities
from one project may sit in the first `shortlist_size` (default 5) places. A project is a
GitHub repository (`owner/repo`) taken from the primary evidence URL; opportunities
without one are never capped. An opportunity over the cap moves below the shortlist, in
score order, with a reason such as "shortlist diversity: placed below the top 5 because
2 higher-ranked opportunities from langchain-ai/langgraph are already there (score
unchanged; rank 3 by score)". Nothing is dropped, no score or evidence changes, the
order stays deterministic, and the moves are listed in the session summary
(`diversity_moves`). Diversity is computed within one session, so within one registry
(domain).

## Audience (read-only)

Candidates are the registry's curated accounts and the authors and threads that appear
in the session's own results. Signals come only from retrieved data (topic overlap,
recent activity, questions asked, primary evidence cited, public counts as returned),
each stored with the supporting item ids. Each entry explains itself and says how a
contribution could be useful.

Never done: follows, likes, reposts, replies, DMs or mentions; follower lookups or
purchases; storing bios; inferring sensitive traits (politics, health, religion,
ethnicity, sexuality and similar). The X user-lookup endpoint is not used.

## Content intents

`ContentIntent`: `original_insight`, `technical_explanation`, `news_commentary`,
`research_summary`, `open_question`, `grok_question`. Suggestions are deterministic from
the evidence (a release suggests news commentary, a paper a research summary, questions
an open question, eligibility a Grok question); you choose. The intent is recorded on
runs' content policy and on candidates (`content_candidates.content_intent`, NULL for
older rows). It is independent of `HookType`, which stays the strategy and experiment
dimension. Experiments refuse `grok_question` runs; discovery runs never come from
`ExperimentService`.

## @grok questions

An opportunity is eligible only when all seven criteria hold:

1. a primary item at most 14 days old (or marked evergreen);
2. an open question or disagreement in the evidence (re-checked from the run's findings:
   with none, the run stops with insufficient signal);
3. a specific subject: a vocabulary term not listed in the registry's `generic_terms`,
   or a named anchor in a title;
4. a stated reason why asking Grok adds value (the candidate's rationale; code checks it
   is present, you judge it);
5. Jaccard < 0.5 against every Grok question drafted in the last 90 days;
6. the draft mentions `@grok`, exactly once, and nobody else;
7. at most one eligible opportunity per session.

There is no quota: zero Grok questions in a session is normal. No controversy is
fabricated, no reply is sent to Grok, nothing polls for an answer, and nothing assumes a
mention helps reach. Grok may not answer.

## Manual-only mentions

The **text** decides: any `@mention` (including the full-width `＠`) makes a draft
manual-only, whatever its intent label says.

| Gate | Behaviour |
|---|---|
| generation and critique | `MentionRule`: no mentions unless intent is `grok_question` and the only one is `@grok` |
| human edit | edits become new candidates and pass the same rules |
| review | `GET /runs/{id}` marks such candidates `manual_only: true`; approval still works |
| `POST /runs/{id}/publish` | refused (409, "manual publication only"); nothing recorded |
| scheduled publishing | refused at request time |
| publisher worker | re-checks before any call; a smuggled row ends `failed` / `manual_only` |
| X and mock publishers | refuse before any HTTP request |
| exported candidates | the API publish path refuses them too, so nothing is posted twice |

### Export and registration

- `POST /runs/{id}/candidates/{cid}/export` (token): only an approved candidate with a
  passing critique and the same content policy checks as publishing (a mention is what
  makes it manual). Returns the exact text, its SHA-256, the instructions and an
  `intent_url` (x.com's compose box, pre-filled; you press Post). Idempotent while the
  text is unchanged. Audited in `manual_exports`.
- You post it yourself from the configured account.
- `POST /runs/{id}/candidates/{cid}/manual-publication` (token) with the post's URL
  (`https://x.com/<user>/status/<id>` or twitter.com) or its id:
  - requires `X_ACCOUNT_USER_ID` (or `X_ACCOUNT_USERNAME`), else 503;
  - requires an export of that candidate;
  - reads the post **once** (app-only bearer token; a verification read in usage) and
    checks the author is the configured account, the text matches the export (exactly,
    or after whitespace, Unicode and t.co link normalization) and it was created after
    the export;
  - records a `published` publication with `provider = 'manual'`, a
    `manual_publications` row, and the usual analytics jobs; lineage reaches the
    opportunity and its evidence;
  - is idempotent: the same post again returns the record without another read; a post
    already linked to another candidate or publication is refused (409).

## X automation rules this follows

From X's automation rules (help.x.com, updated April 2026) and the developer guidelines
(docs.x.com):

- Automated unsolicited mentions and replies are not permitted; neither are replies
  chosen by keyword search alone. AI reply bots need prior written approval.
- Duplicative or substantially similar posts are not allowed (own-post and Grok
  similarity suppression).
- Automatically posting about trending topics is not allowed (sessions and runs start
  only on request and every post needs human approval).
- Bulk or aggressive following, and follower-gain apps, are not allowed (no follow,
  like, repost or DM code exists).
- Inferring sensitive traits is prohibited (no such fields or signals exist).
- Content deleted on X must be removed promptly, within 24 hours (see retention).

## Costs and limits

Default caps per session (a request may lower them; the hard maxima cannot be raised):

| Limit | Default | Hard max |
|---|---|---|
| X requests | 2 | 5 |
| posts per X request | 10 | 100 |
| user reads (author expansions) | 20 | 100 |
| feeds | 8 | 20 |
| items per feed | 20 | 50 |
| GitHub requests | 4 | 10 |
| HN requests | 1 | 2 |
| cache window | 6 h | 72 h |

X pay-per-use prices, verified on docs.x.com on 2026-10-13 ("subject to change"):

| Resource | Price |
|---|---|
| post read | $0.005 |
| user read | $0.010 |
| post create | $0.015 |
| post create with a URL | $0.200 |

- `pricing.toml` stays the source of truth and ships these prices **unset**; unknown
  prices are reported as unknown, never as zero.
- At those prices a session costs at most 20 post reads + 20 user reads = **$0.30**.
  That covers discovery reads only: research in runs from opportunities makes no X
  reads, while LLM usage, publishing, verification reads and analytics are billed and
  reported separately.
- A post created through the API with a link is priced separately
  (`post_create_with_url`, its own cost line). A manually registered post has no API
  create cost; its one verification read is reported.
- Repeating a source within the cache window reuses stored items; `refresh` skips the
  window but still sends a conditional request.

## Retention and deletion

Deletion and retention apply to every copy of third-party text:

| Copy | What happens |
|---|---|
| `discovery_items` | text, title and author set to NULL; `purged_at` and the reason set; id, URL and hashes kept |
| `source_posts` (run evidence) | text replaced with a fixed placeholder; author name cleared |
| LangGraph checkpoints | stored `SourcePost` values of the affected runs rewritten in place |
| `content_purges` | a tombstone per item (append-only) |

- **Deletion requests:** `POST /discovery/purge` (token) or `sga-discovery purge <ids>`.
- **Retention:** X text older than `DISCOVERY_X_RETENTION_DAYS` (default 30) is purged by
  `sga-discovery purge-expired`, an explicit command to run daily from cron or a job
  runner (nothing is scheduled by the application). Feed, GitHub and HN items keep their
  excerpts.
- A tombstoned item is never stored again by a later session (the fetch says so), is
  never replayed into a run, and a database trigger refuses restoring purged text.
- Runs keep working: a paused run resumes from its redacted checkpoint; a new run from
  an opportunity whose evidence is entirely purged is refused.
- Audit ids (item ids, provider ids, URLs, hashes, tombstones) are kept.

## CLI

`sga-discovery` uses the API's configuration and services. Commands that change anything
(`run`, `start-run`, `export`, `register`, `purge`) need `SGA_REVIEW_TOKEN`, checked
against `STRATEGY_REVIEW_TOKEN` exactly like the API's bearer token. `start-run` runs the
workflow in-process until it waits for review.

```bash
uv run sga-discovery sources
uv run sga-discovery plan --account acct_1
SGA_REVIEW_TOKEN=... uv run sga-discovery run --account acct_1 --by you
uv run sga-discovery opportunities disc_...
SGA_REVIEW_TOKEN=... uv run sga-discovery start-run opp_... --intent news_commentary \
    --account-file account.json --by you
SGA_REVIEW_TOKEN=... uv run sga-discovery export run_... cand_... --by you
SGA_REVIEW_TOKEN=... uv run sga-discovery register run_... cand_... https://x.com/you/status/1 --by you
SGA_REVIEW_TOKEN=... uv run sga-discovery purge 1900000000000000101 --by you
uv run sga-discovery purge-expired
```

Live demo (opt-in; real feeds, GitHub and HN; X only with `RESEARCH_PROVIDER=x`):

```bash
RUN_LIVE_DISCOVERY_DEMO=1 DATABASE_URL=postgresql://... \
    uv run python -m social_growth_agent.services.discovery_live_demo
```

## Security

- External text is untrusted data: sanitized, truncated and passed to agents only as
  JSON under a "data, not instructions" boundary. A prompt injection in a fixture post
  is part of the tests.
- Secrets never enter items, rows, logs, prompts or responses; failed fetches store a
  sanitized message, never the response body or auth headers.
- Read endpoints never call a model, a platform or write anything.
- Tokens gate every action that spends, exports, registers or purges.

## Limitations

- Ranking is a heuristic; "interesting" stays a human judgement at review.
- Claim kinds come from a model and are validated only for consistency with the cited
  sources, so facts are attributed, not asserted.
- Without user lookup, audience signals are thin.
- A dead feed shows up as a failed fetch. A feed that answers 200 with a valid but empty
  channel shows up as `empty`, and the fetch ledger records why it is empty (item count,
  build date, size and content type). arXiv category RSS is empty on days without an
  announcement (weekends and holidays); if it stays empty on announcement days, arXiv's export API
  (`export.arxiv.org/api/query`) is the documented alternative (not used yet).
- X prices change; set them in `pricing.toml` yourself.
- Manual registration depends on X returning the post to an app-only read; a protected
  or deleted post cannot be registered.
