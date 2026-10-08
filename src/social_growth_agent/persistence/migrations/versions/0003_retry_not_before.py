"""Publishing: ``publications.retry_not_before`` gates automatic rate-limit retries.

A publication rejected with ``rate_limited`` and attempts left is requeued to
``ready`` with ``retry_not_before`` set to the platform's reset time; the worker's
claim query skips it until then. Existing rows get NULL (no gate).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 17:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "publications",
        sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("publications", "retry_not_before")
