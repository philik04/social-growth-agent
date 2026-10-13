"""Phase 9 correction: synthetic (mock) and real discovery data share a database but
never each other's cache, items, conditional requests or rankings."""

from typing import Any

import psycopg
import pytest

from social_growth_agent.models import utc_now
from social_growth_agent.providers.discovery import LiveDiscoveryProviders
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.providers.mocks.discovery import (
    FixtureWeb,
    MockDiscoveryProviders,
    discovery_x_posts,
)
from tests.db.conftest import sql
from tests.db.discovery_helpers import Env


class RealishX(MockResearchProvider):
    """The fixture posts, presented as a real X provider would be."""

    def __init__(self) -> None:
        super().__init__(discovery_x_posts(utc_now()))
        self.source_name = "x"
        self.synthetic = False


@pytest.fixture
def env(make_runtime, client_for) -> Env:
    return Env(make_runtime, client_for)


def use_live(env: Env) -> FixtureWeb:
    """Switch the runtime to real provider classes (same fixtures, offline)."""
    web = FixtureWeb()
    assert env.runtime.discovery is not None
    env.runtime.discovery._providers = LiveDiscoveryProviders(
        RealishX(), timeout_seconds=5, transport=web.transport
    )
    return web


def use_mock(env: Env) -> FixtureWeb:
    web = FixtureWeb()
    assert env.runtime.discovery is not None
    env.runtime.discovery._providers = MockDiscoveryProviders(web=web)
    return web


def outcomes(session: dict[str, Any]) -> dict[str, str]:
    return {f["source_id"]: f["outcome"] for f in session["fetches"] if f["outcome"] != "skipped"}


def items_of(env: Env, session: dict[str, Any]) -> list[tuple[Any, ...]]:
    return sql(
        env.runtime.db.libpq_url,
        "SELECT DISTINCT i.id, i.synthetic FROM discovery_items i JOIN discovery_item_sightings s "
        "ON s.item_id = i.id WHERE s.session_id = :s",
        s=session["id"],
    )


def evidence_synthetic(env: Env, session: dict[str, Any]) -> set[Any]:
    rows = sql(
        env.runtime.db.libpq_url,
        "SELECT DISTINCT i.synthetic FROM discovery_opportunities o JOIN opportunity_evidence e "
        "ON e.opportunity_id = o.id JOIN discovery_items i ON i.id = e.item_id "
        "WHERE o.session_id = :s",
        s=session["id"],
    )
    return {r[0] for r in rows}


def test_a_live_session_never_reuses_mock_cache_items_or_rankings(env: Env):
    mock = env.session()
    assert mock["synthetic"] is True and items_of(env, mock)
    web = use_live(env)
    live = env.session()  # within the mock session's cache window

    assert live["synthetic"] is False
    got = outcomes(live)
    assert "cached" not in got.values() and "not_modified" not in got.values()
    assert got["openai-news"] == "succeeded"
    # the real providers made their own requests, with no validators from mock fetches
    assert web.requests and all("if-none-match" not in r.headers for r in web.requests)
    live_items = items_of(env, live)
    assert live_items and all(synthetic is False for _, synthetic in live_items)
    assert not {i for i, _ in live_items} & {i for i, _ in items_of(env, mock)}
    assert evidence_synthetic(env, live) == {False}
    for opp in env.opportunities(live["id"]):
        assert opp["synthetic"] is False

    # a second live session uses the live cache and dedupes onto the live rows
    again = env.session()
    assert set(outcomes(again).values()) == {"cached"}
    assert {i for i, _ in items_of(env, again)} == {i for i, _ in live_items}
    assert again["synthetic"] is False


def test_a_mock_session_never_reuses_live_cache_or_items(env: Env):
    use_live(env)
    live = env.session()
    assert live["synthetic"] is False
    web = use_mock(env)
    mock = env.session()

    assert mock["synthetic"] is True
    assert "cached" not in outcomes(mock).values()
    assert web.requests and all("if-none-match" not in r.headers for r in web.requests)
    assert all(synthetic is True for _, synthetic in items_of(env, mock))
    assert not {i for i, _ in items_of(env, mock)} & {i for i, _ in items_of(env, live)}
    assert evidence_synthetic(env, mock) == {True}
    # refreshing the live source afterwards sends the live validators, not the mock ones
    live_web = use_live(env)
    refreshed = env.session(refresh=True)
    assert outcomes(refreshed)["openai-news"] == "not_modified"
    assert all(s is False for _, s in items_of(env, refreshed))
    assert any("if-none-match" in r.headers for r in live_web.requests)


def test_mock_items_kept_from_before_the_fix_are_never_taken_over(env: Env):
    """Rows stored before mode-scoped keys (same key, synthetic) stay synthetic; a live
    fetch of the same URL gets its own row instead of reusing them."""
    env.session()
    url = env.runtime.db.libpq_url
    legacy = sql(url, "SELECT count(*) FROM discovery_items WHERE item_key LIKE 'synthetic|%'")
    assert legacy != [(0,)]
    # simulate a pre-fix database: the synthetic rows carry the unprefixed keys
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("ALTER TABLE discovery_items DISABLE TRIGGER trg_discovery_items_guard")
        conn.execute("UPDATE discovery_items SET item_key = substr(item_key, 11)")
        conn.execute("ALTER TABLE discovery_items ENABLE TRIGGER trg_discovery_items_guard")
    use_live(env)
    live = env.session()
    assert live["synthetic"] is False
    assert all(s is False for _, s in items_of(env, live))
    assert evidence_synthetic(env, live) == {False}
    keys = [str(k) for (k,) in sql(url, "SELECT item_key FROM discovery_items WHERE NOT synthetic")]
    assert keys and all(not k.startswith("synthetic|") for k in keys)
    assert any(k.startswith("live|") for k in keys)  # the legacy synthetic rows held the keys


def test_real_feeds_with_synthetic_x_posts_are_labelled_synthetic(env: Env):
    """DISCOVERY_SOURCES=live without RESEARCH_PROVIDER=x: real feeds, fixture X posts."""
    web = FixtureWeb()
    assert env.runtime.discovery is not None
    env.runtime.discovery._providers = LiveDiscoveryProviders(
        MockResearchProvider(discovery_x_posts(utc_now())),
        timeout_seconds=5,
        transport=web.transport,
    )
    mixed = env.session()
    assert mixed["synthetic"] is True
    flags = {s for _, s in items_of(env, mixed)}
    assert flags == {True, False}  # each item keeps its own label
    assert all(o["synthetic"] is True for o in env.opportunities(mixed["id"]))
