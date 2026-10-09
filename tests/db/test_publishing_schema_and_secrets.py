"""Publishing credentials stay out of every store and response; 0002 on a Phase 4 DB;
the generalized started/finished provider-operation ledger.
"""

import logging

import httpx
import pytest
from alembic import command
from pydantic import SecretStr
from sqlalchemy import make_url

from social_growth_agent.agents import ResearchReport
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.models import PublishFailureCategory
from social_growth_agent.persistence import Database
from social_growth_agent.persistence.checkpointer import CHECKPOINT_TABLES, setup_checkpoint_tables
from social_growth_agent.persistence.db import libpq_url, sqlalchemy_url
from social_growth_agent.persistence.migrate import alembic_config, current_revision, upgrade
from social_growth_agent.persistence.tables import APP_TABLES
from social_growth_agent.providers.mocks import MockPublisher
from social_growth_agent.providers.x import XPublisher
from social_growth_agent.providers.x.oauth1 import OAuth1Credentials
from social_growth_agent.services.demo import demo_account
from social_growth_agent.services.publications import PublicationService
from tests.db.conftest import (
    DB_PASSWORD,
    TEST_PRICES,
    create_scratch_database,
    drop_scratch_database,
    sql,
)
from tests.db.helpers import CrashOnce, SimulatedCrash
from tests.db.publishing_helpers import approved_run, worker
from tests.db.test_secrets_and_schema import dump_everything
from tests.x_fakes import FakeX, created_post

PUBLISH_SECRETS = {
    "x_publish_api_key": "pub-key-SECRET-1a2b",
    "x_publish_api_secret": "pub-api-secret-SECRET-3c4d",
    "x_publish_access_token": "pub-token-SECRET-5e6f",
    "x_publish_access_token_secret": "pub-token-secret-SECRET-7g8h",
}


def x_publisher(fake: FakeX) -> XPublisher:
    credentials = OAuth1Credentials(
        consumer_key=SecretStr(PUBLISH_SECRETS["x_publish_api_key"]),
        consumer_secret=SecretStr(PUBLISH_SECRETS["x_publish_api_secret"]),
        token=SecretStr(PUBLISH_SECRETS["x_publish_access_token"]),
        token_secret=SecretStr(PUBLISH_SECRETS["x_publish_access_token_secret"]),
    )
    return XPublisher(
        credentials,
        timeout_seconds=1.0,
        base_url="https://api.x.com",
        transport=fake.transport(),
    )


# --- 25. secrets -----------------------------------------------------------------------


def test_publishing_credentials_never_reach_the_database_api_output_or_logs(
    db_url_with_secret_password, make_runtime, client_for, run_payload, clean_db, caplog
):
    caplog.set_level(logging.DEBUG)
    fake = FakeX(created_post("1908222222222222222"))
    publisher = x_publisher(fake)
    runtime = make_runtime(publisher=publisher, url=db_url_with_secret_password)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    publication = client.post(
        f"/runs/{run_id}/publish", json={"candidate_id": candidate_id, "requested_by": "philipp"}
    ).json()

    report = worker(runtime.db, publisher).run_once()
    assert report.published == (publication["id"],)
    # The credentials really were used: the request carries a signed OAuth header.
    assert fake.requests[0].headers["authorization"].startswith("OAuth ")

    outputs = [
        client.get(path).text
        for path in (
            f"/runs/{run_id}",
            f"/runs/{run_id}/usage",
            f"/publications/{publication['id']}",
            "/publications",
            "/health",
        )
    ]
    haystacks = [
        dump_everything(clean_db),
        *outputs,
        caplog.text,
        repr(publisher),
        repr(runtime.db),
    ]
    for secret in (*PUBLISH_SECRETS.values(), DB_PASSWORD):
        for haystack in haystacks:
            assert secret not in haystack
    dumped = dump_everything(clean_db)
    assert "OAuth " not in dumped and "oauth_signature" not in dumped
    publisher.close()


def test_a_sanitized_failure_message_is_stored_without_the_provider_payload(
    make_runtime, client_for, run_payload
):
    body = {
        "title": "Forbidden",
        "detail": "You are not allowed to create a Tweet with duplicate content.",
        "internal_trace_id": "trace-SECRET-should-not-be-stored",
    }
    fake = FakeX(httpx.Response(403, json=body))
    publisher = x_publisher(fake)
    runtime = make_runtime(publisher=publisher)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    publication = client.post(f"/runs/{run_id}/publish", json={"candidate_id": candidate_id}).json()

    worker(runtime.db, publisher).run_once()

    stored = client.get(f"/publications/{publication['id']}").json()
    assert stored["status"] == "failed"
    assert stored["failure_category"] == PublishFailureCategory.DUPLICATE_CONTENT.value
    assert "duplicate content" in stored["failure_message"]
    assert "trace-SECRET-should-not-be-stored" not in stored["failure_message"]
    assert stored["attempts"][0]["failure_message"] is not None
    publisher.close()


def test_publishing_settings_are_secrets_and_never_printed():
    settings = AppSettings(_env_file=None, **{k: SecretStr(v) for k, v in PUBLISH_SECRETS.items()})
    printed = f"{settings!r} {settings.model_dump()} {settings.x_publish_api_key!r}"
    for secret in PUBLISH_SECRETS.values():
        assert secret not in printed


# --- 26 (schema). migrating a populated Phase 4 database -------------------------------


def test_0002_applies_to_a_populated_phase_4_database(base_database_url, run_payload, make_runtime):
    """0001 is never modified: an existing database upgrades in place and keeps its rows."""
    url = create_scratch_database(base_database_url, prefix="sga_phase4")
    try:
        command.upgrade(alembic_config(url), "0001")
        setup_checkpoint_tables(libpq_url(url))
        assert current_revision(url) == "0001"
        # A Phase 4 run, approved before publishing existed (no decision snapshot).
        sql(
            url,
            "INSERT INTO pricing_versions (id, version, as_of, currency, content) "
            "VALUES (:id, 'test-1', '2026-10-01', 'USD', '{}') RETURNING id",
            id=TEST_PRICES.id,
        )
        sql(
            url,
            "INSERT INTO runs (id, status, research_query, account, strategy, config, "
            "strategy_id, strategy_version, research_attempts, generation_attempts, "
            "regeneration_rounds, edit_rounds, review_candidate_ids, pricing_id) VALUES "
            "('run_legacy', 'approved', 'q', '{}', '{}', '{}', 's', 1, 1, 1, 0, 0, '[]', :p) "
            "RETURNING id",
            p=TEST_PRICES.id,
        )
        sql(
            url,
            "INSERT INTO content_candidates (id, run_id, generation_attempt, origin, content, "
            "topic, hook_type, format, target_audience, strategy_id, strategy_version, "
            "created_at, position) VALUES ('cand_legacy', 'run_legacy', 1, 'generated', "
            "'legacy post', 't', 'number', 'single', 'a', 's', 1, now(), 0) RETURNING id",
        )
        sql(
            url,
            "INSERT INTO critiques (id, run_id, candidate_id, generation_attempt, verdict, "
            "recommended_verdict, score, factual_risk, originality_risk, tone_match, issues, "
            "policy_violations, position) VALUES ('crit_legacy', 'run_legacy', 'cand_legacy', "
            "1, 'pass', 'pass', 0.9, 'low', 'low', 'strong', '[]', '[]', 0) RETURNING id",
        )
        sql(
            url,
            "INSERT INTO review_decisions (id, run_id, action, candidate_id, reviewer, "
            "decided_at) VALUES ('dec_legacy', 'run_legacy', 'approve', 'cand_legacy', "
            "'philipp', now()) RETURNING id",
        )

        upgrade(url)  # 0001 -> head (0002, 0003) on the populated database

        assert current_revision(url) == "0004"
        assert sql(url, "SELECT status FROM runs WHERE id = 'run_legacy'") == [("approved",)]
        assert sql(
            url, "SELECT reviewed_candidate_ids FROM review_decisions WHERE id = 'dec_legacy'"
        ) == [([],)]
        tables = {
            name
            for (name,) in sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert {"publications", "publication_attempts", "provider_operations"} <= tables
        assert set(APP_TABLES) | set(CHECKPOINT_TABLES) <= tables

        # The pre-Phase 5 approval carries no review snapshot, and still publishes.
        db = Database.connect(make_url(sqlalchemy_url(url)).render_as_string(hide_password=False))
        try:
            publication = PublicationService(db).request("run_legacy", "cand_legacy")
            assert publication.status == "ready"
            publisher = MockPublisher()
            assert worker(db, publisher).run_once().published == (publication.id,)
        finally:
            db.dispose()
    finally:
        drop_scratch_database(base_database_url, url)


# --- 23 (generalized). the provider-operation ledger -----------------------------------


def test_every_provider_calling_node_leaves_a_started_and_finished_operation(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, _ = approved_run(client, run_payload)

    rows = sql(
        runtime.db.libpq_url,
        "SELECT node, provider, operation, outcome, finished_at IS NOT NULL, usage_records "
        "FROM provider_operations WHERE run_id = :id ORDER BY started_at",
        id=run_id,
    )

    nodes = [node for node, *_ in rows]
    # The fake content agent needs a second attempt, so generate and critic ran twice.
    assert nodes == ["retrieve", "research", "generate", "critic", "generate", "critic"]
    assert all(outcome == "succeeded" and finished for *_, outcome, finished, _u in rows)
    assert rows[0][1] == "mock_fixtures" and rows[0][2] == "search"
    assert rows[1][1] == "fake:fake-deterministic" and rows[1][2] == "llm"
    assert all(usage >= 1 for *_, usage in rows)  # each recorded the usage it produced
    # Review nodes make no provider call, so they get no operation row.
    assert "human_review" not in nodes and "request_review" not in nodes


def test_an_operation_whose_process_dies_mid_call_stays_visible_as_started(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime(llm=CrashOnce(build_fake_llm(), crash_on=ResearchReport))
    account, strategy = demo_account()
    with pytest.raises(SimulatedCrash):
        runtime.service.create_run(account, strategy)

    rows = sql(
        runtime.db.libpq_url,
        "SELECT node, outcome, finished_at IS NOT NULL, usage_records FROM provider_operations "
        "ORDER BY started_at",
    )

    assert rows[0][:3] == ("retrieve", "succeeded", True)
    # The call that died: visible, unfinished, and with no usage or cost invented for it.
    assert rows[1] == ("research", "started", False, None)
