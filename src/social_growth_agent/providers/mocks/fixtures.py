"""Static, deterministic fixture data for mock providers."""

from datetime import UTC, datetime

from social_growth_agent.models import SourcePost

_T0 = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)

FIXTURE_POSTS: tuple[SourcePost, ...] = (
    SourcePost(
        id="src_001",
        author_handle="infra_notes",
        text="We cut our LangGraph agent's p95 latency 40% by caching tool results. Here's how:",
        topic="agent engineering",
        posted_at=_T0,
        impressions=48_000,
        likes=1_150,
        replies=64,
        reposts=210,
    ),
    SourcePost(
        id="src_002",
        author_handle="mlops_daily",
        text="3 mistakes teams make when evaluating LLM agents (and what to measure instead)",
        topic="llm evaluation",
        posted_at=_T0,
        impressions=31_000,
        likes=720,
        replies=41,
        reposts=150,
    ),
    SourcePost(
        id="src_003",
        author_handle="builder_bea",
        text="Unpopular opinion: most 'multi-agent' demos are one prompt with extra steps.",
        topic="agent engineering",
        posted_at=_T0,
        impressions=62_000,
        likes=1_900,
        replies=230,
        reposts=180,
    ),
    SourcePost(
        id="src_004",
        author_handle="py_patterns",
        text="Typed state + small pure routing functions made our graph workflows testable.",
        topic="python",
        posted_at=_T0,
        impressions=12_000,
        likes=310,
        replies=12,
        reposts=40,
    ),
    SourcePost(
        id="src_005",
        author_handle="mlops_daily",
        text="How we built a regression suite for prompts: golden sets, rubrics, and CI gates.",
        topic="llm evaluation",
        posted_at=_T0,
        impressions=27_000,
        likes=640,
        replies=22,
        reposts=120,
    ),
)
