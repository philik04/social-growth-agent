"""Deterministic research rules: the default query and limitations code always states.

Limitations here do not depend on the model noticing them; the Research Agent appends
them to whatever the model reported.
"""

from social_growth_agent.models import ContentStrategy, ResearchQuery, SourcePost

MIN_REPRESENTATIVE_SAMPLE = 20

SYNTHETIC_LIMITATION = (
    "Research material is synthetic sample data supplied by the application, not live X data."
)


def default_research_query(strategy: ContentStrategy) -> ResearchQuery:
    """Used when a run has no explicit query: the strategy's pillars as quoted phrases."""
    phrases = [f'"{p}"' if " " in p else p for p in strategy.pillars]
    return ResearchQuery(text=" OR ".join(phrases))


def small_sample_limitation(size: int) -> str:
    return (
        f"Small sample: {size} posts (fewer than {MIN_REPRESENTATIVE_SAMPLE}); "
        "patterns may not generalize beyond these posts."
    )


def missing_impressions_limitation(missing: int, total: int) -> str:
    return (
        f"Impressions were unavailable for {missing} of {total} posts, so engagement "
        "cannot be compared relative to reach."
    )


def deterministic_limitations(
    posts: list[SourcePost],
    *,
    synthetic: bool,
    min_sample: int = MIN_REPRESENTATIVE_SAMPLE,
) -> list[str]:
    limitations = []
    if len(posts) < min_sample:
        limitations.append(small_sample_limitation(len(posts)))
    missing = sum(1 for p in posts if p.impressions is None)
    if missing:
        limitations.append(missing_impressions_limitation(missing, len(posts)))
    if synthetic:
        limitations.append(SYNTHETIC_LIMITATION)
    return limitations
