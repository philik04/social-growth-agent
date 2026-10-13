"""Synthetic experiment runs, for the Phase 8 demo and tests only.

Randomizes exactly like ``ExperimentService.create_run`` (same lock, same permuted-block
draw, same treatment applied to the run's content policy), but instead of executing the
graph it writes what a finished run would leave behind: a candidate that follows the
treatment, a human approval, a publication and a 24h snapshot. Outcomes come from a
caller-supplied function of the arm, so a scenario can set effect sizes, attrition and
missing impressions. Snapshots are labelled ``mock_analytics`` (synthetic) unless a test
asks otherwise; synthetic results are never adoptable.

Never used by the API or the workers.
"""

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from social_growth_agent.graph import RunConfig
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    HookType,
    PublicationStatus,
    RunStatus,
    new_id,
)
from social_growth_agent.models.experiment import ExperimentDesign, Treatment
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import experiments as xdb
from social_growth_agent.persistence import strategies as sdb
from social_growth_agent.persistence.tables import (
    AnalyticsJobRow,
    AnalyticsRequestRow,
    ContentCandidateRow,
    ExperimentAssignmentRow,
    PostMetricRow,
    PublicationRow,
    ReviewDecisionRow,
    RunRow,
)
from social_growth_agent.policies import ContentPolicy
from social_growth_agent.policies.treatment import apply_treatment
from social_growth_agent.services.strategy_seed import SYNTHETIC_PROVIDER, text_of_length

_OPENERS = {
    HookType.QUESTION: "What if the sample is honest? ",
    HookType.NUMBER: "3 notes on honest samples. ",
    HookType.HOW_TO: "How to keep a sample honest: ",
    HookType.STORY: "Last week our sample surprised us. ",
    HookType.CONTRARIAN: "Unpopular: intuition is a sample of one. ",
}


@dataclass(frozen=True)
class SeedOutcome:
    """What happened to one randomized run after assignment."""

    published: bool = True
    metrics: dict[str, int | None] | None = None
    """Snapshot counts; None means no snapshot was taken."""
    approved: bool = True
    generated: bool = True


Outcome = Callable[[str, int, random.Random], SeedOutcome]
"""(arm label, index of the run within its arm, rng) -> outcome."""


def rate_outcome(rates: dict[str, float], *, impressions: int = 1000) -> Outcome:
    """Published posts whose engagement rate scatters around the arm's rate."""

    def outcome(label: str, _: int, rng: random.Random) -> SeedOutcome:
        shown = impressions + rng.randint(-200, 200)
        rate = max(0.0, rng.gauss(rates[label], rates[label] * 0.35))
        engaged = round(shown * rate)
        likes = round(engaged * 0.7)
        replies = round(engaged * 0.15)
        reposts = engaged - likes - replies
        return SeedOutcome(
            metrics={
                "likes": likes,
                "replies": replies,
                "reposts": reposts,
                "quotes": 0,
                "bookmarks": 1,
                "impressions": shown,
            }
        )

    return outcome


def seed_experiment_runs(
    db: Database,
    experiment_id: str,
    *,
    account: Account,
    count: int,
    outcome: Outcome,
    start: datetime,
    spacing: timedelta = timedelta(hours=12),
    rng: random.Random,
    provider: str = SYNTHETIC_PROVIDER,
) -> list[str]:
    """Assign ``count`` runs (oldest first, ``spacing`` apart from ``start``) and write
    their outcomes. Returns the run ids in assignment order."""
    run_ids = []
    per_arm: dict[str, int] = {}
    for i in range(count):
        assigned_at = start + spacing * i
        with db.transaction() as session:
            row = xdb.get(session, experiment_id, lock=True)
            design = ExperimentDesign.model_validate(row.design)
            arm_rows = xdb.arms(session, row.id)
            block_no, position, arm = xdb.draw_slot(xdb.assignments(session, row.id), arm_rows, rng)
            treatment = Treatment.model_validate(arm.treatment)
            policy = apply_treatment(ContentPolicy(), treatment)
            strategy = ContentStrategy.model_validate(
                sdb.get_version(session, row.base_version_id).content
            )
            index = per_arm.get(arm.label, 0)
            per_arm[arm.label] = index + 1
            result = outcome(arm.label, index, rng)
            run_id, cand_id = new_id("run"), new_id("cand")
            status = (
                RunStatus.APPROVED
                if result.approved
                else RunStatus.REJECTED
                if result.generated
                else RunStatus.FAILED
            )
            session.add(
                RunRow(
                    id=run_id,
                    status=status.value,
                    research_query="seeded experiment run",
                    account=account.model_dump(mode="json"),
                    strategy=strategy.model_dump(mode="json"),
                    config=RunConfig(content_policy=policy).model_dump(mode="json"),
                    strategy_id=strategy.id,
                    strategy_version=strategy.version,
                    research_attempts=1,
                    generation_attempts=1 if result.generated else 0,
                    regeneration_rounds=0,
                    edit_rounds=0,
                    review_candidate_ids=[cand_id] if result.generated else [],
                    strategy_version_ref=row.base_version_id,
                )
            )
            session.flush()
            session.add(
                ExperimentAssignmentRow(
                    id=new_id("asg"),
                    experiment_id=row.id,
                    arm_id=arm.id,
                    run_id=run_id,
                    block_no=block_no,
                    position=position,
                    assigned_by="experiment-seed",
                    auth_method="synthetic_seed",
                    assigned_at=assigned_at,
                )
            )
            if result.generated:
                hook = treatment.hook_type or HookType.NUMBER
                content = _content(policy, hook, index)
                session.add(
                    ContentCandidateRow(
                        id=cand_id,
                        run_id=run_id,
                        generation_attempt=1,
                        origin="generated",
                        content=content,
                        topic="honest samples",
                        hook_type=hook.value,
                        format="single",
                        target_audience=strategy.target_audience,
                        rationale=None,
                        revises_candidate_id=None,
                        strategy_id=strategy.id,
                        strategy_version=strategy.version,
                        created_at=assigned_at + timedelta(minutes=5),
                        position=0,
                    )
                )
                session.flush()
                session.add(
                    ReviewDecisionRow(
                        id=new_id("dec"),
                        run_id=run_id,
                        action="approve" if result.approved else "reject",
                        candidate_id=cand_id if result.approved else None,
                        reviewed_candidate_ids=[cand_id],
                        reviewer="seed-reviewer",
                        decided_at=assigned_at + timedelta(hours=1),
                    )
                )
                if result.approved and result.published:
                    _publish(
                        session,
                        run_id,
                        cand_id,
                        content,
                        assigned_at + timedelta(hours=2),
                        result.metrics,
                        design.snapshot_age,
                        row.snapshot_age_seconds,
                        provider,
                    )
            session.flush()
            run_ids.append(run_id)
    return run_ids


def _content(policy: ContentPolicy, hook: HookType, index: int) -> str:
    low = policy.min_post_length or 60
    high = policy.max_post_length
    length = min(high, max(low, low + (index * 7) % max(1, high - low + 1)))
    text = _OPENERS[hook] + text_of_length(max(1, length))
    return text[:length] if len(text) > length else text


def _publish(
    session: Session,
    run_id: str,
    cand_id: str,
    content: str,
    published_at: datetime,
    metrics: dict[str, int | None] | None,
    snapshot_age: str,
    age_seconds: int,
    provider: str,
) -> None:
    pub_id = new_id("pub")
    created = published_at - timedelta(seconds=2)
    post_id = str(1_908_200_000_000_000_000 + uuid4().int % 10**15)
    session.add(
        PublicationRow(
            id=pub_id,
            run_id=run_id,
            candidate_id=cand_id,
            platform="x",
            idempotency_key=new_id("idem"),
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            status=PublicationStatus.PUBLISHED.value,
            attempt_count=1,
            provider="mock",
            provider_post_id=post_id,
            published_at=published_at,
            provider_created_at=created,
        )
    )
    session.flush()
    if metrics is None:
        return
    target = published_at + timedelta(seconds=age_seconds)
    captured = created + timedelta(seconds=age_seconds + 30)
    request_id, job_id = new_id("areq"), new_id("ajob")
    session.add(
        AnalyticsRequestRow(
            id=request_id,
            worker_id="seed",
            provider=provider,
            metrics_scope="public",
            post_ids=[post_id],
            started_at=captured,
            finished_at=captured,
            latency_ms=1.0,
            outcome="succeeded",
            posts_returned=1,
        )
    )
    session.add(
        AnalyticsJobRow(
            id=job_id,
            publication_id=pub_id,
            snapshot_age=snapshot_age,
            age_seconds=age_seconds,
            schedule_basis="recorded_published_at",
            basis_at=published_at,
            scheduled_for=target,
            origin="publish",
            status="collected",
            attempt_count=1,
            attempt_base=0,
            collected_at=captured,
        )
    )
    session.flush()
    session.add(
        PostMetricRow(
            id=new_id("pm"),
            job_id=job_id,
            publication_id=pub_id,
            request_id=request_id,
            platform="x",
            provider=provider,
            metrics_scope="public",
            provider_post_id=post_id,
            snapshot_age=snapshot_age,
            target_age_seconds=age_seconds,
            scheduled_for=target,
            captured_at=captured,
            provider_created_at=created,
            recorded_published_at=published_at,
            likes=metrics.get("likes"),
            reposts=metrics.get("reposts"),
            replies=metrics.get("replies"),
            quotes=metrics.get("quotes"),
            bookmarks=metrics.get("bookmarks"),
            impressions=metrics.get("impressions"),
        )
    )


def backdate_start(db: Database, experiment_id: str, when: datetime) -> None:
    """Move the approval and start of a running synthetic experiment into the past, so
    seeded history fits inside its duration. Timestamps are the only experiment columns
    that may change; the design-freeze trigger still guards everything else."""
    with db.transaction() as session:
        row = xdb.get(session, experiment_id, lock=True)
        row.approved_at = when
        row.started_at = when
