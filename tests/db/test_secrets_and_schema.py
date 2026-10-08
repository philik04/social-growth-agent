"""Credential isolation in the database, checkpoints and API output; schema management."""

import logging

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy import create_engine, pool

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.api.app import create_app
from social_growth_agent.config import AppSettings
from social_growth_agent.graph import Dependencies
from social_growth_agent.persistence import Database
from social_growth_agent.persistence.checkpointer import CHECKPOINT_TABLES
from social_growth_agent.persistence.db import sqlalchemy_url
from social_growth_agent.persistence.migrate import alembic_config, current_revision, upgrade
from social_growth_agent.persistence.tables import APP_TABLES, Base
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.runs import InlineExecutor, RunService
from social_growth_agent.services.runtime import Runtime
from social_growth_agent.services.workflow import WorkflowService
from tests.db.conftest import (
    DB_PASSWORD,
    TEST_PRICES,
    create_scratch_database,
    drop_scratch_database,
    sql,
)
from tests.x_fakes import RESET_EPOCH, TOKEN, FakeX, error, ok, sample_body


def dump_everything(url: str) -> str:
    """Every row of every app and checkpoint table as text (bytea blobs included)."""
    chunks = []
    for table in (*APP_TABLES, *CHECKPOINT_TABLES):
        for (row,) in sql(url, f'SELECT t::text FROM "{table}" t'):
            chunks.append(row)
    for (blob,) in sql(
        url, "SELECT encode(blob, 'escape') FROM checkpoint_blobs WHERE blob IS NOT NULL"
    ):
        chunks.append(blob)
    for (blob,) in sql(
        url, "SELECT encode(blob, 'escape') FROM checkpoint_writes WHERE blob IS NOT NULL"
    ):
        chunks.append(blob)
    return "\n".join(str(c) for c in chunks)


def test_secrets_never_reach_db_rows_checkpoints_or_api_output(
    db_url_with_secret_password, make_runtime, client_for, run_payload, clean_db, caplog
):
    caplog.set_level(logging.DEBUG)
    ok_x = FakeX(ok(sample_body(5)))
    runtime = make_runtime(research=ok_x.provider(), url=db_url_with_secret_password)
    client = client_for(runtime)
    query = {"research_query": {"text": "AI agents"}}
    good = client.post("/runs", json=run_payload(**query)).json()["id"]
    client.post(f"/runs/{good}/review", json={"action": "regenerate", "note": "n", "reviewer": "r"})

    limited = make_runtime(
        research=FakeX(error(429, **{"x-rate-limit-reset": str(RESET_EPOCH)})).provider(),
        url=db_url_with_secret_password,
    )
    limited_client = client_for(limited)
    failed = limited_client.post("/runs", json=run_payload(**query)).json()["id"]
    assert limited_client.get(f"/runs/{failed}").json()["run"]["status"] == "failed"
    assert ok_x.requests[0].headers["authorization"] == f"Bearer {TOKEN}"  # it was really used

    outputs = [
        client.get(path).text
        for path in (
            f"/runs/{good}",
            f"/runs/{good}/usage",
            f"/runs/{failed}",
            "/runs",
            "/reviews/pending",
            "/health",
        )
    ]
    haystacks = [dump_everything(clean_db), *outputs, caplog.text, repr(runtime.db)]
    for secret in (TOKEN, DB_PASSWORD):
        for haystack in haystacks:
            assert secret not in haystack
    assert "Bearer" not in dump_everything(clean_db)


def test_unreachable_database_returns_503_without_leaking_the_url(run_payload):
    url = f"postgresql://sga:{DB_PASSWORD}@127.0.0.1:1/nowhere"
    db = Database.connect(SecretStr(url))
    workflow = WorkflowService(
        Dependencies(llm=build_fake_llm(), research_provider=FakeX().provider()),
        checkpointer=InMemorySaver(),
    )
    service = RunService(workflow=workflow, db=db, prices=TEST_PRICES, executor=InlineExecutor())
    runtime = Runtime(
        service=service,
        publications=PublicationService(db),
        db=db,
        pool=_NoPool(),
        executor=InlineExecutor(),
    )
    with TestClient(create_app(runtime=_NoRecovery(runtime))) as client:
        for response in (client.get("/runs"), client.post("/runs", json=run_payload())):
            assert response.status_code == 503
            assert DB_PASSWORD not in response.text and "127.0.0.1" not in response.text
        health = client.get("/health").json()
        assert health["database"] == "unavailable"
    db.dispose()


class _NoPool:
    def close(self) -> None: ...


class _NoRecovery:
    """Runtime whose startup recovery is skipped (the database is down on purpose)."""

    def __init__(self, runtime: Runtime) -> None:
        self.db = runtime.db
        self.service = _Wrapped(runtime.service)
        self.publications = runtime.publications

    def start_embedded_worker(self) -> None: ...


class _Wrapped:
    def __init__(self, service: RunService) -> None:
        self._service = service

    def recover_stalled(self) -> list[str]:
        return []

    def __getattr__(self, name: str):
        return getattr(self._service, name)


def test_api_without_database_url_reports_not_configured_and_503(run_payload):
    settings = AppSettings(_env_file=None)  # ignore any local .env with a DATABASE_URL
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["database"] == "not_configured"
        response = client.post("/runs", json=run_payload())
        assert response.status_code == 503
        assert "DATABASE_URL" in response.json()["detail"]


# --- schema ---------------------------------------------------------------------------


def test_fresh_database_migrates_to_head_with_checkpoint_tables(base_database_url):
    url = create_scratch_database(base_database_url, prefix="sga_migrate")
    try:
        upgrade(url)
        upgrade(url)  # idempotent
        assert current_revision(url) == "0003"
        tables = {
            name
            for (name,) in sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert set(APP_TABLES) | set(CHECKPOINT_TABLES) | {"alembic_version"} <= tables

        engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = [d for d in compare_metadata(context, Base.metadata) if not _langgraph_owned(d)]
        engine.dispose()
        assert diff == []  # the migration matches the models exactly

        from alembic import command

        command.downgrade(alembic_config(url), "base")
        remaining = {
            n for (n,) in sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert not set(APP_TABLES) & remaining
        upgrade(url)
        assert current_revision(url) == "0003"
    finally:
        drop_scratch_database(base_database_url, url)


def _langgraph_owned(diff) -> bool:
    """Checkpoint tables and their indexes belong to LangGraph, not to our migrations."""
    obj = diff[1]
    table = obj if diff[0].endswith("_table") else getattr(obj, "table", None)
    return table is not None and table.name.startswith("checkpoint")


def test_run_rows_cascade_on_delete(make_runtime, run_payload, client_for):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id = client.post("/runs", json=run_payload()).json()["id"]
    sql(runtime.db.libpq_url, "DELETE FROM runs WHERE id = :id RETURNING id", id=run_id)
    for table in ("source_posts", "content_candidates", "critiques", "llm_calls", "run_events"):
        assert sql(runtime.db.libpq_url, f"SELECT count(*) FROM {table}") == [(0,)]
