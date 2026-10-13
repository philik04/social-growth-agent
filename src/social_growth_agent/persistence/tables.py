"""SQLAlchemy table definitions: the queryable record of every run.

These tables are a projection of graph state (see ``recorder.py``), plus usage ledgers
that are written at call time (``research_fetches``, ``llm_calls``, and since Phase 5 the
started/finished ``provider_operations``). Publishing (Phase 5) is not graph state: the
``publications`` and ``publication_attempts`` tables are written only by the publication
service and the publisher worker. Analytics (Phase 6) is not graph state either:
``analytics_jobs``, ``analytics_requests``, ``analytics_attempts`` and ``post_metrics``
are written by the analytics service and worker. The strategy tables (Phase 7) are
written only by the evaluation and strategy services; five of them are append-only,
enforced by a trigger from migration 0005. The experiment tables (Phase 8) are written
only by the experiment service; arms, assignments, events and results are append-only
(migration 0006). Execution state (where a run resumes)
lives in LangGraph's checkpoint tables, which are not declared here.

Provenance is enforced with foreign keys: a finding's evidence must reference a post
retrieved in the same run, and every critique, decision and candidate link points at
rows of the same run.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Json = JSON().with_variant(JSONB(), "postgresql")
Timestamp = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class PricingVersionRow(Base):
    """An immutable snapshot of the pricing configuration a run was estimated with."""

    __tablename__ = "pricing_versions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8))
    content: Mapped[dict[str, Any]] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_node: Mapped[str | None] = mapped_column(String(64))
    research_query: Mapped[str] = mapped_column(Text)
    account: Mapped[dict[str, Any]] = mapped_column(Json)
    strategy: Mapped[dict[str, Any]] = mapped_column(Json)
    config: Mapped[dict[str, Any]] = mapped_column(Json)
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[int] = mapped_column(Integer)
    research_attempts: Mapped[int] = mapped_column(Integer, default=0)
    generation_attempts: Mapped[int] = mapped_column(Integer, default=0)
    regeneration_rounds: Mapped[int] = mapped_column(Integer, default=0)
    edit_rounds: Mapped[int] = mapped_column(Integer, default=0)
    review_candidate_ids: Mapped[list[str]] = mapped_column(Json, default=list)
    failure_node: Mapped[str | None] = mapped_column(String(64))
    failure_type: Mapped[str | None] = mapped_column(String(128))
    failure_message: Mapped[str | None] = mapped_column(Text)
    pricing_id: Mapped[str | None] = mapped_column(ForeignKey("pricing_versions.id"))
    strategy_version_ref: Mapped[str | None] = mapped_column(ForeignKey("strategy_versions.id"))
    # Phase 9 (0007): the discovery opportunity a run was started from (NULL otherwise).
    opportunity_id: Mapped[str | None] = mapped_column(
        ForeignKey("discovery_opportunities.id"), index=True
    )
    """The registered strategy version this run used (Phase 7); NULL for runs created with
    an unregistered inline strategy and for every run created before Phase 7."""
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp, server_default=func.now(), onupdate=func.now()
    )


def _run_fk() -> Any:
    return ForeignKey("runs.id", ondelete="CASCADE")


class ResearchFetchRow(Base):
    """Usage ledger for platform retrievals (written at call time and from state)."""

    __tablename__ = "research_fetches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    effective_query: Mapped[str] = mapped_column(Text)
    max_results: Mapped[int] = mapped_column(Integer)
    requests_made: Mapped[int] = mapped_column(Integer)
    posts_fetched: Mapped[int] = mapped_column(Integer)
    users_fetched: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(16))
    error_category: Mapped[str | None] = mapped_column(String(32))
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(Timestamp)
    started_at: Mapped[datetime] = mapped_column(Timestamp)


class LLMCallRow(Base):
    """Usage ledger for model calls (written at call time and from state)."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    agent: Mapped[str] = mapped_column(String(32))
    task: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    generation_attempt: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[float] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(Timestamp)


class SourcePostRow(Base):
    __tablename__ = "source_posts"

    run_id: Mapped[str] = mapped_column(_run_fk(), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    author_id: Mapped[str] = mapped_column(String(64))
    author_username: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    post_created_at: Mapped[datetime] = mapped_column(Timestamp)
    lang: Mapped[str | None] = mapped_column(String(16))
    likes: Mapped[int | None] = mapped_column(Integer)
    reposts: Mapped[int | None] = mapped_column(Integer)
    replies: Mapped[int | None] = mapped_column(Integer)
    quotes: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    query: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(Timestamp)


class ResearchBriefRow(Base):
    __tablename__ = "research_briefs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    topic: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16))
    limitations: Mapped[list[str]] = mapped_column(Json)
    opportunities: Mapped[list[dict[str, Any]]] = mapped_column(Json)
    source: Mapped[dict[str, Any]] = mapped_column(Json)


class ResearchFindingRow(Base):
    __tablename__ = "research_findings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    brief_id: Mapped[str | None] = mapped_column(ForeignKey("research_briefs.id"))
    theme: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(16))
    signal_strength: Mapped[float] = mapped_column(Float)
    position: Mapped[int] = mapped_column(Integer)
    # Phase 9 (0007): discovery runs only, validated against the cited evidence.
    claim_kind: Mapped[str | None] = mapped_column(String(24))
    verification: Mapped[str | None] = mapped_column(String(24))


class FindingEvidenceRow(Base):
    """Provenance: the database refuses evidence for a post the run did not retrieve."""

    __tablename__ = "finding_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "source_id"],
            ["source_posts.run_id", "source_posts.source_id"],
            ondelete="CASCADE",
            name="fk_finding_evidence_source_post",
        ),
    )

    finding_id: Mapped[str] = mapped_column(
        ForeignKey("research_findings.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)


class ContentCandidateRow(Base):
    __tablename__ = "content_candidates"
    __table_args__ = (
        # Target of the publications FK: a publication's candidate belongs to its run.
        UniqueConstraint("run_id", "id", name="uq_content_candidates_run_id_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    generation_attempt: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(Text)
    hook_type: Mapped[str] = mapped_column(String(32))
    format: Mapped[str] = mapped_column(String(16))
    target_audience: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    revises_candidate_id: Mapped[str | None] = mapped_column(ForeignKey("content_candidates.id"))
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    position: Mapped[int] = mapped_column(Integer)
    content_intent: Mapped[str | None] = mapped_column(String(32))  # Phase 9 (0007)


class CandidateFindingRow(Base):
    __tablename__ = "candidate_findings"

    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("content_candidates.id", ondelete="CASCADE"), primary_key=True
    )
    finding_id: Mapped[str] = mapped_column(
        ForeignKey("research_findings.id", ondelete="CASCADE"), primary_key=True
    )


class CritiqueRow(Base):
    __tablename__ = "critiques"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("content_candidates.id", ondelete="CASCADE"), index=True
    )
    generation_attempt: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(16))
    recommended_verdict: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    factual_risk: Mapped[str] = mapped_column(String(16))
    originality_risk: Mapped[str] = mapped_column(String(16))
    tone_match: Mapped[str] = mapped_column(String(16))
    issues: Mapped[list[dict[str, Any]]] = mapped_column(Json)
    policy_violations: Mapped[list[dict[str, Any]]] = mapped_column(Json)
    suggested_revision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    position: Mapped[int] = mapped_column(Integer)


class ReviewDecisionRow(Base):
    __tablename__ = "review_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    action: Mapped[str] = mapped_column(String(16))
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("content_candidates.id"))
    resulting_candidate_id: Mapped[str | None] = mapped_column(ForeignKey("content_candidates.id"))
    edited_content: Mapped[str | None] = mapped_column(Text)
    reviewed_candidate_ids: Mapped[list[str]] = mapped_column(Json, default=list)
    """The review request this decision answered; empty for pre-Phase 5 decisions."""
    note: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(128))
    decided_at: Mapped[datetime] = mapped_column(Timestamp)


class RunEventRow(Base):
    """Graph transitions: one row per node execution."""

    __tablename__ = "run_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    node: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    generation_attempt: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[float] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(Timestamp)


class RunErrorRow(Base):
    __tablename__ = "run_errors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    node: Mapped[str] = mapped_column(String(64))
    error_type: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    generation_attempt: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(Timestamp)


PUBLICATION_STATUSES = (
    "scheduled",
    "ready",
    "publishing",
    "published",
    "failed",
    "unknown",
    "cancelled",
)


class PublicationRow(Base):
    """One durable publication intent per (run, candidate, platform).

    Committed before any platform call. Retries reuse this row; the database refuses a
    second intent for the same key. Runs with publications cannot be deleted: the
    record of an external side effect must not disappear with its run.
    """

    __tablename__ = "publications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            ["content_candidates.run_id", "content_candidates.id"],
            name="fk_publications_run_candidate",
        ),
        UniqueConstraint("run_id", "candidate_id", "platform", name="uq_publications_target"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in PUBLICATION_STATUSES) + ")",
            name="status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        Index("ix_publications_due", "status", "scheduled_for"),
        # Phase 9 (0007): find the publication of a platform post (manual registration).
        # Not unique: mock publishers of earlier phases may have reused ids.
        Index(
            "ix_publications_provider_post",
            "platform",
            "provider_post_id",
            postgresql_where=text("provider_post_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    scheduled_for: Mapped[datetime | None] = mapped_column(Timestamp)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    lease_expires_at: Mapped[datetime | None] = mapped_column(Timestamp)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_post_id: Mapped[str | None] = mapped_column(String(64))
    provider_post_url: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    published_at: Mapped[datetime | None] = mapped_column(Timestamp)
    """When this application *recorded* the publication as published (worker success, or
    a human resolve). Not the platform's creation time: see ``provider_created_at``."""
    provider_created_at: Mapped[datetime | None] = mapped_column(Timestamp)
    """The post's creation time as reported by the platform (Phase 6, first metrics
    read). NULL until observed; never filled in from ``published_at``."""
    failure_category: Mapped[str | None] = mapped_column(String(32))
    failure_message: Mapped[str | None] = mapped_column(Text)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # A rate-limited row requeued to ``ready`` is not claimable before this time.
    retry_not_before: Mapped[datetime | None] = mapped_column(Timestamp)
    resolved_by: Mapped[str | None] = mapped_column(String(128))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp, server_default=func.now(), onupdate=func.now()
    )


class PublicationAttemptRow(Base):
    """Started ledger for publishing: inserted and committed *before* the platform call,
    finalized after it. A row left at ``started`` with no ``finished_at`` means the
    process died while the call may have been in flight."""

    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint("publication_id", "attempt", name="uq_publication_attempts_attempt"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(Integer)
    failure_category: Mapped[str | None] = mapped_column(String(32))
    failure_message: Mapped[str | None] = mapped_column(Text)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(Timestamp)
    provider_post_id: Mapped[str | None] = mapped_column(String(64))


class ProviderOperationRow(Base):
    """Generalized started/finished ledger: one row per provider-calling node attempt.

    Inserted (committed) when the node starts and finalized when it returns or raises.
    A row with no ``finished_at`` is an operation whose process died mid-call. No usage
    or cost is ever inferred for it: the usage ledgers only hold what providers reported.
    """

    __tablename__ = "provider_operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(_run_fk(), index=True)
    node: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(64))
    generation_attempt: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)
    outcome: Mapped[str] = mapped_column(String(16))
    error_type: Mapped[str | None] = mapped_column(String(128))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    usage_records: Mapped[int | None] = mapped_column(Integer)


# --- analytics (Phase 6) ----------------------------------------------------------------

ANALYTICS_JOB_STATUSES = ("scheduled", "collecting", "collected", "failed", "cancelled")


class AnalyticsJobRow(Base):
    """One planned metrics snapshot of one published post (work state, leased).

    ``UNIQUE (publication_id, snapshot_age)``: enqueueing twice (publish, backfill, a
    restart) can never plan a second snapshot for the same age.
    """

    __tablename__ = "analytics_jobs"
    __table_args__ = (
        UniqueConstraint("publication_id", "snapshot_age", name="uq_analytics_jobs_target"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in ANALYTICS_JOB_STATUSES) + ")",
            name="status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        Index("ix_analytics_jobs_due", "status", "scheduled_for"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    snapshot_age: Mapped[str] = mapped_column(String(16))
    age_seconds: Mapped[int] = mapped_column(Integer)
    schedule_basis: Mapped[str] = mapped_column(String(32))
    basis_at: Mapped[datetime] = mapped_column(Timestamp)
    scheduled_for: Mapped[datetime] = mapped_column(Timestamp)
    original_scheduled_for: Mapped[datetime | None] = mapped_column(Timestamp)
    """Set when reconciliation moved ``scheduled_for`` to the verified creation time."""
    origin: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    lease_expires_at: Mapped[datetime | None] = mapped_column(Timestamp)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    attempt_base: Mapped[int] = mapped_column(Integer, default=0)
    """``attempt_count`` at the last manual retry: the automatic bound applies to the
    attempts made since (``attempt_count - attempt_base``)."""
    retry_not_before: Mapped[datetime | None] = mapped_column(Timestamp)
    failure_category: Mapped[str | None] = mapped_column(String(32))
    failure_message: Mapped[str | None] = mapped_column(Text)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(Timestamp)
    collected_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp, server_default=func.now(), onupdate=func.now()
    )


class AnalyticsRequestRow(Base):
    """Started ledger for analytics: one row per provider request, committed before the
    call and finalized after it. No ``finished_at`` means the process died mid-call."""

    __tablename__ = "analytics_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))
    metrics_scope: Mapped[str] = mapped_column(String(32))
    post_ids: Mapped[list[str]] = mapped_column(Json)
    started_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    posts_returned: Mapped[int | None] = mapped_column(Integer)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(Timestamp)


class AnalyticsAttemptRow(Base):
    """One job's part in one request: the exact response-to-publication mapping, and the
    per-publication read count used for cost."""

    __tablename__ = "analytics_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt", name="uq_analytics_attempts_attempt"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("analytics_requests.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("analytics_jobs.id"), index=True)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    provider_post_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16))
    failure_category: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)


class PostMetricRow(Base):
    """An immutable metrics observation. Written only when the platform returned the
    post; every count is nullable (NULL = not reported, never a stand-in for 0).

    ``UNIQUE (job_id)`` and ``UNIQUE (publication_id, snapshot_age)``: a restart, a
    duplicate claim or a re-run can never store a second snapshot for the same age.
    """

    __tablename__ = "post_metrics"
    __table_args__ = (
        UniqueConstraint("publication_id", "snapshot_age", name="uq_post_metrics_target"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("analytics_jobs.id"), unique=True)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("analytics_requests.id"))
    platform: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    metrics_scope: Mapped[str] = mapped_column(String(32))
    provider_post_id: Mapped[str] = mapped_column(String(64))
    snapshot_age: Mapped[str] = mapped_column(String(16))
    target_age_seconds: Mapped[int] = mapped_column(Integer)
    scheduled_for: Mapped[datetime] = mapped_column(Timestamp)
    captured_at: Mapped[datetime] = mapped_column(Timestamp)
    provider_created_at: Mapped[datetime | None] = mapped_column(Timestamp)
    recorded_published_at: Mapped[datetime | None] = mapped_column(Timestamp)
    likes: Mapped[int | None] = mapped_column(Integer)
    reposts: Mapped[int | None] = mapped_column(Integer)
    replies: Mapped[int | None] = mapped_column(Integer)
    quotes: Mapped[int | None] = mapped_column(Integer)
    bookmarks: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


# --- strategy evaluation (Phase 7) -------------------------------------------------------
# Append-only tables (versions, activations, evaluations, observations, decisions) are
# also protected by a database trigger that rejects UPDATE and DELETE (migration 0005).

IMMUTABLE_TABLES = (
    "strategy_versions",
    "strategy_activations",
    "performance_evaluations",
    "evaluation_observations",
    "strategy_proposal_decisions",
)
STRATEGY_VERSION_ORIGINS = ("registered", "imported_from_run", "proposal", "experiment")
PROPOSAL_STATUSES = (
    "generating",
    "pending_review",
    "failed",
    "approved",
    "rejected",
    "superseded",
    "abandoned",
)
DECISION_KINDS = ("approve", "reject")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class StrategyVersionRow(Base):
    """An immutable strategy version. ``UNIQUE (strategy_id, version)``."""

    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_version"),
        CheckConstraint(_in("origin", STRATEGY_VERSION_ORIGINS), name="origin"),
        CheckConstraint("version >= 1", name="version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(Json)
    content_sha256: Mapped[str] = mapped_column(String(64))
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("strategy_versions.id"))
    origin: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str | None] = mapped_column(String(128))
    auth_method: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    # Phase 8 (0006): the concluded experiment a version was adopted from; one adoption
    # per result.
    source_experiment_result_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "experiment_results.id",
            use_alter=True,
            name="fk_strategy_versions_source_experiment_result",
        ),
        unique=True,
    )


class StrategyActivationRow(Base):
    """Append-only activation history. The active version of an account is the row with
    the highest ``seq``; ``UNIQUE (account_id, seq)`` lets only one of two concurrent
    activations win."""

    __tablename__ = "strategy_activations"
    __table_args__ = (
        UniqueConstraint("account_id", "seq", name="uq_strategy_activations_seq"),
        CheckConstraint("seq >= 1", name="seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    strategy_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    previous_version_id: Mapped[str | None] = mapped_column(ForeignKey("strategy_versions.id"))
    activated_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime] = mapped_column(Timestamp)


class PerformanceEvaluationRow(Base):
    """An immutable, reproducible evaluation (the evidence package). ``input_digest`` is
    the normalized request: the same request returns the same evaluation."""

    __tablename__ = "performance_evaluations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    snapshot_age: Mapped[str] = mapped_column(String(16))
    published_since: Mapped[datetime | None] = mapped_column(Timestamp)
    published_until: Mapped[datetime | None] = mapped_column(Timestamp)
    data_cutoff: Mapped[datetime] = mapped_column(Timestamp)
    synthetic: Mapped[bool] = mapped_column(Boolean)
    config: Mapped[dict[str, Any]] = mapped_column(Json)
    config_version: Mapped[str] = mapped_column(String(16))
    request: Mapped[dict[str, Any]] = mapped_column(Json)
    input_digest: Mapped[str] = mapped_column(String(64), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    eligible_count: Mapped[int] = mapped_column(Integer)
    excluded_count: Mapped[int] = mapped_column(Integer)
    posts_considered: Mapped[int] = mapped_column(Integer)
    snapshots_used: Mapped[int] = mapped_column(Integer)
    summary: Mapped[dict[str, Any]] = mapped_column(Json)
    duration_ms: Mapped[float] = mapped_column(Float)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class EvaluationObservationRow(Base):
    """One published post's part in one evaluation. Content and lineage are not copied:
    ``publication_id`` and ``post_metric_id`` reach them."""

    __tablename__ = "evaluation_observations"

    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("performance_evaluations.id"), primary_key=True
    )
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), primary_key=True)
    post_metric_id: Mapped[str | None] = mapped_column(ForeignKey("post_metrics.id"))
    run_id: Mapped[str] = mapped_column(String(64))
    candidate_id: Mapped[str] = mapped_column(String(64))
    eligible: Mapped[bool] = mapped_column(Boolean)
    exclusion_reason: Mapped[str | None] = mapped_column(String(48))
    metrics_provider: Mapped[str | None] = mapped_column(String(32))
    actual_age_seconds: Mapped[int | None] = mapped_column(Integer)
    hook_type: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(16))
    format: Mapped[str] = mapped_column(String(16))
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[int] = mapped_column(Integer)
    strategy_digest: Mapped[str] = mapped_column(String(64))
    strategy_group: Mapped[str] = mapped_column(String(160))
    length_chars: Mapped[int] = mapped_column(Integer)
    length_bucket: Mapped[str] = mapped_column(String(32))
    hour_utc: Mapped[int | None] = mapped_column(Integer)
    hour_block_utc: Mapped[str | None] = mapped_column(String(8))
    likes: Mapped[int | None] = mapped_column(Integer)
    replies: Mapped[int | None] = mapped_column(Integer)
    reposts: Mapped[int | None] = mapped_column(Integer)
    quotes: Mapped[int | None] = mapped_column(Integer)
    bookmarks: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)


class StrategyProposalRow(Base):
    """A proposal and its own started/finished ledger: committed as ``generating`` before
    the model call, finalized after it. At most one open proposal (``generating`` or
    ``pending_review``) per account, enforced by a partial unique index."""

    __tablename__ = "strategy_proposals"
    __table_args__ = (
        CheckConstraint(_in("status", PROPOSAL_STATUSES), name="status"),
        Index(
            "uq_strategy_proposals_open",
            "account_id",
            unique=True,
            postgresql_where=text("status IN ('generating', 'pending_review')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("performance_evaluations.id"), index=True)
    evaluation_digest: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(64))
    base_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(16))
    proposer: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    proposal: Mapped[dict[str, Any] | None] = mapped_column(Json)
    proposal_sha256: Mapped[str | None] = mapped_column(String(64))
    validation_errors: Mapped[list[str] | None] = mapped_column(Json)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_of: Mapped[str | None] = mapped_column(ForeignKey("strategy_proposals.id"), unique=True)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    pricing_id: Mapped[str] = mapped_column(ForeignKey("pricing_versions.id"))
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    llm_calls: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp, server_default=func.now(), onupdate=func.now()
    )


class StrategyProposalDecisionRow(Base):
    """The human decision on a proposal. ``UNIQUE (proposal_id)``: a proposal is decided
    once, so a second approval (or a reject after an approve) is refused by the database."""

    __tablename__ = "strategy_proposal_decisions"
    __table_args__ = (CheckConstraint(_in("decision", DECISION_KINDS), name="decision"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("strategy_proposals.id"), unique=True)
    decision: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    reviewed_proposal_sha256: Mapped[str | None] = mapped_column(String(64))
    reviewed_evaluation_digest: Mapped[str | None] = mapped_column(String(64))
    resulting_version_id: Mapped[str | None] = mapped_column(ForeignKey("strategy_versions.id"))
    decided_at: Mapped[datetime] = mapped_column(Timestamp)


# --- experiments (Phase 8) ----------------------------------------------------------------
# Arms, assignments, events and results are append-only (trigger from migration 0006). An
# experiment row's status and timestamps change; its design never does (another trigger).

EXPERIMENT_IMMUTABLE_TABLES = (
    "experiment_arms",
    "experiment_assignments",
    "experiment_events",
    "experiment_results",
)
EXPERIMENT_STATUSES = ("draft", "approved", "running", "stopped", "concluded", "cancelled")
EXPERIMENT_EVENT_KINDS = (
    "created",
    "approved",
    "started",
    "stopped",
    "cancelled",
    "unblinded",
    "concluded",
    "adopted",
)


class ExperimentRow(Base):
    """A pre-registered experiment. ``design`` (and its digest) are fixed at creation."""

    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint(_in("status", EXPERIMENT_STATUSES), name="status"),
        CheckConstraint(_in("factor", ("hook_type", "length")), name="factor"),
        Index(
            "uq_experiments_open",
            "account_id",
            unique=True,
            postgresql_where=text("status IN ('approved', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    base_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    factor: Mapped[str] = mapped_column(String(16))
    synthetic: Mapped[bool] = mapped_column(Boolean)
    design: Mapped[dict[str, Any]] = mapped_column(Json)
    design_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_age_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    peeked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(Timestamp)
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    stopped_at: Mapped[datetime | None] = mapped_column(Timestamp)
    unblinded_at: Mapped[datetime | None] = mapped_column(Timestamp)
    concluded_at: Mapped[datetime | None] = mapped_column(Timestamp)
    cancelled_at: Mapped[datetime | None] = mapped_column(Timestamp)


class ExperimentArmRow(Base):
    __tablename__ = "experiment_arms"
    __table_args__ = (
        UniqueConstraint("experiment_id", "label", name="uq_experiment_arms_label"),
        UniqueConstraint("experiment_id", "position", name="uq_experiment_arms_position"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"))
    label: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer)
    is_control: Mapped[bool] = mapped_column(Boolean)
    treatment: Mapped[dict[str, Any]] = mapped_column(Json)


class ExperimentAssignmentRow(Base):
    """One randomized run. ``UNIQUE (run_id)``: a run belongs to at most one arm;
    ``UNIQUE (experiment_id, block_no, position)``: a slot is drawn once."""

    __tablename__ = "experiment_assignments"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "block_no", "position", name="uq_experiment_assignments_slot"
        ),
        CheckConstraint("block_no >= 0 AND position >= 0", name="slot"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    arm_id: Mapped[str] = mapped_column(ForeignKey("experiment_arms.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    block_no: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    assigned_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    assigned_at: Mapped[datetime] = mapped_column(Timestamp)


class ExperimentEventRow(Base):
    __tablename__ = "experiment_events"
    __table_args__ = (CheckConstraint(_in("kind", EXPERIMENT_EVENT_KINDS), name="kind"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(Timestamp)


class ExperimentResultRow(Base):
    """The immutable conclusion. One per experiment."""

    __tablename__ = "experiment_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), unique=True)
    verdict: Mapped[str] = mapped_column(String(40))
    evidence_class: Mapped[str] = mapped_column(String(16))
    peeked: Mapped[bool] = mapped_column(Boolean)
    cutoff: Mapped[datetime] = mapped_column(Timestamp)
    analysis_version: Mapped[str] = mapped_column(String(16))
    input_digest: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    analysis: Mapped[dict[str, Any]] = mapped_column(Json)
    flags: Mapped[list[str]] = mapped_column(Json)
    concluded_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


# --- content discovery (Phase 9, migration 0007) -----------------------------------------

DISCOVERY_SESSION_STATUSES = ("running", "succeeded", "partial", "failed")
DISCOVERY_FETCH_OUTCOMES = (
    "started",
    "succeeded",
    "empty",
    "cached",
    "not_modified",
    "failed",
    "skipped",
)
OPPORTUNITY_STATUSES = ("open", "used", "dismissed")


class DiscoverySessionRow(Base):
    """One bounded, on-demand discovery sweep. Its request, limits, registry digest and
    pricing are fixed at start; status and summary are written once, at the end."""

    __tablename__ = "discovery_sessions"
    __table_args__ = (CheckConstraint(_in("status", DISCOVERY_SESSION_STATUSES), name="status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))
    synthetic: Mapped[bool] = mapped_column(Boolean)
    request: Mapped[dict[str, Any]] = mapped_column(Json)
    limits: Mapped[dict[str, Any]] = mapped_column(Json)
    registry_version: Mapped[str] = mapped_column(String(64))
    registry_sha256: Mapped[str] = mapped_column(String(64))
    strategy_version_id: Mapped[str | None] = mapped_column(ForeignKey("strategy_versions.id"))
    pricing_id: Mapped[str | None] = mapped_column(ForeignKey("pricing_versions.id"))
    requested_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    summary: Mapped[dict[str, Any] | None] = mapped_column(Json)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)


class DiscoveryFetchRow(Base):
    """Started/finished ledger: committed as ``started`` before every external call."""

    __tablename__ = "discovery_fetches"
    __table_args__ = (CheckConstraint(_in("outcome", DISCOVERY_FETCH_OUTCOMES), name="outcome"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("discovery_sessions.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(16))
    request: Mapped[dict[str, Any]] = mapped_column(Json)
    requests_made: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    items_returned: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    post_reads: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    user_reads: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(Timestamp)
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)


class DiscoveryItemRow(Base):
    """One unique external item. Text fields become NULL when purged (retention or a
    deletion request); ids, URL and hash stay as audit metadata. A trigger forbids
    deleting rows, changing their identity and restoring purged text."""

    __tablename__ = "discovery_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(600), unique=True)
    source_id: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(16))
    evidence_type: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(32))
    verification_status: Mapped[str] = mapped_column(String(24))
    publisher: Mapped[str] = mapped_column(String(80))
    provider_item_id: Mapped[str] = mapped_column(String(512))
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(128))
    author_id: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(Timestamp)
    first_retrieved_at: Mapped[datetime] = mapped_column(Timestamp)
    last_retrieved_at: Mapped[datetime] = mapped_column(Timestamp)
    keywords: Mapped[list[str]] = mapped_column(Json)
    claim_hints: Mapped[list[str]] = mapped_column(Json)
    metrics: Mapped[dict[str, Any]] = mapped_column(Json)
    content_sha256: Mapped[str] = mapped_column(String(64))
    evergreen: Mapped[bool] = mapped_column(Boolean, server_default=false())
    synthetic: Mapped[bool] = mapped_column(Boolean)
    first_session_id: Mapped[str] = mapped_column(ForeignKey("discovery_sessions.id"))
    purged_at: Mapped[datetime | None] = mapped_column(Timestamp)
    purge_reason: Mapped[str | None] = mapped_column(String(32))


class DiscoverySightingRow(Base):
    """Which session saw which item, from which fetch (append-only)."""

    __tablename__ = "discovery_item_sightings"

    session_id: Mapped[str] = mapped_column(ForeignKey("discovery_sessions.id"), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("discovery_items.id"), primary_key=True)
    fetch_id: Mapped[str] = mapped_column(ForeignKey("discovery_fetches.id"))
    position: Mapped[int] = mapped_column(Integer)
    seen_at: Mapped[datetime] = mapped_column(Timestamp)


class DiscoveryOpportunityRow(Base):
    """A ranked content opportunity. Only ``status`` and the dismissal fields change."""

    __tablename__ = "discovery_opportunities"
    __table_args__ = (CheckConstraint(_in("status", OPPORTUNITY_STATUSES), name="status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("discovery_sessions.id"), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    keywords: Mapped[list[str]] = mapped_column(Json)
    headline: Mapped[str] = mapped_column(Text)
    components: Mapped[list[dict[str, Any]]] = mapped_column(Json)
    reasons: Mapped[list[str]] = mapped_column(Json)
    claim_hints: Mapped[list[dict[str, Any]]] = mapped_column(Json)
    claim_summary: Mapped[dict[str, Any]] = mapped_column(Json)
    suggested_intents: Mapped[list[str]] = mapped_column(Json)
    grok: Mapped[dict[str, Any]] = mapped_column(Json)
    grok_eligible: Mapped[bool] = mapped_column(Boolean)
    audience_entry_ids: Mapped[list[str]] = mapped_column(Json)
    synthetic: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    dismissed_by: Mapped[str | None] = mapped_column(String(128))
    dismissed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    dismiss_reason: Mapped[str | None] = mapped_column(Text)


class OpportunityEvidenceRow(Base):
    __tablename__ = "opportunity_evidence"

    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("discovery_opportunities.id"), primary_key=True
    )
    item_id: Mapped[str] = mapped_column(ForeignKey("discovery_items.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    position: Mapped[int] = mapped_column(Integer)


class AudienceEntryRow(Base):
    """A read-only audience suggestion: topic signals over public posts, nothing else."""

    __tablename__ = "audience_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("discovery_sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref: Mapped[str] = mapped_column(String(512))
    platform: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    signals: Mapped[list[dict[str, Any]]] = mapped_column(Json)
    matched_topics: Mapped[list[str]] = mapped_column(Json)
    explanation: Mapped[str] = mapped_column(Text)
    evidence_item_ids: Mapped[list[str]] = mapped_column(Json)
    curated: Mapped[bool] = mapped_column(Boolean)
    position: Mapped[int] = mapped_column(Integer)


class ManualExportRow(Base):
    """An approved candidate handed to a person to post by hand (append-only)."""

    __tablename__ = "manual_exports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("content_candidates.id"))
    content_sha256: Mapped[str] = mapped_column(String(64))
    content_intent: Mapped[str | None] = mapped_column(String(32))
    manual_only: Mapped[bool] = mapped_column(Boolean)
    exported_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(Timestamp)


class ManualPublicationRow(Base):
    """A post the user published by hand, verified as the configured account's own and
    linked to a ``publications`` row so analytics and lineage treat it like any other
    published post. One per publication and one per platform post (append-only)."""

    __tablename__ = "manual_publications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), unique=True)
    export_id: Mapped[str] = mapped_column(ForeignKey("manual_exports.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    provider_post_id: Mapped[str] = mapped_column(String(64))
    author_id: Mapped[str] = mapped_column(String(64))
    author_username: Mapped[str | None] = mapped_column(String(64))
    provider_created_at: Mapped[datetime | None] = mapped_column(Timestamp)
    text_match: Mapped[str] = mapped_column(String(16))
    verification_provider: Mapped[str] = mapped_column(String(32))
    verification_post_reads: Mapped[int] = mapped_column(Integer)
    verification_user_reads: Mapped[int] = mapped_column(Integer)
    registered_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    __table_args__ = (
        UniqueConstraint("platform", "provider_post_id", name="uq_manual_publications_post"),
    )


class ContentPurgeRow(Base):
    """Tombstone: this platform item's text was purged (retention or a deletion request)
    and must never be stored, replayed or shown to a model again."""

    __tablename__ = "content_purges"
    __table_args__ = (
        UniqueConstraint("platform", "provider_item_id", name="uq_content_purges_item"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    provider_item_id: Mapped[str] = mapped_column(String(512))
    reason: Mapped[str] = mapped_column(String(32))
    requested_by: Mapped[str] = mapped_column(String(128))
    auth_method: Mapped[str] = mapped_column(String(32))
    counts: Mapped[dict[str, Any]] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(Timestamp)


Index("ix_runs_created_at", RunRow.created_at.desc())

APP_TABLES: tuple[str, ...] = tuple(Base.metadata.tables)
