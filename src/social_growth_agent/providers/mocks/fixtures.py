"""Static, deterministic fixture data for mock providers.

Fixture posts carry a placeholder ``query``; ``MockResearchProvider`` replaces it with
the query that matched them. Texts carry hashtags so phrase matching stays simple.
"""

from datetime import UTC, datetime

from social_growth_agent.models import SourcePost

_T0 = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
FIXTURE_RETRIEVED_AT = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


def _post(
    source_id: str,
    username: str,
    text: str,
    *,
    impressions: int,
    likes: int,
    replies: int,
    reposts: int,
    quotes: int,
) -> SourcePost:
    return SourcePost(
        source_id=source_id,
        author_id=f"mock_user_{username}",
        author_username=username,
        text=text,
        created_at=_T0,
        lang="en",
        impressions=impressions,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
        query="",
        retrieved_at=FIXTURE_RETRIEVED_AT,
    )


FIXTURE_POSTS: tuple[SourcePost, ...] = (
    _post(
        "src_001",
        "infra_notes",
        "We cut our LangGraph agent's p95 latency 40% by caching tool results. "
        "Here's how: #AgentEngineering",
        impressions=48_000,
        likes=1_150,
        replies=64,
        reposts=210,
        quotes=18,
    ),
    _post(
        "src_002",
        "mlops_daily",
        "3 mistakes teams make when evaluating LLM agents (and what to measure instead) "
        "#LLMEvaluation",
        impressions=31_000,
        likes=720,
        replies=41,
        reposts=150,
        quotes=9,
    ),
    _post(
        "src_003",
        "builder_bea",
        "Unpopular opinion: most 'multi-agent' demos are one prompt with extra steps. "
        "#AgentEngineering",
        impressions=62_000,
        likes=1_900,
        replies=230,
        reposts=180,
        quotes=44,
    ),
    _post(
        "src_004",
        "py_patterns",
        "Typed state + small pure routing functions made our graph workflows testable. #Python",
        impressions=12_000,
        likes=310,
        replies=12,
        reposts=40,
        quotes=2,
    ),
    _post(
        "src_005",
        "mlops_daily",
        "How we built a regression suite for prompts: golden sets, rubrics, and CI gates. "
        "#LLMEvaluation",
        impressions=27_000,
        likes=640,
        replies=22,
        reposts=120,
        quotes=6,
    ),
)
