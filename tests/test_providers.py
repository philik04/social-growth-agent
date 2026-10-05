import pytest

from social_growth_agent.errors import ProviderError
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
    assert [p.source_id for p in first.posts] == ["src_002", "src_005"]
    assert all(p.query == '"llm evaluation"' for p in first.posts)
    assert first.fetch.requests_made == 0  # synthetic: no platform requests
    assert first.fetch.posts_fetched == 2


def test_publisher_assigns_sequential_ids_and_enforces_length():
    publisher = MockPublisher()
    post = publisher.publish(PublishRequest(account_id="a", candidate_id="c", content="hello"))
    assert post.platform_post_id == "mock-x-0001"
    with pytest.raises(ProviderError):
        publisher.publish(PublishRequest(account_id="a", candidate_id="c", content="x" * 281))


def test_analytics_provider_is_stable_per_post_id():
    analytics = MockAnalyticsProvider()
    a1, b = analytics.fetch_metrics(["mock-x-0001", "mock-x-0002"])
    [a2] = analytics.fetch_metrics(["mock-x-0001"])
    assert a1.model_dump(exclude={"collected_at"}) == a2.model_dump(exclude={"collected_at"})
    assert a1.impressions != b.impressions
