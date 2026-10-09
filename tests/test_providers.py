import pytest

from social_growth_agent.errors import PublishRejectedError
from social_growth_agent.models import PublishRequest, ResearchQuery
from social_growth_agent.providers.mocks import (
    MockAnalyticsProvider,
    MockPublisher,
    MockResearchProvider,
)


def test_research_provider_is_deterministic_and_filters_by_query_phrase():
    query = ResearchQuery(text='"llm evaluation"')
    first = MockResearchProvider().search(query)
    second = MockResearchProvider().search(query)
    assert first.posts == second.posts
    assert [p.source_id for p in first.posts] == ["src_002", "src_005", "src_006"]
    assert all(p.query == '"llm evaluation"' for p in first.posts)
    assert first.fetch.requests_made == 0  # synthetic: no platform requests
    assert first.fetch.posts_fetched == 3


def test_publisher_assigns_sequential_ids_and_enforces_length():
    publisher = MockPublisher()
    first = publisher.publish(PublishRequest(account_id="a", candidate_id="c", content="hello"))
    second = publisher.publish(PublishRequest(account_id="a", candidate_id="d", content="hi"))
    assert int(second.provider_post_id) == int(first.provider_post_id) + 1
    with pytest.raises(PublishRejectedError):
        publisher.publish(PublishRequest(account_id="a", candidate_id="c", content="x" * 281))


def test_analytics_mock_is_deterministic_and_reports_deleted_posts_as_misses():
    analytics = MockAnalyticsProvider(deleted=["gone"])
    first = analytics.fetch_post_metrics(["1001", "1002", "gone"])
    again = MockAnalyticsProvider(deleted=["gone"]).fetch_post_metrics(["1001", "1002", "gone"])
    assert first == again
    a, b = first.records
    assert a.impressions != b.impressions
    assert [m.provider_post_id for m in first.misses] == ["gone"]
    assert first.posts_returned == 2
    assert a.provider_created_at is None  # unknown unless the platform reports it
