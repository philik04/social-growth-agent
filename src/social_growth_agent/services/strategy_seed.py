"""Synthetic published posts with snapshots, for the Phase 7 demo and tests only.

Writes the same rows Phases 4-6 would (run, candidate, publication, analytics request,
job and snapshot) directly, so many posts with chosen hooks, times and metrics can be set
up in milliseconds. Never used by the API or the workers. Snapshots are labelled with the
provider given (``mock_analytics`` by default), which marks them as synthetic: they never
enter a real (non-synthetic) evaluation.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from social_growth_agent.models import (
    Account,
    CandidateOrigin,
    ContentStrategy,
    HookType,
    PublicationStatus,
    new_id,
)
from social_growth_agent.persistence import Database
from social_growth_agent.persistence.tables import (
    AnalyticsJobRow,
    AnalyticsRequestRow,
    ContentCandidateRow,
    PostMetricRow,
    PublicationRow,
    RunRow,
)

SYNTHETIC_PROVIDER = "mock_analytics"
_WORDS = ("evidence", "beats", "intuition", "when", "the", "sample", "is", "honest", "and")


@dataclass(frozen=True)
class SeedPost:
    account: Account
    strategy: ContentStrategy
    hook_type: HookType
    published_at: datetime
    metrics: dict[str, int | None] | None = field(default=None)
    """Counts by name (likes, replies, reposts, quotes, bookmarks, impressions); None
    means no snapshot was taken. A missing key means not reported (NULL)."""
    length: int = 120
    origin: CandidateOrigin = CandidateOrigin.GENERATED
    snapshot_age: str = "24h"
    age_seconds: int = 86_400
    capture_offset_seconds: int = 30
    """Actual age minus target age; beyond the tolerance the snapshot is off target."""
    provider: str = SYNTHETIC_PROVIDER
    provider_created_at_known: bool = True


def text_of_length(length: int) -> str:
    words: list[str] = []
    while len(" ".join(words)) < length:
        words.append(_WORDS[len(words) % len(_WORDS)])
    return " ".join(words)[:length]


def seed_posts(db: Database, posts: Sequence[SeedPost]) -> list[str]:
    """Insert each post (one run each); returns the publication ids in order."""
    ids = []
    with db.transaction() as session:
        for post in posts:
            run_id, cand_id, pub_id = new_id("run"), new_id("cand"), new_id("pub")
            content = text_of_length(post.length)
            session.add(
                RunRow(
                    id=run_id,
                    status="approved",
                    research_query="seeded",
                    account=post.account.model_dump(mode="json"),
                    strategy=post.strategy.model_dump(mode="json"),
                    config={},
                    strategy_id=post.strategy.id,
                    strategy_version=post.strategy.version,
                    research_attempts=1,
                    generation_attempts=1,
                    regeneration_rounds=0,
                    edit_rounds=0,
                    review_candidate_ids=[cand_id],
                )
            )
            session.flush()
            session.add(
                ContentCandidateRow(
                    id=cand_id,
                    run_id=run_id,
                    generation_attempt=1,
                    origin=post.origin.value,
                    content=content,
                    topic="seeded topic",
                    hook_type=post.hook_type.value,
                    format="single",
                    target_audience=post.strategy.target_audience,
                    rationale=None,
                    revises_candidate_id=None,
                    strategy_id=post.strategy.id,
                    strategy_version=post.strategy.version,
                    created_at=post.published_at - timedelta(hours=1),
                    position=0,
                )
            )
            session.flush()
            created = post.published_at - timedelta(seconds=2)
            post_id = str(1_908_100_000_000_000_000 + uuid4().int % 10**15)
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
                    published_at=post.published_at,
                    provider_created_at=created if post.provider_created_at_known else None,
                )
            )
            session.flush()
            if post.metrics is not None:
                _snapshot(session, post, pub_id, post_id, created)
            ids.append(pub_id)
    return ids


def _snapshot(
    session: Session, post: SeedPost, pub_id: str, post_id: str, created: datetime
) -> None:
    target = post.published_at + timedelta(seconds=post.age_seconds)
    captured = created + timedelta(seconds=post.age_seconds + post.capture_offset_seconds)
    request_id, job_id = new_id("areq"), new_id("ajob")
    session.add(
        AnalyticsRequestRow(
            id=request_id,
            worker_id="seed",
            provider=post.provider,
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
            snapshot_age=post.snapshot_age,
            age_seconds=post.age_seconds,
            schedule_basis="recorded_published_at",
            basis_at=post.published_at,
            scheduled_for=target,
            origin="publish",
            status="collected",
            attempt_count=1,
            attempt_base=0,
            collected_at=captured,
        )
    )
    session.flush()
    m = post.metrics or {}
    session.add(
        PostMetricRow(
            id=new_id("pm"),
            job_id=job_id,
            publication_id=pub_id,
            request_id=request_id,
            platform="x",
            provider=post.provider,
            metrics_scope="public",
            provider_post_id=post_id,
            snapshot_age=post.snapshot_age,
            target_age_seconds=post.age_seconds,
            scheduled_for=target,
            captured_at=captured,
            provider_created_at=created if post.provider_created_at_known else None,
            recorded_published_at=post.published_at,
            likes=m.get("likes"),
            reposts=m.get("reposts"),
            replies=m.get("replies"),
            quotes=m.get("quotes"),
            bookmarks=m.get("bookmarks"),
            impressions=m.get("impressions"),
        )
    )
    session.flush()
