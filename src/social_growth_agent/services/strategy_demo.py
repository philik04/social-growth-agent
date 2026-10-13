"""Phase 7 demo: evaluation, proposal, human approval, explicit activation, a future run.

    DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.strategy_demo

Offline and free. The posts and their 24h snapshots are synthetic (seeded directly and
labelled ``mock_analytics``), the proposal comes from the deterministic fake Strategy
Agent, and the new run uses the fake agents and mock research. Nothing is read from or
posted to any platform, and no model is called. The demo shows, in order:

1. registering and activating strategy v1 (review token checked like the API does);
2. synthetic published posts: comparable ones, plus late, missing, zero-impression,
   not-yet-due and real-provider snapshots that must be excluded;
3. a synthetic evaluation (eligible vs excluded with reasons, groups, comparisons), and
   the same request as a *real* evaluation, which excludes every synthetic snapshot;
4. a proposal from the evaluation, with its evidence references and hypothesis;
5. approval: a new version, **not** active; a second approval is refused;
6. explicit activation (compare-and-set), then a run created with
   ``use_active_strategy`` that records the new version. No publication is created.

Use a scratch database: the demo runs ``sga-db upgrade`` on it.
"""

import secrets
from datetime import timedelta

from pydantic import SecretStr

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ProposalStateError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import Account, ContentStrategy, HookType, new_id, utc_now
from social_growth_agent.models.strategy import Dimension, GroupStats, Metric
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.providers.mocks import MockPublisher, MockResearchProvider
from social_growth_agent.services.evaluations import EvaluationRequest
from social_growth_agent.services.review_auth import ReviewAuth
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime
from social_growth_agent.services.strategy_seed import SeedPost, seed_posts


def build(settings: AppSettings) -> Runtime:
    deps = Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider())
    return build_runtime(settings, deps=deps, executor=InlineExecutor(), publisher=MockPublisher())


def demo_posts(account: Account, strategy: ContentStrategy) -> list[SeedPost]:
    """28 posts over ten days, 24h snapshots. Hooks number and how_to are comparable;
    question has too few posts to compare. Some snapshots must be excluded."""
    start = utc_now() - timedelta(days=10)
    posts: list[SeedPost] = []
    for i in range(13):
        for hook, likes, replies, reposts in (
            (HookType.NUMBER, 40 + i, 5, 4),
            (HookType.HOW_TO, 22 + i, 3, 2),
        ):
            posts.append(
                SeedPost(
                    account=account,
                    strategy=strategy,
                    hook_type=hook,
                    published_at=start + timedelta(hours=7 * len(posts)),
                    length=80 + 15 * (i % 8),
                    metrics={
                        "likes": likes,
                        "replies": replies,
                        "reposts": reposts,
                        "quotes": 1,
                        "bookmarks": 2,
                        "impressions": 1000 + 50 * i,
                    },
                )
            )
    for _ in range(3):
        posts.append(
            SeedPost(
                account=account,
                strategy=strategy,
                hook_type=HookType.QUESTION,
                published_at=start + timedelta(hours=7 * len(posts)),
                metrics={"likes": 30, "replies": 9, "reposts": 1, "quotes": 0, "impressions": 900},
            )
        )
    base = start + timedelta(days=5)
    excluded = [
        # collected 3 hours late: listed, never compared
        SeedPost(
            account, strategy, HookType.NUMBER, base, {"likes": 90}, capture_offset_seconds=10_800
        ),
        # impressions not reported: counts usable, rates not; never treated as 0
        SeedPost(
            account,
            strategy,
            HookType.NUMBER,
            base + timedelta(hours=1),
            {"likes": 41, "replies": 5, "reposts": 4, "quotes": 1},
        ),
        # zero impressions: no rate (no division by zero)
        SeedPost(
            account,
            strategy,
            HookType.HOW_TO,
            base + timedelta(hours=2),
            {"likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "impressions": 0},
        ),
        # no snapshot was ever taken
        SeedPost(account, strategy, HookType.HOW_TO, base + timedelta(hours=3), None),
        # a real (X) snapshot never enters a synthetic evaluation
        SeedPost(
            account,
            strategy,
            HookType.NUMBER,
            base + timedelta(hours=4),
            {"likes": 5},
            provider="x",
        ),
        # published an hour ago: its 24h snapshot is not due yet
        SeedPost(account, strategy, HookType.HOW_TO, utc_now() - timedelta(hours=1), None),
    ]
    return posts + excluded


def show_groups(groups: list[GroupStats], dimension: Dimension) -> None:
    for g in (g for g in groups if g.dimension is dimension):
        rate = next(m for m in g.metrics if m.metric is Metric.ENGAGEMENT_RATE)
        likes = next(m for m in g.metrics if m.metric is Metric.LIKES)
        print(
            f"    {g.value:<10} n={g.n_eligible:<3} {g.evidence_level.value:<12} "
            f"median likes={likes.median}  median engagement rate={rate.median} "
            f"(pooled {rate.pooled_rate}, rate n={rate.denominator_n})"
        )


def main() -> int:
    settings = AppSettings()
    if settings.database_url is None:
        print("Set DATABASE_URL to a scratch PostgreSQL database (see docs/DATABASE.md).")
        return 2
    upgrade(settings.database_url.get_secret_value())
    token = secrets.token_urlsafe(24)  # never printed
    settings = settings.model_copy(
        update={
            "llm_provider": "fake",
            "research_provider": "mock",
            "publisher_provider": "mock",
            "strategy_proposer": "fake",
            "strategy_review_token": SecretStr(token),
        }
    )
    runtime = build(settings)
    strategies, evaluations = runtime.strategies, runtime.evaluations
    assert strategies is not None and evaluations is not None
    auth = ReviewAuth(token=SecretStr(token)).authenticate(f"Bearer {token}")

    print("== 1. register and activate strategy v1 (review token required)")
    account = Account(id=new_id("acct"), handle="@phase7_demo", niche="AI engineering")
    v1 = ContentStrategy(
        id=new_id("strat"),
        account_id=account.id,
        pillars=["agent engineering", "llm evaluation"],
        tone="practical, specific, no hype",
        target_audience="engineers shipping LLM features",
        preferred_hooks=[HookType.HOW_TO],
    )
    registered = strategies.register_version(
        v1, reviewer="demo-reviewer", auth=auth, note="baseline"
    )
    active = strategies.activate(
        v1.id, 1, expected_active_version_id=None, reviewer="demo-reviewer", auth=auth, note=None
    )
    print(f"    {registered.version.strategy_id} v1 active ({active.activation.auth_method})")

    print("\n== 2. synthetic published posts with 24h snapshots (labelled mock_analytics)")
    posts = demo_posts(account, v1)
    seed_posts(runtime.db, posts)
    print(f"    {len(posts)} posts seeded")

    print("\n== 3. deterministic evaluation (no model, no platform call)")
    request = EvaluationRequest(
        account_id=account.id,
        snapshot_age="24h",
        synthetic=True,
        data_cutoff=utc_now(),  # fixed, so repeating the request is idempotent
        created_by="demo",
    )
    created = evaluations.create(request)
    ev = created.evaluation
    print(
        f"    {ev.id}: {ev.status.value}, eligible={ev.eligible_count} excluded={ev.excluded_count}"
    )
    for reason, n in ev.summary.excluded_by_reason.items():
        print(f"      excluded {n} x {reason}")
    print("    by declared hook type:")
    show_groups(ev.summary.groups, Dimension.HOOK_TYPE)
    for c in ev.summary.comparisons:
        if c.dimension is Dimension.HOOK_TYPE:
            print(f"    comparison {c.comparison_id} ({c.interpretation})")
            for note in c.confounders:
                print(f"      confounder: {note}")
    again = evaluations.create(request)
    print(
        f"    same request again: created={again.created}, same id={again.evaluation.id == ev.id}"
    )
    real = evaluations.create(request.model_copy(update={"synthetic": False}))
    print(
        f"    as a real evaluation: {real.evaluation.status.value}, "
        f"excluded {real.evaluation.summary.excluded_by_reason}"
    )

    print("\n== 4. proposal from the evaluation (fake Strategy Agent, validated)")
    proposal = strategies.request_proposal(ev.id, requested_by="demo")
    assert proposal.proposal is not None and proposal.proposal_sha256 is not None
    print(f"    {proposal.id}: {proposal.status.value}, outcome {proposal.outcome}")
    for change in proposal.proposal.changes:
        print(f"    {change.field.value}: {change.current_value} -> {change.proposed_value}")
        print(f"      evidence: {change.evidence_refs}")
        print(f"      {change.hypothesis}; confidence {change.confidence.value}")

    print("\n== 5. human approval creates v2; it is not active")
    approved = strategies.approve(
        proposal.id,
        reviewer="demo-reviewer",
        auth=auth,
        summary="Try number hooks for a while.",
        proposal_sha256=proposal.proposal_sha256,
        evaluation_digest=proposal.evaluation_digest,
    )
    assert approved.decision is not None
    history = strategies.history(v1.id)
    for v in history.versions:
        hooks = [h.value for h in v.content.preferred_hooks]
        print(f"    v{v.version} hooks={hooks} active={v.active}")
    try:
        strategies.approve(
            proposal.id,
            reviewer="demo-reviewer",
            auth=auth,
            summary="again",
            proposal_sha256=proposal.proposal_sha256,
            evaluation_digest=proposal.evaluation_digest,
        )
    except ProposalStateError as exc:
        print(f"    second approval refused: {exc}")

    print("\n== 6. explicit activation, then a future run uses v2")
    activated = strategies.activate(
        v1.id,
        2,
        expected_active_version_id=active.version.id,
        reviewer="demo-reviewer",
        auth=auth,
        note="approved in the demo",
    )
    print(f"    active: v{activated.version.version} (seq {activated.activation.seq})")
    run = runtime.service.create_run(account, strategies.active_strategy(account.id))
    print(
        f"    run {run.id}: strategy v{run.strategy_version}, "
        f"version ref {run.strategy_version_ref}, status {run.status.value}"
    )
    pubs = runtime.publications.list(run_id=run.id, status=None, limit=10)
    print(f"    publications for that run: {len(pubs)} (approval and activation publish nothing)")
    runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
