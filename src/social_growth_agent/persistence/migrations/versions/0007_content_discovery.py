"""Content discovery (Phase 9): sessions, evidence items, opportunities, audience, manual
exports and manual-publication registration, and content purges.

- ``discovery_sessions`` / ``discovery_fetches``: one bounded sweep and its started/finished
  call ledger.
- ``discovery_items``: one row per unique external item. A trigger forbids deleting rows,
  changing their identity, and restoring text once purged.
- ``discovery_item_sightings``, ``opportunity_evidence``, ``audience_entries``,
  ``manual_exports``, ``manual_publications``, ``content_purges``: append-only (the
  ``sga_reject_mutation`` trigger from 0005).
- ``discovery_opportunities``: may change only ``status`` and the dismissal fields.
- New nullable columns: ``runs.opportunity_id``, ``content_candidates.content_intent``,
  ``research_findings.claim_kind`` / ``verification``. Existing rows stay NULL.
- ``ix_publications_provider_post``: lookup of a publication by platform post id (not
  unique: mock posts from earlier phases may share ids). A manually registered post is
  recorded once through ``uq_manual_publications_post`` and a locked lookup.

Applies on top of a populated Phase 8 database (0006) without touching its rows.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
TS = sa.DateTime(timezone=True)
APPEND_ONLY = (
    "discovery_item_sightings",
    "opportunity_evidence",
    "audience_entries",
    "manual_exports",
    "manual_publications",
    "content_purges",
)
ITEM_IDENTITY = (
    "id",
    "item_key",
    "source_id",
    "platform",
    "kind",
    "category",
    "provider_item_id",
    "content_sha256",
    "first_retrieved_at",
    "first_session_id",
    "synthetic",
)
OPPORTUNITY_FIXED = (
    "id",
    "session_id",
    "account_id",
    "fingerprint",
    "rank",
    "score",
    "keywords",
    "headline",
    "components",
    "reasons",
    "claim_hints",
    "claim_summary",
    "suggested_intents",
    "grok",
    "grok_eligible",
    "audience_entry_ids",
    "synthetic",
    "expires_at",
    "created_at",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "discovery_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("request", JSON, nullable=False),
        sa.Column("limits", JSON, nullable=False),
        sa.Column("registry_version", sa.String(length=64), nullable=False),
        sa.Column("registry_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy_version_id", sa.String(length=64), nullable=True),
        sa.Column("pricing_id", sa.String(length=80), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("summary", JSON, nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(
            _in("status", ("running", "succeeded", "partial", "failed")),
            name=op.f("ck_discovery_sessions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["pricing_id"],
            ["pricing_versions.id"],
            name=op.f("fk_discovery_sessions_pricing_id_pricing_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
            name=op.f("fk_discovery_sessions_strategy_version_id_strategy_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_sessions")),
    )
    op.create_index(op.f("ix_discovery_sessions_account_id"), "discovery_sessions", ["account_id"])
    op.create_table(
        "discovery_fetches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("request", JSON, nullable=False),
        sa.Column("requests_made", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_returned", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("post_reads", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("user_reads", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("rate_limit_reset_at", TS, nullable=True),
        sa.Column("etag", sa.String(length=256), nullable=True),
        sa.Column("last_modified", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(
            _in(
                "outcome",
                ("started", "succeeded", "empty", "cached", "not_modified", "failed", "skipped"),
            ),
            name=op.f("ck_discovery_fetches_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["discovery_sessions.id"],
            name=op.f("fk_discovery_fetches_session_id_discovery_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_fetches")),
    )
    op.create_index(op.f("ix_discovery_fetches_session_id"), "discovery_fetches", ["session_id"])
    op.create_table(
        "discovery_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("item_key", sa.String(length=600), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("verification_status", sa.String(length=24), nullable=False),
        sa.Column("publisher", sa.String(length=80), nullable=False),
        sa.Column("provider_item_id", sa.String(length=512), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=128), nullable=True),
        sa.Column("author_id", sa.String(length=64), nullable=True),
        sa.Column("published_at", TS, nullable=True),
        sa.Column("first_retrieved_at", TS, nullable=False),
        sa.Column("last_retrieved_at", TS, nullable=False),
        sa.Column("keywords", JSON, nullable=False),
        sa.Column("claim_hints", JSON, nullable=False),
        sa.Column("metrics", JSON, nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("evergreen", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("first_session_id", sa.String(length=64), nullable=False),
        sa.Column("purged_at", TS, nullable=True),
        sa.Column("purge_reason", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["first_session_id"],
            ["discovery_sessions.id"],
            name=op.f("fk_discovery_items_first_session_id_discovery_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_items")),
        sa.UniqueConstraint("item_key", name=op.f("uq_discovery_items_item_key")),
    )
    op.create_index(op.f("ix_discovery_items_canonical_url"), "discovery_items", ["canonical_url"])
    op.create_table(
        "discovery_item_sightings",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("fetch_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("seen_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["fetch_id"],
            ["discovery_fetches.id"],
            name=op.f("fk_discovery_item_sightings_fetch_id_discovery_fetches"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["discovery_items.id"],
            name=op.f("fk_discovery_item_sightings_item_id_discovery_items"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["discovery_sessions.id"],
            name=op.f("fk_discovery_item_sightings_session_id_discovery_sessions"),
        ),
        sa.PrimaryKeyConstraint("session_id", "item_id", name=op.f("pk_discovery_item_sightings")),
    )
    op.create_table(
        "discovery_opportunities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("keywords", JSON, nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("components", JSON, nullable=False),
        sa.Column("reasons", JSON, nullable=False),
        sa.Column("claim_hints", JSON, nullable=False),
        sa.Column("claim_summary", JSON, nullable=False),
        sa.Column("suggested_intents", JSON, nullable=False),
        sa.Column("grok", JSON, nullable=False),
        sa.Column("grok_eligible", sa.Boolean(), nullable=False),
        sa.Column("audience_entry_ids", JSON, nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("dismissed_by", sa.String(length=128), nullable=True),
        sa.Column("dismissed_at", TS, nullable=True),
        sa.Column("dismiss_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            _in("status", ("open", "used", "dismissed")),
            name=op.f("ck_discovery_opportunities_status"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["discovery_sessions.id"],
            name=op.f("fk_discovery_opportunities_session_id_discovery_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_opportunities")),
    )
    for column in ("session_id", "account_id", "fingerprint"):
        op.create_index(
            op.f(f"ix_discovery_opportunities_{column}"), "discovery_opportunities", [column]
        )
    op.create_table(
        "opportunity_evidence",
        sa.Column("opportunity_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["discovery_items.id"],
            name=op.f("fk_opportunity_evidence_item_id_discovery_items"),
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["discovery_opportunities.id"],
            name=op.f("fk_opportunity_evidence_opportunity_id_discovery_opportunities"),
        ),
        sa.PrimaryKeyConstraint("opportunity_id", "item_id", name=op.f("pk_opportunity_evidence")),
    )
    op.create_table(
        "audience_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ref", sa.String(length=512), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signals", JSON, nullable=False),
        sa.Column("matched_topics", JSON, nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence_item_ids", JSON, nullable=False),
        sa.Column("curated", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["discovery_sessions.id"],
            name=op.f("fk_audience_entries_session_id_discovery_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audience_entries")),
    )
    op.create_index(op.f("ix_audience_entries_session_id"), "audience_entries", ["session_id"])
    op.create_table(
        "manual_exports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_intent", sa.String(length=32), nullable=True),
        sa.Column("manual_only", sa.Boolean(), nullable=False),
        sa.Column("exported_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_manual_exports_candidate_id_content_candidates"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_manual_exports_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_exports")),
    )
    op.create_index(op.f("ix_manual_exports_run_id"), "manual_exports", ["run_id"])
    op.create_table(
        "manual_publications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("export_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("provider_post_id", sa.String(length=64), nullable=False),
        sa.Column("author_id", sa.String(length=64), nullable=False),
        sa.Column("author_username", sa.String(length=64), nullable=True),
        sa.Column("provider_created_at", TS, nullable=True),
        sa.Column("text_match", sa.String(length=16), nullable=False),
        sa.Column("verification_provider", sa.String(length=32), nullable=False),
        sa.Column("verification_post_reads", sa.Integer(), nullable=False),
        sa.Column("verification_user_reads", sa.Integer(), nullable=False),
        sa.Column("registered_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["export_id"],
            ["manual_exports.id"],
            name=op.f("fk_manual_publications_export_id_manual_exports"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_manual_publications_publication_id_publications"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_manual_publications_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_publications")),
        sa.UniqueConstraint("publication_id", name=op.f("uq_manual_publications_publication_id")),
        sa.UniqueConstraint("platform", "provider_post_id", name="uq_manual_publications_post"),
    )
    op.create_index(op.f("ix_manual_publications_run_id"), "manual_publications", ["run_id"])
    op.create_table(
        "content_purges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("provider_item_id", sa.String(length=512), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("counts", JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_purges")),
        sa.UniqueConstraint("platform", "provider_item_id", name="uq_content_purges_item"),
    )

    op.add_column("runs", sa.Column("opportunity_id", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_opportunity_id_discovery_opportunities"),
        "runs",
        "discovery_opportunities",
        ["opportunity_id"],
        ["id"],
    )
    op.create_index(op.f("ix_runs_opportunity_id"), "runs", ["opportunity_id"])
    op.add_column(
        "content_candidates", sa.Column("content_intent", sa.String(length=32), nullable=True)
    )
    op.add_column("research_findings", sa.Column("claim_kind", sa.String(length=24), nullable=True))
    op.add_column(
        "research_findings", sa.Column("verification", sa.String(length=24), nullable=True)
    )
    op.create_index(
        "ix_publications_provider_post",
        "publications",
        ["platform", "provider_post_id"],
        postgresql_where=sa.text("provider_post_id IS NOT NULL"),
    )

    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION sga_reject_mutation()"
        )
    identity_changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in ITEM_IDENTITY)
    op.execute(
        f"""
        CREATE FUNCTION sga_guard_discovery_item() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'discovery items cannot be deleted (purge their text instead)'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {identity_changed} THEN
                RAISE EXCEPTION 'a discovery item''s identity is fixed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.purged_at IS NOT NULL AND (
                NEW.purged_at IS NULL OR NEW.text IS NOT NULL OR NEW.title IS NOT NULL
                OR NEW.author IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'purged content cannot be restored'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_discovery_items_guard BEFORE UPDATE OR DELETE ON discovery_items "
        "FOR EACH ROW EXECUTE FUNCTION sga_guard_discovery_item()"
    )
    fixed_changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in OPPORTUNITY_FIXED)
    op.execute(
        f"""
        CREATE FUNCTION sga_guard_opportunity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'opportunities cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {fixed_changed} THEN
                RAISE EXCEPTION 'an opportunity''s ranking and evidence are fixed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_discovery_opportunities_guard BEFORE UPDATE OR DELETE "
        "ON discovery_opportunities FOR EACH ROW EXECUTE FUNCTION sga_guard_opportunity()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_discovery_opportunities_guard ON discovery_opportunities"
    )
    op.execute("DROP FUNCTION IF EXISTS sga_guard_opportunity()")
    op.execute("DROP TRIGGER IF EXISTS trg_discovery_items_guard ON discovery_items")
    op.execute("DROP FUNCTION IF EXISTS sga_guard_discovery_item()")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.drop_index(
        "ix_publications_provider_post",
        table_name="publications",
        postgresql_where=sa.text("provider_post_id IS NOT NULL"),
    )
    op.drop_column("research_findings", "verification")
    op.drop_column("research_findings", "claim_kind")
    op.drop_column("content_candidates", "content_intent")
    op.drop_index(op.f("ix_runs_opportunity_id"), table_name="runs")
    op.drop_constraint(
        op.f("fk_runs_opportunity_id_discovery_opportunities"), "runs", type_="foreignkey"
    )
    op.drop_column("runs", "opportunity_id")
    op.drop_table("content_purges")
    op.drop_index(op.f("ix_manual_publications_run_id"), table_name="manual_publications")
    op.drop_table("manual_publications")
    op.drop_index(op.f("ix_manual_exports_run_id"), table_name="manual_exports")
    op.drop_table("manual_exports")
    op.drop_index(op.f("ix_audience_entries_session_id"), table_name="audience_entries")
    op.drop_table("audience_entries")
    op.drop_table("opportunity_evidence")
    for column in ("session_id", "account_id", "fingerprint"):
        op.drop_index(
            op.f(f"ix_discovery_opportunities_{column}"), table_name="discovery_opportunities"
        )
    op.drop_table("discovery_opportunities")
    op.drop_table("discovery_item_sightings")
    op.drop_index(op.f("ix_discovery_items_canonical_url"), table_name="discovery_items")
    op.drop_table("discovery_items")
    op.drop_index(op.f("ix_discovery_fetches_session_id"), table_name="discovery_fetches")
    op.drop_table("discovery_fetches")
    op.drop_index(op.f("ix_discovery_sessions_account_id"), table_name="discovery_sessions")
    op.drop_table("discovery_sessions")
