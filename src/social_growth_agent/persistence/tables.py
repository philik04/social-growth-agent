"""SQLAlchemy table definitions: the queryable record of every run.

These tables are a projection of graph state (see ``recorder.py``), plus usage ledgers
that are written at call time (``research_fetches``, ``llm_calls``, and since Phase 5 the
started/finished ``provider_operations``). Publishing (Phase 5) is not graph state: the
``publications`` and ``publication_attempts`` tables are written only by the publication
service and the publisher worker. Analytics (Phase 6) is not graph state either:
``analytics_jobs``, ``analytics_requests``, ``analytics_attempts`` and ``post_metrics``
are written by the analytics service and worker. Execution state (where a run resumes)
lives in LangGraph's checkpoint tables, which are not declared here.

Provenance is enforced with foreign keys: a finding's evidence must reference a post
retrieved in the same run, and every critique, decision and candidate link points at
rows of the same run.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
    func,
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


Index("ix_runs_created_at", RunRow.created_at.desc())

APP_TABLES: tuple[str, ...] = tuple(Base.metadata.tables)
