# Experiments (Phase 8)

Phase 7 describes what happened; it cannot say why, because hook, length, topic, time and
audience all change together. Phase 8 adds pre-registered A/B experiments: each new run of
an experiment is randomly assigned to one arm (a hook type or a length range), the arm is
enforced as a hard rule, and the published posts are compared at one age-matched snapshot
with an uncertainty interval. Humans still review, publish, adopt and activate everything.

```
POST /experiments                 (token) draft with its full pre-registration, base version pinned
POST /experiments/{id}/approve    (token) at most one approved/running experiment per account
POST /experiments/{id}/start      (token)
POST /experiments/{id}/runs       (token) randomize -> run + assignment in one transaction -> run starts
   ... normal human review (POST /runs/{id}/review) and explicit publishing (POST /runs/{id}/publish)
GET  /experiments/{id}/analysis           funnel, balance, adherence; outcomes masked while open
POST /experiments/{id}/unblind    (token) optional, irreversible: everything after is exploratory
POST /experiments/{id}/stop       (token) operational | cost | content_safety | other
POST /experiments/{id}/conclude   (token) immutable result
POST /experiments/{id}/adopt      (token) inactive strategy version (confirmatory, real data only)
POST /strategies/{id}/activate    (token) Phase 7 compare-and-set, unchanged
```

Read-only: `GET /experiments`, `GET /experiments/{id}`, `.../assignments`, `.../analysis`,
`.../lineage`, `.../usage`, and `POST /experiments/feasibility` (pure planning arithmetic,
stores nothing). No read endpoint writes, calls a model or calls X.

## 1. Pre-registration

`ExperimentDesign` (`models/experiment.py`) is fixed when the draft is created. A database
trigger (`sga_freeze_experiment_design`, migration 0006) refuses any change to the design,
its digest, the factor, the base version or the synthetic flag, and refuses deletion. A
different design is a new draft.

| Field | Default | Meaning |
|---|---|---|
| `factor` | | `hook_type` or `length`; every arm sets exactly this factor |
| `hypothesis` | | free text, stored and shown |
| `arms` | | 2 or 3; exactly one `is_control` |
| `primary_metric` | `engagement_rate` | must be a rate (`engagement_rate`, `reply_rate`, `repost_rate`) |
| `snapshot_age` | `24h` | one of `ANALYTICS_SNAPSHOT_AGES`; the only age analysed |
| `planned_n_per_arm` | 50 (min 30) | analysable posts per arm for a confirmatory verdict |
| `max_runs_per_arm` | 2 x planned | assignment cap (spend bound) |
| `max_duration_days` | 120 (7-365) | from `started_at`; no assignment after it, no extension |
| `bootstrap_resamples` | 10 000 | |
| `attrition_threshold_pp` | 20 | published-share gap that flags differential attrition |
| `missing_impressions_threshold_pct` | 20 | share of eligible snapshots without impressions that flags the result |
| `synthetic` | false | analyse `mock_analytics` snapshots only; never adoptable |
| `planning_posts_per_week` | 5 | feasibility table only; never used in the analysis |

The stopping rules are part of the design and the code, not of the request: assignment
ends at the cap, at the maximum duration, or on an explicit stop with an outcome-free
reason. There is no extend endpoint, and stopping does not lower the bar for a verdict.

### Feasibility (D9)

Every experiment view and `POST /experiments/feasibility` carry a planning table:

- power table: posts per arm for relative differences of 20-50% at 80% power, two-sided
  alpha 0.05 (Bonferroni with three arms), with an **assumed** coefficient of variation of
  0.6 (labelled as an assumption until real data can estimate it);
- weeks needed at 3, 5, 7 and 10 published posts per week, with 100%, 80% and 60% of posts
  getting an eligible snapshot;
- runs needed when 90%, 70% or 50% of runs end in a published post;
- warnings when the planned sample is unlikely before the maximum duration at the planning
  assumption (5 posts/week, 80% eligible), or when 70% publication would exhaust the cap.

At 5 posts per week and the default 50 posts per arm, a two-arm experiment needs about 25
weeks; the default 120-day maximum is shorter, and the warning says so. That is the honest
arithmetic for a personal account: either plan 30 posts per arm (15 weeks), publish more,
or accept `insufficient_data`.

## 2. Treatments are hard rules (D3)

An arm's treatment is written into the run's `ContentPolicy` (`policies/treatment.py`):

- hook arm: `required_hook_type`; rule `required_hook_type` refuses a candidate that
  **declares** another hook;
- length arm: `min_post_length`/`max_post_length`; rules `min_post_length` and
  `max_post_length`. A length arm can never exceed the policy maximum (280).

The rules run on every generation and regeneration (critic gate), on human edits (the
edited candidate is critiqued like any other) and again at publish time
(`publish_refusals`). Without a treatment the new fields are unset and the rules are
no-ops, so Phase 1-7 runs, prompts and checkpoints are unchanged.

**A declared hook label is not proof that the text uses the hook.** The analysis reports a
heuristic adherence count per arm (`policies/treatment.hook_adherence`):
`question` looks for a question mark in the opening, `number` for a digit, `how_to` for
"how to" or step markers; `story` and `contrarian` are `not_checkable`. These counts
estimate the uncertainty; they verify nothing and block nothing.

## 3. Randomization and assignment

`POST /experiments/{id}/runs` (review token: it starts a run that may call paid model and
X research APIs):

1. `SELECT ... FOR UPDATE` on the experiment row (serializes draws per experiment).
2. Checks: running, before `started_at + max_duration_days`, below the cap, the account is
   the experiment's, and the active strategy is still the pinned base version.
3. Permuted blocks of `2 x arms` slots: the next arm is drawn uniformly from the arms with
   slots left in the current block (`secrets.SystemRandom`). Blocks stay balanced and the
   next arm is never stored in advance.
4. In the same transaction: the run row (queued, with the treated policy and the base
   strategy) and the `experiment_assignments` row (`UNIQUE (run_id)`, `UNIQUE
   (experiment_id, block_no, position)`). Commit.
5. Only then is the run submitted for execution.

Review and publishing are unchanged: a human approves, edits, regenerates or rejects, and
publishes explicitly. An experiment never publishes, adopts or activates anything.

## 4. Analysis (`analytics/experiment.py`, pure)

- **Unit**: one randomized run. Its **first** published post (by publication time) is
  analysed; further posts of the same run are listed (`extra_publication_ids`) and ignored.
- **Funnel per arm** (D4): assigned, generated, reviewed, approved, published,
  snapshot-eligible, analysable, extra publications and exclusion reasons. Persisted with
  the result.
- **Eligibility**: Phase 7's `classify` at the experiment's snapshot age (on target within
  `max(120 s, 10%)`); synthetic and real snapshots never mix; a post without impressions
  (or with 0) is eligible but not analysable for a rate.
- **Primary estimate**: difference in pooled rates (sum of numerators / sum of
  impressions), variant minus control, with a 95% percentile bootstrap interval over posts
  (97.5% per comparison with two variants, Bonferroni). Robustness: the difference in
  medians with its own interval. Relative lift is reported. No p-values.
- **Balance** (shown, never adjusted): UTC 4-hour publication blocks, weekdays, candidate
  origin (generated vs human edit) and median hours from assignment to publication.
- **Reproducible**: facts are filtered by a cutoff (candidates, review decisions,
  publications and snapshot captures after it are ignored), numbers are fixed-precision
  strings, and the bootstrap seed is the first 64 bits of the input digest. The same rows
  and cutoff give the same result and digest, in any process.

### Verdicts (final result only)

In order:

1. any arm below `planned_n_per_arm` analysable posts, or `impressions_missing`
   -> `insufficient_data` (also at the maximum duration: the bar is never lowered);
2. `differential_attrition` (published share differs by more than the threshold)
   -> `inconclusive`;
3. per variant: interval above 0 -> `favours_variant`; below 0 -> `favours_control`;
   otherwise `inconclusive`. Overall: `favours_variant` if any variant is favoured.

If outcomes were unblinded before the conclusion, (3) becomes
`exploratory_favours_variant` / `exploratory_favours_control` and the evidence class is
`exploratory`.

Flags: `below_planned_n`, `max_duration_reached`, `stopped_early`, `unblinded_early`,
`differential_attrition`, `impressions_missing`, `synthetic`.

## 5. Masking and unblinding (D6)

While an experiment is open, `GET .../analysis` withholds exactly these fields
(`masked_fields`): per-arm outcomes (engagements, impressions, rates), effects and their
intervals, the verdict, and per-post counts in `observations`. The funnel, balance,
adherence and assignment counts stay visible because they are needed to run the
experiment.

**The experiment is not blind** to anyone who looks elsewhere, and the response says so
(`not_masked_elsewhere`): per-post metrics (`GET /publications/{id}/metrics`, `GET
/posts`), each run's arm (`GET .../assignments`, the run's content policy in `GET
/runs/{id}`) and Phase 7 evaluations of the same posts are all readable. Masking keeps the
experiment's own summaries out of view; it does not prevent a determined peek.

`POST .../unblind` (token, a reason required) shows the summaries early. It is recorded
(`peeked`, `unblinded_at`, an event), cannot be undone, and makes every later conclusion
exploratory and never adoptable. An unblinded interim shows estimates without a verdict.

## 6. Conclusion, immutability and adoption

`POST .../conclude` is allowed once every arm has its planned analysable sample, the
maximum duration has passed, or assignment was stopped. It stores one
`experiment_results` row (unique per experiment, append-only by trigger) with the verdict,
evidence class, flags, cutoff, analysis version, input digest, result digest and the full
analysis, and is idempotent. A concluded result is read back as stored, never recomputed.

`POST .../adopt` with a `variant` creates a new **inactive** strategy version
(`origin = experiment`, `parent_version_id` = the base, `source_experiment_result_id`,
unique) only if:

- the experiment is not synthetic;
- the result is `confirmatory` and the verdict is `favours_variant`;
- the named variant's own comparison favours it;
- the base version is still the active one;
- it has not been adopted already.

A hook winner moves to the front of `preferred_hooks`; a length winner becomes
`length_guidance`. Both are soft preferences again once adopted: the experiment enforced
them, a strategy only favours them. Activation remains the Phase 7 compare-and-set request.

## 7. Cost (D9)

`GET .../usage`: every assigned run's canonical usage (LLM tokens, research reads,
publish operations, analytics reads), merged per arm and in total, estimated at each run's
stored price list and at current prices. Unknown prices are listed in `missing_prices`
and make totals `null`; nothing is assumed to cost 0, and the X and OpenAI prices in
`pricing.toml` are not verified billing rates. `projected` multiplies the mean cost of
finished runs (at current pricing) by the runs the cap still allows. Analytics request
counts are an upper bound when summed across runs (one batched read can cover several).

## 8. Strategy Agent digest (D8)

Since `strategy-v2` the Strategy Agent no longer receives the whole evaluation summary.
`agents/evidence_digest.py` reduces it to the groups along hook type, length bucket and
UTC hour block (at least descriptive), the three rate metrics as `[n, median, pooled]`,
the comparisons along those dimensions with rate differences only, and the
evaluation-wide confounders, as compact JSON, plus only the two proposable fields of the
current strategy. Proposals are validated against the **same** digest: evidence references
and every number must appear in what the model was shown. `no_change` stays valid.

The digest has a character budget (12 000). Over it, it is reduced deterministically:
descriptive-only groups, then posting-time data, then the comparisons with the smallest
samples, and the omissions are listed in the digest.

Measured offline (characters, not tokens): 38.6k -> 3.0k characters of payload on the
12-post test fixture and 119.9k -> 9.9k on a 150-post fixture; including the unchanged
system prompt (3.0k characters) the reduction is 86% and 90%. The 90% token reduction is a
target: `strategy_live_demo` prints the digest size and the provider's input-token count,
and only that measurement counts.

## 9. Synthetic scenarios (D10)

```bash
DATABASE_URL=postgresql://sga@localhost:5433/sga_scratch \
  uv run python -m social_growth_agent.services.experiment_demo
```

Offline and free. Six scenarios on generated data labelled SYNTHETIC: a positive effect
(with one real assignment through the service and the fake agents), a null effect, too
little data at the maximum duration, differential attrition, missing impressions and early
unblinding. Every adoption is refused because the data are synthetic.
`services/experiment_seed.py` writes the finished-run rows directly (same lock, draw and
treatment as the service) and is never used by the API or the workers.

## 10. Limitations

- **Selection after randomization.** The comparison uses published posts, and humans
  choose what to approve, edit and publish after assignment. Randomization protects the
  assignment, not those choices. The attrition flag catches only gaps above the threshold;
  a smaller gap does not mean there is no selection bias.
- **Small samples.** At personal-account volume an experiment takes months. The bootstrap
  interval is approximate at a few dozen posts and can be too narrow for skewed rates. In
  400 simulated null experiments (30+ posts per arm) 5.5% reached a confirmatory verdict,
  close to the nominal 5%; that is one setting, not a guarantee.
- **One test in twenty.** An interval excluding zero happens by chance about once in
  twenty null experiments. Running several experiments in sequence multiplies that.
- **Declared hooks.** The hook rule enforces a label; adherence is a heuristic estimate.
- **Posting time** is chosen by a human after assignment; it is shown, not controlled.
- **Not blind.** See section 5.
- **Interference.** Posts of different arms share one audience and one timeline; an
  effect on one can spill over to the other.
- **Fixed horizon only.** No sequential testing; looking early is allowed only as an
  explicit, recorded unblinding that downgrades the evidence.
