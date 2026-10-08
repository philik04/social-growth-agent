"""SQLAlchemy table definitions: the queryable record of every run.

These tables are a projection of graph state (see ``recorder.py``), plus two usage
ledgers that are also written at call time. Execution state (where a run resumes)
lives in LangGraph's checkpoint tables, which are not declared here.

Provenance is enforced with foreign keys: a finding's evidence must reference a post
retrieved in the same run, and every critique, decision and candidate link points at
rows of the same run.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
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


Index("ix_runs_created_at", RunRow.created_at.desc())

APP_TABLES: tuple[str, ...] = tuple(Base.metadata.tables)
