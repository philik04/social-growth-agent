"""Initial schema: runs, provenance, review and usage tables (Phase 4).

LangGraph's checkpoint tables are not managed here; ``sga-db upgrade`` creates them
with ``PostgresSaver.setup()``.

Revision ID: 0001
Revises:
Create Date: 2026-10-05 15:37:34.595479
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "pricing_versions",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("content", JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pricing_versions")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_node", sa.String(length=64), nullable=True),
        sa.Column("research_query", sa.Text(), nullable=False),
        sa.Column("account", JSON, nullable=False),
        sa.Column("strategy", JSON, nullable=False),
        sa.Column("config", JSON, nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("research_attempts", sa.Integer(), nullable=False),
        sa.Column("generation_attempts", sa.Integer(), nullable=False),
        sa.Column("regeneration_rounds", sa.Integer(), nullable=False),
        sa.Column("edit_rounds", sa.Integer(), nullable=False),
        sa.Column("review_candidate_ids", JSON, nullable=False),
        sa.Column("failure_node", sa.String(length=64), nullable=True),
        sa.Column("failure_type", sa.String(length=128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("pricing_id", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["pricing_id"],
            ["pricing_versions.id"],
            name=op.f("fk_runs_pricing_id_pricing_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index(
        "ix_runs_created_at", "runs", [sa.literal_column("created_at DESC")], unique=False
    )
    op.create_index(op.f("ix_runs_status"), "runs", ["status"], unique=False)
    op.create_table(
        "content_candidates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("hook_type", sa.String(length=32), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("target_audience", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("revises_candidate_id", sa.String(length=64), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["revises_candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_content_candidates_revises_candidate_id_content_candidates"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_content_candidates_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_candidates")),
    )
    op.create_index(
        op.f("ix_content_candidates_run_id"), "content_candidates", ["run_id"], unique=False
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_llm_calls_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index(op.f("ix_llm_calls_run_id"), "llm_calls", ["run_id"], unique=False)
    op.create_table(
        "research_briefs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("limitations", JSON, nullable=False),
        sa.Column("opportunities", JSON, nullable=False),
        sa.Column("source", JSON, nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_research_briefs_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_briefs")),
    )
    op.create_index(op.f("ix_research_briefs_run_id"), "research_briefs", ["run_id"], unique=False)
    op.create_table(
        "research_fetches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("effective_query", sa.Text(), nullable=False),
        sa.Column("max_results", sa.Integer(), nullable=False),
        sa.Column("requests_made", sa.Integer(), nullable=False),
        sa.Column("posts_fetched", sa.Integer(), nullable=False),
        sa.Column("users_fetched", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("rate_limit_remaining", sa.Integer(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_research_fetches_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_fetches")),
    )
    op.create_index(
        op.f("ix_research_fetches_run_id"), "research_fetches", ["run_id"], unique=False
    )
    op.create_table(
        "run_errors",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node", sa.String(length=64), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_errors_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_errors")),
    )
    op.create_index(op.f("ix_run_errors_run_id"), "run_errors", ["run_id"], unique=False)
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_events_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
    )
    op.create_index(op.f("ix_run_events_run_id"), "run_events", ["run_id"], unique=False)
    op.create_table(
        "source_posts",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("author_id", sa.String(length=64), nullable=False),
        sa.Column("author_username", sa.String(length=64), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("post_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lang", sa.String(length=16), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("reposts", sa.Integer(), nullable=True),
        sa.Column("replies", sa.Integer(), nullable=True),
        sa.Column("quotes", sa.Integer(), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_source_posts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "source_id", name=op.f("pk_source_posts")),
    )
    op.create_table(
        "critiques",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("recommended_verdict", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("factual_risk", sa.String(length=16), nullable=False),
        sa.Column("originality_risk", sa.String(length=16), nullable=False),
        sa.Column("tone_match", sa.String(length=16), nullable=False),
        sa.Column("issues", JSON, nullable=False),
        sa.Column("policy_violations", JSON, nullable=False),
        sa.Column("suggested_revision", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_critiques_candidate_id_content_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_critiques_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_critiques")),
    )
    op.create_index(op.f("ix_critiques_candidate_id"), "critiques", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_critiques_run_id"), "critiques", ["run_id"], unique=False)
    op.create_table(
        "research_findings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("brief_id", sa.String(length=64), nullable=True),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.String(length=16), nullable=False),
        sa.Column("signal_strength", sa.Float(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["brief_id"],
            ["research_briefs.id"],
            name=op.f("fk_research_findings_brief_id_research_briefs"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_research_findings_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_findings")),
    )
    op.create_index(
        op.f("ix_research_findings_run_id"), "research_findings", ["run_id"], unique=False
    )
    op.create_table(
        "review_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=True),
        sa.Column("resulting_candidate_id", sa.String(length=64), nullable=True),
        sa.Column("edited_content", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_review_decisions_candidate_id_content_candidates"),
        ),
        sa.ForeignKeyConstraint(
            ["resulting_candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_review_decisions_resulting_candidate_id_content_candidates"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_review_decisions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_decisions")),
    )
    op.create_index(
        op.f("ix_review_decisions_run_id"), "review_decisions", ["run_id"], unique=False
    )
    op.create_table(
        "candidate_findings",
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("finding_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_candidate_findings_candidate_id_content_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["research_findings.id"],
            name=op.f("fk_candidate_findings_finding_id_research_findings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("candidate_id", "finding_id", name=op.f("pk_candidate_findings")),
    )
    op.create_table(
        "finding_evidence",
        sa.Column("finding_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["research_findings.id"],
            name=op.f("fk_finding_evidence_finding_id_research_findings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "source_id"],
            ["source_posts.run_id", "source_posts.source_id"],
            name="fk_finding_evidence_source_post",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("finding_id", "source_id", name=op.f("pk_finding_evidence")),
    )


def downgrade() -> None:
    op.drop_table("finding_evidence")
    op.drop_table("candidate_findings")
    op.drop_index(op.f("ix_review_decisions_run_id"), table_name="review_decisions")
    op.drop_table("review_decisions")
    op.drop_index(op.f("ix_research_findings_run_id"), table_name="research_findings")
    op.drop_table("research_findings")
    op.drop_index(op.f("ix_critiques_run_id"), table_name="critiques")
    op.drop_index(op.f("ix_critiques_candidate_id"), table_name="critiques")
    op.drop_table("critiques")
    op.drop_table("source_posts")
    op.drop_index(op.f("ix_run_events_run_id"), table_name="run_events")
    op.drop_table("run_events")
    op.drop_index(op.f("ix_run_errors_run_id"), table_name="run_errors")
    op.drop_table("run_errors")
    op.drop_index(op.f("ix_research_fetches_run_id"), table_name="research_fetches")
    op.drop_table("research_fetches")
    op.drop_index(op.f("ix_research_briefs_run_id"), table_name="research_briefs")
    op.drop_table("research_briefs")
    op.drop_index(op.f("ix_llm_calls_run_id"), table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index(op.f("ix_content_candidates_run_id"), table_name="content_candidates")
    op.drop_table("content_candidates")
    op.drop_index(op.f("ix_runs_status"), table_name="runs")
    op.drop_index("ix_runs_created_at", table_name="runs")
    op.drop_table("runs")
    op.drop_table("pricing_versions")
