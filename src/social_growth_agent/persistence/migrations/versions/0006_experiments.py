"""Experiments (Phase 8): pre-registered A/B experiments on hook type or post length.

- ``experiments``: the pre-registration (design and its digest) plus lifecycle status; at
  most one approved or running experiment per account (partial unique index). A trigger
  rejects DELETE and any UPDATE of the design columns.
- ``experiment_arms`` / ``experiment_assignments`` / ``experiment_events`` /
  ``experiment_results``: append-only (the ``sga_reject_mutation`` trigger from 0005).
  ``UNIQUE (run_id)`` and ``UNIQUE (experiment_id, block_no, position)`` make every
  assignment single and every randomization slot drawn once.
- ``strategy_versions.source_experiment_result_id``: nullable, unique; the ``origin``
  check gains ``'experiment'``. Existing versions stay NULL (no backfill).

Applies on top of a populated Phase 7 database (0005) without touching its rows.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-13 12:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
IMMUTABLE_TABLES = (
    "experiment_arms",
    "experiment_assignments",
    "experiment_events",
    "experiment_results",
)
OPEN_WHERE = "status IN ('approved', 'running')"
DESIGN_COLUMNS = (
    "id",
    "account_id",
    "strategy_id",
    "base_version_id",
    "factor",
    "synthetic",
    "design",
    "design_sha256",
    "snapshot_age_seconds",
    "created_by",
    "auth_method",
    "created_at",
)


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("base_version_id", sa.String(length=64), nullable=False),
        sa.Column("factor", sa.String(length=16), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("design", JSON, nullable=False),
        sa.Column("design_sha256", sa.String(length=64), nullable=False),
        sa.Column("snapshot_age_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("peeked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unblinded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'running', 'stopped', 'concluded', 'cancelled')",
            name=op.f("ck_experiments_status"),
        ),
        sa.CheckConstraint("factor IN ('hook_type', 'length')", name=op.f("ck_experiments_factor")),
        sa.ForeignKeyConstraint(
            ["base_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_experiments_base_version_id_strategy_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiments")),
    )
    op.create_index(op.f("ix_experiments_account_id"), "experiments", ["account_id"], unique=False)
    op.create_index(
        "uq_experiments_open",
        "experiments",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text(OPEN_WHERE),
    )
    op.create_table(
        "experiment_arms",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_control", sa.Boolean(), nullable=False),
        sa.Column("treatment", JSON, nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_experiment_arms_experiment_id_experiments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_arms")),
        sa.UniqueConstraint("experiment_id", "label", name="uq_experiment_arms_label"),
        sa.UniqueConstraint("experiment_id", "position", name="uq_experiment_arms_position"),
    )
    op.create_table(
        "experiment_assignments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("arm_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("block_no", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("assigned_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "block_no >= 0 AND position >= 0", name=op.f("ck_experiment_assignments_slot")
        ),
        sa.ForeignKeyConstraint(
            ["arm_id"],
            ["experiment_arms.id"],
            name=op.f("fk_experiment_assignments_arm_id_experiment_arms"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_experiment_assignments_experiment_id_experiments"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_experiment_assignments_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_assignments")),
        sa.UniqueConstraint("run_id", name=op.f("uq_experiment_assignments_run_id")),
        sa.UniqueConstraint(
            "experiment_id", "block_no", "position", name="uq_experiment_assignments_slot"
        ),
    )
    op.create_index(op.f("ix_experiment_assignments_arm_id"), "experiment_assignments", ["arm_id"])
    op.create_index(
        op.f("ix_experiment_assignments_experiment_id"),
        "experiment_assignments",
        ["experiment_id"],
    )
    op.create_table(
        "experiment_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("detail", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('created', 'approved', 'started', 'stopped', 'cancelled', 'unblinded', "
            "'concluded', 'adopted')",
            name=op.f("ck_experiment_events_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_experiment_events_experiment_id_experiments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_events")),
    )
    op.create_index(
        op.f("ix_experiment_events_experiment_id"), "experiment_events", ["experiment_id"]
    )
    op.create_table(
        "experiment_results",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=40), nullable=False),
        sa.Column("evidence_class", sa.String(length=16), nullable=False),
        sa.Column("peeked", sa.Boolean(), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analysis_version", sa.String(length=16), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("analysis", JSON, nullable=False),
        sa.Column("flags", JSON, nullable=False),
        sa.Column("concluded_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_experiment_results_experiment_id_experiments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_results")),
        sa.UniqueConstraint("experiment_id", name=op.f("uq_experiment_results_experiment_id")),
    )
    op.add_column(
        "strategy_versions",
        sa.Column("source_experiment_result_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_strategy_versions_source_experiment_result",
        "strategy_versions",
        "experiment_results",
        ["source_experiment_result_id"],
        ["id"],
    )
    op.create_unique_constraint(
        op.f("uq_strategy_versions_source_experiment_result_id"),
        "strategy_versions",
        ["source_experiment_result_id"],
    )
    op.drop_constraint(op.f("ck_strategy_versions_origin"), "strategy_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_strategy_versions_origin"),
        "strategy_versions",
        "origin IN ('registered', 'imported_from_run', 'proposal', 'experiment')",
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION sga_reject_mutation()"
        )
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in DESIGN_COLUMNS)
    op.execute(
        f"""
        CREATE FUNCTION sga_freeze_experiment_design() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'experiments cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {changed} THEN
                RAISE EXCEPTION 'an experiment design is fixed once created'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_experiments_design_frozen BEFORE UPDATE OR DELETE ON experiments "
        "FOR EACH ROW EXECUTE FUNCTION sga_freeze_experiment_design()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_experiments_design_frozen ON experiments")
    op.execute("DROP FUNCTION IF EXISTS sga_freeze_experiment_design()")
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.drop_constraint(op.f("ck_strategy_versions_origin"), "strategy_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_strategy_versions_origin"),
        "strategy_versions",
        "origin IN ('registered', 'imported_from_run', 'proposal')",
    )
    op.drop_constraint(
        op.f("uq_strategy_versions_source_experiment_result_id"),
        "strategy_versions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_strategy_versions_source_experiment_result", "strategy_versions", type_="foreignkey"
    )
    op.drop_column("strategy_versions", "source_experiment_result_id")
    op.drop_table("experiment_results")
    op.drop_index(op.f("ix_experiment_events_experiment_id"), table_name="experiment_events")
    op.drop_table("experiment_events")
    op.drop_index(
        op.f("ix_experiment_assignments_experiment_id"), table_name="experiment_assignments"
    )
    op.drop_index(op.f("ix_experiment_assignments_arm_id"), table_name="experiment_assignments")
    op.drop_table("experiment_assignments")
    op.drop_table("experiment_arms")
    op.drop_index(
        "uq_experiments_open", table_name="experiments", postgresql_where=sa.text(OPEN_WHERE)
    )
    op.drop_index(op.f("ix_experiments_account_id"), table_name="experiments")
    op.drop_table("experiments")
