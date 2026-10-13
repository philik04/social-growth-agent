"""Strategy evaluation (Phase 7): evaluations, proposals, decisions, versions, activations.

- ``performance_evaluations`` / ``evaluation_observations``: immutable evidence packages
  computed from ``post_metrics`` (no copies of content or lineage).
- ``strategy_proposals``: model proposals and their own started/finished ledger; at most
  one open proposal per account (partial unique index).
- ``strategy_proposal_decisions``: one human decision per proposal (UNIQUE).
- ``strategy_versions`` / ``strategy_activations``: immutable versions and an append-only
  activation history (the highest ``seq`` per account is active).
- ``runs.strategy_version_ref``: nullable; existing runs stay NULL (no backfill).

Append-only tables get a trigger that rejects UPDATE and DELETE. Applies on top of a
populated Phase 6 database (0004) without touching its rows.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13 01:06:34
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
IMMUTABLE_TABLES = (
    "strategy_versions",
    "strategy_activations",
    "performance_evaluations",
    "evaluation_observations",
    "strategy_proposal_decisions",
)


def upgrade() -> None:
    op.create_table(
        "performance_evaluations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=True),
        sa.Column("snapshot_age", sa.String(length=16), nullable=False),
        sa.Column("published_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("config", JSON, nullable=False),
        sa.Column("config_version", sa.String(length=16), nullable=False),
        sa.Column("request", JSON, nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("posts_considered", sa.Integer(), nullable=False),
        sa.Column("snapshots_used", sa.Integer(), nullable=False),
        sa.Column("summary", JSON, nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_performance_evaluations")),
        sa.UniqueConstraint("input_digest", name=op.f("uq_performance_evaluations_input_digest")),
    )
    op.create_index(
        op.f("ix_performance_evaluations_account_id"),
        "performance_evaluations",
        ["account_id"],
        unique=False,
    )
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", JSON, nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("parent_version_id", sa.String(length=64), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("auth_method", sa.String(length=32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin IN ('registered', 'imported_from_run', 'proposal')",
            name=op.f("ck_strategy_versions_origin"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_strategy_versions_version")),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_strategy_versions_parent_version_id_strategy_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_versions")),
        sa.UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_version"),
    )
    op.create_index(
        op.f("ix_strategy_versions_account_id"), "strategy_versions", ["account_id"], unique=False
    )
    op.create_table(
        "strategy_activations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("strategy_version_id", sa.String(length=64), nullable=False),
        sa.Column("previous_version_id", sa.String(length=64), nullable=True),
        sa.Column("activated_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_strategy_activations_seq")),
        sa.ForeignKeyConstraint(
            ["previous_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_strategy_activations_previous_version_id_strategy_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_strategy_activations_strategy_version_id_strategy_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_activations")),
        sa.UniqueConstraint("account_id", "seq", name="uq_strategy_activations_seq"),
    )
    op.create_table(
        "strategy_proposals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_digest", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("base_version_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("proposer", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("proposal", JSON, nullable=True),
        sa.Column("proposal_sha256", sa.String(length=64), nullable=True),
        sa.Column("validation_errors", JSON, nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_of", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=True),
        sa.Column("pricing_id", sa.String(length=80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('generating', 'pending_review', 'failed', 'approved', 'rejected', "
            "'superseded', 'abandoned')",
            name=op.f("ck_strategy_proposals_status"),
        ),
        sa.ForeignKeyConstraint(
            ["base_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_strategy_proposals_base_version_id_strategy_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["performance_evaluations.id"],
            name=op.f("fk_strategy_proposals_evaluation_id_performance_evaluations"),
        ),
        sa.ForeignKeyConstraint(
            ["pricing_id"],
            ["pricing_versions.id"],
            name=op.f("fk_strategy_proposals_pricing_id_pricing_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["retry_of"],
            ["strategy_proposals.id"],
            name=op.f("fk_strategy_proposals_retry_of_strategy_proposals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_proposals")),
        sa.UniqueConstraint("retry_of", name=op.f("uq_strategy_proposals_retry_of")),
    )
    op.create_index(
        op.f("ix_strategy_proposals_evaluation_id"),
        "strategy_proposals",
        ["evaluation_id"],
        unique=False,
    )
    op.create_index(
        "uq_strategy_proposals_open",
        "strategy_proposals",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('generating', 'pending_review')"),
    )
    op.create_table(
        "strategy_proposal_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reviewed_proposal_sha256", sa.String(length=64), nullable=True),
        sa.Column("reviewed_evaluation_digest", sa.String(length=64), nullable=True),
        sa.Column("resulting_version_id", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')",
            name=op.f("ck_strategy_proposal_decisions_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["strategy_proposals.id"],
            name=op.f("fk_strategy_proposal_decisions_proposal_id_strategy_proposals"),
        ),
        sa.ForeignKeyConstraint(
            ["resulting_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_strategy_proposal_decisions_resulting_version_id_strategy_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_proposal_decisions")),
        sa.UniqueConstraint("proposal_id", name=op.f("uq_strategy_proposal_decisions_proposal_id")),
    )
    op.create_table(
        "evaluation_observations",
        sa.Column("evaluation_id", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("post_metric_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=48), nullable=True),
        sa.Column("metrics_provider", sa.String(length=32), nullable=True),
        sa.Column("actual_age_seconds", sa.Integer(), nullable=True),
        sa.Column("hook_type", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("strategy_digest", sa.String(length=64), nullable=False),
        sa.Column("strategy_group", sa.String(length=160), nullable=False),
        sa.Column("length_chars", sa.Integer(), nullable=False),
        sa.Column("length_bucket", sa.String(length=32), nullable=False),
        sa.Column("hour_utc", sa.Integer(), nullable=True),
        sa.Column("hour_block_utc", sa.String(length=8), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("replies", sa.Integer(), nullable=True),
        sa.Column("reposts", sa.Integer(), nullable=True),
        sa.Column("quotes", sa.Integer(), nullable=True),
        sa.Column("bookmarks", sa.Integer(), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["performance_evaluations.id"],
            name=op.f("fk_evaluation_observations_evaluation_id_performance_evaluations"),
        ),
        sa.ForeignKeyConstraint(
            ["post_metric_id"],
            ["post_metrics.id"],
            name=op.f("fk_evaluation_observations_post_metric_id_post_metrics"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_evaluation_observations_publication_id_publications"),
        ),
        sa.PrimaryKeyConstraint(
            "evaluation_id", "publication_id", name=op.f("pk_evaluation_observations")
        ),
    )
    op.add_column("runs", sa.Column("strategy_version_ref", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_strategy_version_ref_strategy_versions"),
        "runs",
        "strategy_versions",
        ["strategy_version_ref"],
        ["id"],
    )
    op.execute(
        """
        CREATE FUNCTION sga_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION sga_reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS sga_reject_mutation()")
    op.drop_constraint(
        op.f("fk_runs_strategy_version_ref_strategy_versions"), "runs", type_="foreignkey"
    )
    op.drop_column("runs", "strategy_version_ref")
    op.drop_table("evaluation_observations")
    op.drop_table("strategy_proposal_decisions")
    op.drop_index(
        "uq_strategy_proposals_open",
        table_name="strategy_proposals",
        postgresql_where=sa.text("status IN ('generating', 'pending_review')"),
    )
    op.drop_index(op.f("ix_strategy_proposals_evaluation_id"), table_name="strategy_proposals")
    op.drop_table("strategy_proposals")
    op.drop_table("strategy_activations")
    op.drop_index(op.f("ix_strategy_versions_account_id"), table_name="strategy_versions")
    op.drop_table("strategy_versions")
    op.drop_index(
        op.f("ix_performance_evaluations_account_id"), table_name="performance_evaluations"
    )
    op.drop_table("performance_evaluations")
