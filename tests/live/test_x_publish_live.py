"""Opt-in live publishing test. Never runs in the normal suite and posts at most once.

    RUN_LIVE_X_PUBLISH_TESTS=1 CONFIRM_LIVE_X_PUBLISH=YES PUBLISHER_PROVIDER=x \\
      DATABASE_URL=postgresql://... TEST_DATABASE_URL=postgresql://... \\
      uv run pytest -m live tests/live/test_x_publish_live.py

Both gates are required; credentials being present is never enough. The test creates
exactly one post and never deletes it.
"""

import os

import pytest
from pydantic import SecretStr

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import PublicationExistsError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import PublicationStatus
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import build_publisher_worker, build_runtime
from social_growth_agent.services.x_publish_demo import (
    _approved_candidate,
    gates_open,
    post_text,
)
from tests.db.conftest import create_scratch_database, drop_scratch_database

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_X_PUBLISH_TESTS") != "1"
        or os.environ.get("CONFIRM_LIVE_X_PUBLISH") != "YES",
        reason="set RUN_LIVE_X_PUBLISH_TESTS=1 and CONFIRM_LIVE_X_PUBLISH=YES to publish",
    ),
]


def test_the_gates_are_both_required(monkeypatch):
    monkeypatch.setenv("CONFIRM_LIVE_X_PUBLISH", "no")
    allowed, reason = gates_open()
    assert not allowed and "CONFIRM_LIVE_X_PUBLISH" in reason


def test_one_live_post_is_created_and_never_published_twice(base_database_url):
    settings = AppSettings()
    if settings.publisher_provider != "x":
        pytest.skip("set PUBLISHER_PROVIDER=x to publish for real")
    url = create_scratch_database(base_database_url, prefix="sga_live_publish")
    upgrade(url)
    db = Database.connect(url)
    try:
        runtime = build_runtime(
            AppSettings(database_url=SecretStr(url), publisher_provider="x"),
            deps=Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider()),
            executor=InlineExecutor(),
        )
        try:
            content = post_text()
            run_id, candidate_id = _approved_candidate(runtime, content)
            service = PublicationService(runtime.db)
            publication = service.request(run_id, candidate_id, requested_by="live-test")

            report = build_publisher_worker(settings, runtime.db).run_once()

            assert report.calls_made == 1  # exactly one platform call
            with runtime.db.transaction() as session:
                view = pubs.publication_view(
                    session, pubs.get_publication_row(session, publication.id)
                )
            assert view.status is not PublicationStatus.READY
            if view.status is PublicationStatus.PUBLISHED:
                assert view.provider_post_id and view.provider_post_id.isdigit()
                assert view.provider_post_url
            else:  # a real failure is reported, never retried behind our back
                assert view.failure_category and view.failure_message
            with pytest.raises(PublicationExistsError):
                service.request(run_id, candidate_id)
            assert build_publisher_worker(settings, runtime.db).run_once().calls_made == 0
        finally:
            runtime.close()
    finally:
        db.dispose()
        drop_scratch_database(base_database_url, url)
