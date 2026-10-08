"""PostgreSQL fixtures. Every test in this package is marked ``db``.

TEST_DATABASE_URL points at a server where the test user may create databases. A
throwaway ``sga_test_<hex>`` database is created and migrated once per session, tables
are truncated between tests, and the database is dropped at the end.

Without TEST_DATABASE_URL these tests are skipped locally with a reason. When ``CI`` is
set they fail instead: CI must never silently skip DB tests.
"""

import os
from collections.abc import Callable, Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, make_url, pool, text

from social_growth_agent.accounting import ModelPrice, PriceList
from social_growth_agent.accounting.pricing import XPrices
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.api.app import create_app
from social_growth_agent.config import AppSettings
from social_growth_agent.graph import AgentSettings, Dependencies
from social_growth_agent.persistence.checkpointer import CHECKPOINT_TABLES
from social_growth_agent.persistence.db import sqlalchemy_url
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.persistence.tables import APP_TABLES
from social_growth_agent.providers.mocks import MockPublisher, MockResearchProvider
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime
from tests.conftest import FAST_RETRY

# Clearly fake prices for tests only. Real prices live in pricing.toml.
TEST_PRICES = PriceList(
    version="test-1",
    as_of="2026-10-01",
    currency="USD",
    source="unit-test values, not real prices",
    unbilled_providers=["mock_fixtures", "fake"],
    x=XPrices(post_read=Decimal("0.005"), user_read=Decimal("0.010")),
    openai={
        "gpt-test": ModelPrice(
            input_per_million=Decimal("1.00"), output_per_million=Decimal("4.00")
        )
    },
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/db/" in item.nodeid:
            item.add_marker(pytest.mark.db)


def _admin_engine(url: str):
    return create_engine(sqlalchemy_url(url), isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)


def create_scratch_database(base_url: str, prefix: str = "sga_test") -> str:
    name = f"{prefix}_{uuid4().hex[:10]}"
    engine = _admin_engine(base_url)
    with engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    return (
        make_url(sqlalchemy_url(base_url)).set(database=name).render_as_string(hide_password=False)
    )


def drop_scratch_database(base_url: str, url: str) -> None:
    name = make_url(url).database
    engine = _admin_engine(base_url)
    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    engine.dispose()


@pytest.fixture(scope="session")
def base_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("TEST_DATABASE_URL must be set in CI: DB tests may not be skipped")
        pytest.skip("PostgreSQL tests need TEST_DATABASE_URL (see docs/DATABASE.md)")
    return url


@pytest.fixture(scope="session")
def database_url(base_database_url: str) -> Iterator[str]:
    url = create_scratch_database(base_database_url)
    try:
        upgrade(url)
        yield url
    finally:
        drop_scratch_database(base_database_url, url)


# Sentinel for leak tests. It is the real password of a throwaway login role (below), so
# the server must accept it on every auth method, trust (local) and scram (CI) alike.
DB_PASSWORD = "db-pass-SECRET-91c2"


@pytest.fixture
def db_url_with_secret_password(clean_db: str) -> Iterator[str]:
    """``clean_db`` reached through a login role whose password is ``DB_PASSWORD``.

    The role inherits the test user's privileges, so the app behaves exactly as with
    ``clean_db``; the working TEST_DATABASE_URL credentials are never changed. Request
    this fixture before ``make_runtime`` so the runtimes close before the role is dropped.
    """
    role = f"sga_secret_probe_{uuid4().hex[:10]}"
    engine = _admin_engine(clean_db)
    with engine.connect() as conn:
        owner = conn.execute(text("SELECT current_user")).scalar_one()
        conn.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{DB_PASSWORD}'"))
        conn.execute(text(f'GRANT "{owner}" TO "{role}"'))
    try:
        yield (
            make_url(sqlalchemy_url(clean_db))
            .set(username=role, password=DB_PASSWORD)
            .render_as_string(hide_password=False)
        )
    finally:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :r"),
                {"r": role},
            )
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))
        engine.dispose()


@pytest.fixture
def clean_db(database_url: str) -> str:
    keep = {"checkpoint_migrations"}
    tables = ", ".join(f'"{t}"' for t in (*APP_TABLES, *CHECKPOINT_TABLES) if t not in keep)
    engine = create_engine(sqlalchemy_url(database_url), poolclass=pool.NullPool)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    engine.dispose()
    return database_url


@pytest.fixture
def make_runtime(clean_db: str) -> Iterator[Callable[..., Runtime]]:
    created: list[Runtime] = []

    def make(
        *,
        llm=None,
        research=None,
        prices: PriceList = TEST_PRICES,
        agent_settings: AgentSettings | None = None,
        url: str | None = None,
        publisher=None,
        **overrides,
    ) -> Runtime:
        settings = AppSettings(
            database_url=SecretStr(url or clean_db),
            llm_provider="fake",
            research_provider="mock",
            **overrides,
        )
        deps = Dependencies(
            llm=llm or build_fake_llm(),
            research_provider=research or MockResearchProvider(),
            agent_settings=agent_settings or AgentSettings(),
        )
        runtime = build_runtime(
            settings,
            deps=deps,
            executor=InlineExecutor(),
            prices=prices,
            retry_policy=FAST_RETRY,
            publisher=publisher or MockPublisher(),
        )
        created.append(runtime)
        return runtime

    yield make
    for runtime in created:
        runtime.close()


@pytest.fixture
def client_for():
    clients: list[TestClient] = []

    def make(runtime: Runtime) -> TestClient:
        client = TestClient(create_app(runtime=runtime))
        client.__enter__()  # runs the lifespan (startup recovery)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def run_payload(account, strategy):
    def make(**config):
        return {
            "account": account.model_dump(mode="json"),
            "strategy": strategy.model_dump(mode="json"),
            "config": config,
        }

    return make


def sql(url: str, query: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:  # commits, so UPDATE/DELETE helpers take effect
            return [tuple(r) for r in conn.execute(text(query), params)]
    finally:
        engine.dispose()
