"""Analytics (Phase 6): snapshot jobs, the request ledger, and metric snapshots.

- ``publications.provider_created_at``: the post's creation time as reported by the
  platform. NULL for every existing row; filled in by the first metrics read, never
  copied from ``published_at`` (which is when this application recorded the post).
- ``analytics_jobs``: one planned snapshot per (publication, age), UNIQUE, leased.
- ``analytics_requests``: started/finished ledger, one row per provider request.
- ``analytics_attempts``: each job's part in a request (the response-to-publication map).
- ``post_metrics``: immutable observations, UNIQUE per job and per (publication, age).

Applies on top of a populated Phase 5 database (0003) without touching its rows and
**creates no jobs**: older publications are scheduled only by an explicit backfill.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 22:22:08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "analytics_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("metrics_scope", sa.String(length=32), nullable=False),
        sa.Column("post_ids", JSON, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("posts_returned", sa.Integer(), nullable=True),
        sa.Column("rate_limit_remaining", sa.Integer(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_requests")),
    )
    op.create_index(
        op.f("ix_analytics_requests_started_at"), "analytics_requests", ["started_at"], unique=False
    )
    op.create_table(
        "analytics_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_age", sa.String(length=16), nullable=False),
        sa.Column("age_seconds", sa.Integer(), nullable=False),
        sa.Column("schedule_basis", sa.String(length=32), nullable=False),
        sa.Column("basis_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("attempt_base", sa.Integer(), nullable=False),
        sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_category", sa.String(length=32), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('scheduled', 'collecting', 'collected', 'failed', 'cancelled')",
            name=op.f("ck_analytics_jobs_status"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_analytics_jobs_attempt_count")),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_analytics_jobs_publication_id_publications"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_jobs")),
        sa.UniqueConstraint("publication_id", "snapshot_age", name="uq_analytics_jobs_target"),
    )
    op.create_index(
        "ix_analytics_jobs_due", "analytics_jobs", ["status", "scheduled_for"], unique=False
    )
    op.create_index(
        op.f("ix_analytics_jobs_publication_id"), "analytics_jobs", ["publication_id"], unique=False
    )
    op.create_table(
        "analytics_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("provider_post_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("failure_category", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["analytics_jobs.id"],
            name=op.f("fk_analytics_attempts_job_id_analytics_jobs"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_analytics_attempts_publication_id_publications"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["analytics_requests.id"],
            name=op.f("fk_analytics_attempts_request_id_analytics_requests"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_attempts")),
        sa.UniqueConstraint("job_id", "attempt", name="uq_analytics_attempts_attempt"),
    )
    op.create_index(
        op.f("ix_analytics_attempts_job_id"), "analytics_attempts", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_analytics_attempts_publication_id"),
        "analytics_attempts",
        ["publication_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analytics_attempts_request_id"), "analytics_attempts", ["request_id"], unique=False
    )
    op.create_table(
        "post_metrics",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("metrics_scope", sa.String(length=32), nullable=False),
        sa.Column("provider_post_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_age", sa.String(length=16), nullable=False),
        sa.Column("target_age_seconds", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("reposts", sa.Integer(), nullable=True),
        sa.Column("replies", sa.Integer(), nullable=True),
        sa.Column("quotes", sa.Integer(), nullable=True),
        sa.Column("bookmarks", sa.Integer(), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["analytics_jobs.id"], name=op.f("fk_post_metrics_job_id_analytics_jobs")
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_post_metrics_publication_id_publications"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["analytics_requests.id"],
            name=op.f("fk_post_metrics_request_id_analytics_requests"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post_metrics")),
        sa.UniqueConstraint("job_id", name=op.f("uq_post_metrics_job_id")),
        sa.UniqueConstraint("publication_id", "snapshot_age", name="uq_post_metrics_target"),
    )
    op.create_index(
        op.f("ix_post_metrics_publication_id"), "post_metrics", ["publication_id"], unique=False
    )
    op.add_column(
        "publications", sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("publications", "provider_created_at")
    op.drop_index(op.f("ix_post_metrics_publication_id"), table_name="post_metrics")
    op.drop_table("post_metrics")
    op.drop_index(op.f("ix_analytics_attempts_request_id"), table_name="analytics_attempts")
    op.drop_index(op.f("ix_analytics_attempts_publication_id"), table_name="analytics_attempts")
    op.drop_index(op.f("ix_analytics_attempts_job_id"), table_name="analytics_attempts")
    op.drop_table("analytics_attempts")
    op.drop_index(op.f("ix_analytics_jobs_publication_id"), table_name="analytics_jobs")
    op.drop_index("ix_analytics_jobs_due", table_name="analytics_jobs")
    op.drop_table("analytics_jobs")
    op.drop_index(op.f("ix_analytics_requests_started_at"), table_name="analytics_requests")
    op.drop_table("analytics_requests")
