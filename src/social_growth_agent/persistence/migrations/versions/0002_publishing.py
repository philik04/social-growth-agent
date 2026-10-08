"""Publishing (Phase 5): publication intents, attempts, and the provider-operation ledger.

- ``review_decisions`` gains ``reviewed_candidate_ids``: which review request a
  decision answered (empty for decisions recorded before Phase 5).
- ``content_candidates`` gains ``UNIQUE (run_id, id)`` so a publication can reference
  its candidate *and* prove the candidate belongs to the same run (composite FK).
- ``publications``: one durable intent per (run, candidate, platform), with a unique
  idempotency key, status CHECK, schedule and lease columns.
- ``publication_attempts``: started/finished ledger for every publish call.
- ``provider_operations``: started/finished ledger for provider-calling graph nodes.

Applies on top of an existing Phase 4 database (0001) without touching its rows.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 15:37:46.386975
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Provenance snapshot of the review request a decision answered. Existing rows get
    # an empty list: decisions made before Phase 5 carry no snapshot.
    op.add_column(
        "review_decisions",
        sa.Column("reviewed_candidate_ids", JSON, nullable=False, server_default=sa.text("'[]'")),
    )
    op.alter_column("review_decisions", "reviewed_candidate_ids", server_default=None)

    # Created first: the publications FK references it.
    op.create_unique_constraint(
        "uq_content_candidates_run_id_id", "content_candidates", ["run_id", "id"]
    )
    op.create_table(
        "provider_operations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("usage_records", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_provider_operations_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_operations")),
    )
    op.create_index(
        op.f("ix_provider_operations_run_id"), "provider_operations", ["run_id"], unique=False
    )
    op.create_table(
        "publications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_post_id", sa.String(length=64), nullable=True),
        sa.Column("provider_post_url", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_category", sa.String(length=32), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
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
            "status IN ('scheduled', 'ready', 'publishing', 'published', 'failed', "
            "'unknown', 'cancelled')",
            name=op.f("ck_publications_status"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_publications_attempt_count")),
        sa.ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            ["content_candidates.run_id", "content_candidates.id"],
            name="fk_publications_run_candidate",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_publications_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publications")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_publications_idempotency_key")),
        sa.UniqueConstraint("run_id", "candidate_id", "platform", name="uq_publications_target"),
    )
    op.create_index(
        "ix_publications_due", "publications", ["status", "scheduled_for"], unique=False
    )
    op.create_index(op.f("ix_publications_run_id"), "publications", ["run_id"], unique=False)
    op.create_table(
        "publication_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("failure_category", sa.String(length=32), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_post_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_publication_attempts_publication_id_publications"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_attempts")),
        sa.UniqueConstraint("publication_id", "attempt", name="uq_publication_attempts_attempt"),
    )
    op.create_index(
        op.f("ix_publication_attempts_publication_id"),
        "publication_attempts",
        ["publication_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_publication_attempts_publication_id"), table_name="publication_attempts")
    op.drop_table("publication_attempts")
    op.drop_index(op.f("ix_publications_run_id"), table_name="publications")
    op.drop_index("ix_publications_due", table_name="publications")
    op.drop_table("publications")
    op.drop_index(op.f("ix_provider_operations_run_id"), table_name="provider_operations")
    op.drop_table("provider_operations")
    op.drop_constraint("uq_content_candidates_run_id_id", "content_candidates", type_="unique")
    op.drop_column("review_decisions", "reviewed_candidate_ids")
