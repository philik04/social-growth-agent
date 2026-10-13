# Strategy evaluation and proposals (Phase 7)

Phase 7 closes the loop from metrics back to strategy, with a human at every step that
changes anything:

```
post_metrics (Phase 6)
   |  POST /evaluations                       deterministic, no model, no platform call
performance_evaluations + evaluation_observations   (immutable evidence package)
   |  POST /evaluations/{id}/proposals        opt-in (STRATEGY_PROPOSER); the only model call
strategy_proposals (pending_review)           validated in code against the evidence
   |  POST /strategy-proposals/{id}/approve   review token; all-or-nothing
strategy_versions (new version, NOT active)
   |  POST /strategies/{id}/activate          review token; separate, compare-and-set
strategy_activations
   |  POST /runs {"use_active_strategy": true}
runs.strategy_version_ref                     future runs only; nothing is published
```

Nothing in this chain publishes, starts a run or changes a strategy on its own. Read
endpoints never call a model, never read from X and never write.

## 1. Evaluations

An evaluation is a pure function of stored rows (`analytics/evaluation.py`). It takes every
**published** post of one account, joins it to its run, candidate and the `post_metrics`
snapshot at one `snapshot_age` (`1h`, `24h`, `72h`, ...), and describes how groups of posts
performed. It reads nothing from X and calls no model (`platform_reads` and `llm_calls` are
always 0).

### Request

```json
POST /evaluations
{"account_id": "acct_1", "snapshot_age": "24h", "synthetic": false,
 "data_cutoff": "2026-10-13T00:00:00Z",          // optional, default now; never in the future
 "published_since": null, "published_until": null, "strategy_id": null,
 "min_group_n": 5, "min_compare_n": 10, "min_total_n": 20}   // optional overrides
```

Datetimes must carry a timezone. Unknown fields are rejected (422). The request plus the
configuration is normalized and hashed (`input_digest`); an identical request returns the
stored evaluation with **200** instead of **201**, so re-posting is safe. To get a fresh
evaluation, change `data_cutoff`.

### Age matching and eligibility

Every post is compared at the same age, because a 1h count and a 72h count are not
comparable. A post is **eligible** only if its snapshot at that age exists and was captured
on target. Otherwise it is listed with an exclusion reason, never silently dropped:

| Reason | Meaning |
|---|---|
| `not_yet_due` | `published_at + age` is after the cutoff: the snapshot cannot exist yet |
| `no_snapshot` | due, but no snapshot at that age (failed, cancelled or not collected) |
| `off_target_late` / `off_target_early` | captured outside `max(120 s, 10 % of the age)` of the target, measured from the snapshot's own `provider_created_at` |
| `no_provider_created_at` | X never reported a creation time, so the actual age is unknown |
| `synthetic_metrics` | a `mock_analytics` snapshot in a real evaluation |
| `real_metrics_in_synthetic_evaluation` | a real snapshot in a synthetic evaluation |

`GET /evaluations/{id}/observations?eligible=false` lists them.

### Synthetic vs real metrics

`synthetic` is part of the request and of the digest. A real evaluation (`false`, the
default) only ever uses snapshots from a real provider (`x`); a synthetic one only uses
`mock_analytics`. The two populations never mix in one evaluation, so demo numbers cannot
leak into a real decision. Every evaluation and proposal view carries the flag.

### Metrics

Per eligible post: `likes`, `replies`, `reposts`, `quotes`, `bookmarks`, `impressions`, and

- `engagement_count = likes + reposts + replies + quotes` (bookmarks are private intent and
  excluded; NULL if any part is NULL);
- `engagement_rate = engagement_count / impressions`, `reply_rate`, `repost_rate`, only when
  `impressions > 0` (otherwise NULL, never 0).

NULL means "the platform did not report it" and is never treated as 0: each metric has its
own `n`.

### Dimensions

| Dimension | Groups |
|---|---|
| `hook_type` | the hook the Content Agent **declared** for the candidate |
| `strategy_version` | `strategy_id@vN`; if two runs used different content under the same number, `#digest8` is appended so they are not merged |
| `length_bucket` | Unicode code points, edges `EVAL_LENGTH_BUCKET_EDGES` (default `0-100`, `101-200`, `201+`) |
| `hour_block_utc` | publication hour in UTC, blocks of `EVAL_HOUR_BLOCK_HOURS` (default 4) |
| `origin` | `generated` vs `human_edit` |
| `format` | only listed when it varies |

### Statistics (descriptive only)

Per group and metric: `n`, `missing`, mean, median, p25/p75, min/max, sample standard
deviation, and for rates also the pooled rate `Σ numerator / Σ impressions`. All arithmetic
is exact (`Fraction`) and numbers are serialized as 6-decimal strings, so the same rows give
byte-identical output on any machine.

Evidence levels per group: `insufficient` below `min_group_n` (default 5), `descriptive`
from `min_group_n`, `comparable` from `min_compare_n` (default 10). If fewer than
`min_total_n` (default 20) posts are eligible the evaluation is `insufficient_evidence` and
contains **no comparisons**. Floors (3 / 5 / 10) cannot be lowered by a request.

Comparisons exist only between two `comparable` groups of the same dimension. Each has an id
(`cmp:hook_type:how_to:vs:number`), the median and pooled differences for every rate, the
label "observed difference between groups; not causal", and a list of confounders (posting
time, topic, audience growth, self-labelled hooks, small samples, ...). A comparison never
names a winner, and there are no p-values or confidence intervals: with tens of posts per
group they would suggest a certainty the data does not have.

### Reproducibility and immutability

Each evaluation stores its request, configuration (`config_version = eval-v1`), summary,
one observation row per considered post, and a sha256 `digest` over the canonical JSON of
request, configuration and result. Recomputing from the stored rows reproduces the digest
(tested). Evaluations and observations are append-only: a trigger rejects `UPDATE` and
`DELETE`.

Caveat: reproducibility holds for data recorded through the normal paths. A publication
inserted afterwards with a `published_at` before an old cutoff (a manual backdated insert)
would change a recomputation of that old evaluation; the stored evaluation itself never
changes.

## 2. Proposals

`POST /evaluations/{id}/proposals` asks the Strategy Agent (`agents/strategy.py`, prompt
`strategy-v1`) for a proposal against the account's **active** version.

- **Opt-in.** `STRATEGY_PROPOSER=disabled` (default) refuses with 409 and calls nothing.
  `fake` uses the deterministic offline agent; `openai` uses `OPENAI_API_KEY` and
  `STRATEGY_LLM_MODEL` (default `OPENAI_MODEL`). A missing key is a startup error, never a
  silent fallback.
- **Insufficient evidence costs nothing.** For an `insufficient_evidence` evaluation the
  service records a deterministic `no_change` proposal without calling a model; it can only
  be rejected.
- **What the model sees:** the evaluation summary (groups, comparisons, limitations) and the
  current strategy. Never post text, never source posts, never credentials. The prompt
  states that everything after the instructions is data.
- **One open proposal per account** (`generating` or `pending_review`), enforced by a
  partial unique index, so two concurrent requests cannot both spend a model call.
- **Ledger.** The row is committed as `generating` before the call and finalized after it
  with tokens, latency and an estimated cost from `pricing.toml` (blank prices give
  "unknown", never 0). Error messages are generic ("model call failed (provider error)");
  provider text and keys are never stored.
- **Cost per evaluation.** `GET /evaluations/{id}` carries `usage`: the token usage of every
  proposal made from it (failed and abandoned ones included, since they spent tokens),
  merged per model, an estimate at current pricing, and `at_proposal_pricing_total`, the
  sum of each proposal's estimate at the price list it snapshotted (`null` unless every
  price is known, with `proposal_pricing_missing`). The evaluation itself costs nothing.

### What may change (allowlist)

Only two fields, both soft preferences for the Content Agent:

| Field | Evidence it must cite |
|---|---|
| `preferred_hooks` | a `hook_type` comparison |
| `length_guidance` (`min_chars`, `max_chars`, 1 ≤ min ≤ max ≤ 280) | a `length_bucket` comparison |

They never override `ContentPolicy` (hard rules such as 280 characters), the critic gate or
a reviewer's notes; the content prompt says so. Pillars, tone, audience, posting cadence
and everything else are outside the allowlist. A posting-time observation can be attached
as a note, but never as a change.

### Validation in code (`policies/strategy_changes.py`)

A draft becomes `pending_review` only if all of these hold; otherwise the proposal is
`failed` with the reasons listed:

- every field is on the allowlist and actually differs from the current value;
- every evidence reference is a comparison id that exists in **this** evaluation and
  belongs to the dimension the field requires;
- every number in the text appears in the evaluation (rounded and percent forms allowed),
  so the model cannot invent figures;
- each field is changed at most once, and the resulting strategy is still valid;
- confidence is `low`, or `medium` only with at least `2 × min_total_n` eligible posts
  (there is no `high`);
- the expected effect is a direction (`increase` / `decrease`) of one named metric with a
  hypothesis, never a predicted size.

### Lifecycle

```
generating ──> pending_review ──> approved | rejected | superseded
     │                │
     └──> failed      └─(new active version)──> superseded on approve
     └──(stale, retried)──> abandoned
```

- **Retry** (`POST /strategy-proposals/{id}/retry`) is explicit and works only on a
  `failed` proposal, or on one stuck in `generating` longer than `2 × LLM_TIMEOUT_SECONDS`
  (a process that died mid-call). The stale row is closed as `abandoned`; if its late
  result ever arrives, the guarded state transition discards it. `retry_of` is unique, so a
  proposal can be retried once, and the open-proposal index still allows only one active
  proposal. A retry never changes a strategy.
- **Approve** (`POST /strategy-proposals/{id}/approve`, review token) is all-or-nothing. The
  body must carry the `proposal_sha256` and `evaluation_digest` the reviewer saw; any
  mismatch is a 409. If the base version is no longer the active one, the proposal becomes
  `superseded` (409) instead of being applied on top of a strategy it was not written for.
  The allowlist is checked again on the resulting strategy. Approval creates a new version
  (`origin = proposal`, number above every registered or run-recorded version) that is
  **not active**. The decision row is unique per proposal, so a second approval, or a reject
  after an approve, is refused by the database.
- **Reject** records the decision and changes nothing else.

## 3. Versions and activation

- `POST /strategies/versions` (review token) registers a human-written strategy as an
  immutable version (idempotent; a different body under an existing number is a 409).
  `sga-strategy import-runs [--dry-run]` registers the strategy bodies recorded in earlier
  runs (origin `imported_from_run`). An `id@version` used with several bodies, or
  registered with a different body, is listed as a conflict and left unregistered, never
  resolved by picking one; conflicts make the exit code 1. Importing activates nothing.
- `POST /strategies/{id}/activate` (review token) activates one version with
  `expected_active_version_id` (compare-and-set: 409 if someone else activated meanwhile).
  Activations are an append-only history; the active version is the latest one.
- `GET /accounts/{id}/active-strategy` and `GET /strategies/{id}/versions` read them.
- `POST /runs` accepts either an inline `strategy` (unchanged from Phases 1-6) or
  `"use_active_strategy": true`. Either way, a run whose strategy matches a registered
  version records it in `runs.strategy_version_ref`, so results stay attributable to the
  exact version. Activation affects future runs only; running and paused runs keep the
  strategy they started with.

## 4. Review authentication

Approve, reject, register and activate require `Authorization: Bearer <STRATEGY_REVIEW_TOKEN>`
(at least 16 characters, compared in constant time):

| Situation | Response |
|---|---|
| token configured, header missing | 401 with `WWW-Authenticate: Bearer` |
| token configured, wrong token | 403 |
| no token configured | 503 (review disabled) |
| no token, `STRATEGY_REVIEW_ALLOW_UNAUTHENTICATED=true` and `APP_ENVIRONMENT=development` | allowed, recorded as `unauthenticated_local` |

`APP_ENVIRONMENT` defaults to `production`, and the unauthenticated flag is a startup error
anywhere else. Every decision, version and activation records `auth_method`
(`review_token` or `unauthenticated_local`) next to the reviewer name. The token proves
possession of a shared secret, not identity: the reviewer name is self-declared.

Requesting and retrying a proposal are not token-protected (they change no strategy), but
with `STRATEGY_PROPOSER=openai` they spend money, like starting a run does.

## 5. Configuration

| Setting | Default | |
|---|---|---|
| `APP_ENVIRONMENT` | `production` | `development` is required for unauthenticated review |
| `STRATEGY_REVIEW_TOKEN` | unset | review disabled (503) until set |
| `STRATEGY_REVIEW_ALLOW_UNAUTHENTICATED` | `false` | local development only |
| `STRATEGY_PROPOSER` | `disabled` | `disabled`, `fake`, `openai` |
| `STRATEGY_LLM_MODEL` | `OPENAI_MODEL` | |
| `EVAL_MIN_GROUP_N` / `EVAL_MIN_COMPARE_N` / `EVAL_MIN_TOTAL_N` | 5 / 10 / 20 | floors 3 / 5 / 10 |
| `EVAL_HOUR_BLOCK_HOURS` | 4 | 1, 2, 3, 4, 6, 8, 12 or 24 |
| `EVAL_LENGTH_BUCKET_EDGES` | `100,200` | increasing upper edges |

## 6. Demos

```bash
# offline and free: synthetic snapshots, fake Strategy Agent, nothing posted
DATABASE_URL=postgresql://sga@localhost:5433/sga_scratch \
  uv run python -m social_growth_agent.services.strategy_demo

# opt-in, one real OpenAI call for a stored evaluation; never approves or activates
RUN_LIVE_STRATEGY_PROPOSAL_DEMO=1 STRATEGY_PROPOSER=openai OPENAI_API_KEY=... DATABASE_URL=... \
  uv run python -m social_growth_agent.services.strategy_live_demo eval_...
```

## 7. Limitations

- **Small samples.** A personal account publishes tens of posts per month. With the default
  thresholds a comparison needs 10 on-target snapshots per group and 20 overall; even then
  medians move with a few posts. Results are descriptive, never causal.
- **Self-labelled hooks.** `hook_type` is what the Content Agent declared, not a measured
  property of the text.
- **Confounding.** Time, topic, news, audience growth and posting hour all vary together;
  nothing is randomized. Phase 8 (experiments) addresses this.
- **Public metrics only.** Impressions may be NULL for some posts, which removes them from
  rate statistics (their counts still appear).
- **Length** is counted in Unicode code points, not X's weighted character count.
- **Posting time** is advisory: it can be noted, never changed.
