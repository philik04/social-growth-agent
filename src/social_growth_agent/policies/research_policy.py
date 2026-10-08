"""Deterministic research rules: the default query, the signal check, query broadening
and the limitations code always states.

Nothing here calls a provider or a model. Limitations do not depend on the model
noticing them; the Research Agent appends them to whatever the model reported.
"""

from pydantic import ValidationError

from social_growth_agent.models import (
    ContentStrategy,
    ResearchQuery,
    SignalDecision,
    SourcePost,
)

MIN_REPRESENTATIVE_SAMPLE = 20
DEFAULT_MIN_SIGNAL_POSTS = 5

SYNTHETIC_LIMITATION = (
    "Research material is synthetic sample data supplied by the application, not live X data."
)


def default_research_query(strategy: ContentStrategy) -> ResearchQuery:
    """Used when a run has no explicit query: the strategy's pillars as quoted phrases."""
    phrases = [f'"{p}"' if " " in p else p for p in strategy.pillars]
    return ResearchQuery(text=" OR ".join(phrases))


def _pillar_clause(strategy: ContentStrategy) -> str:
    return " OR ".join(f'"{p}"' if " " in p else p for p in strategy.pillars)


def broadened_queries(original: ResearchQuery, strategy: ContentStrategy) -> list[ResearchQuery]:
    """The original query followed by progressively broader variants, de-duplicated.

    1. drop the recency window and the author constraint;
    2. remove phrase quotes;
    3. OR the query with the strategy's pillars.
    The original is never modified; each step is a new ``ResearchQuery``.
    """
    base = original.model_dump()
    relaxed = {**base, "recency_hours": None, "author_username": None}
    unquoted_text = " ".join(original.text.replace('"', " ").split())
    candidates = [
        base,
        relaxed,
        {**relaxed, "text": unquoted_text},
        {**relaxed, "text": f"({unquoted_text}) OR {_pillar_clause(strategy)}"},
    ]
    steps: list[ResearchQuery] = []
    seen: set[tuple[str, int | None, str | None]] = set()
    for data in candidates:
        try:
            query = ResearchQuery.model_validate(data)
        except ValidationError:
            continue  # e.g. the widened text would exceed the length limit
        key = (query.text, query.recency_hours, query.author_username)
        if key not in seen:
            seen.add(key)
            steps.append(query)
    return steps


def assess_signal(
    *,
    posts: int,
    min_posts: int,
    can_fetch_again: bool,
    can_broaden: bool,
) -> SignalDecision | None:
    """Enough signal to interpret? ``None`` means no material at all and no way to get more.

    Thin but non-empty samples proceed once the budget is spent: the small-sample
    limitation is then stated deterministically by the Research Agent.
    """
    if posts >= min_posts:
        return SignalDecision.PROCEED
    if can_fetch_again and can_broaden:
        return SignalDecision.BROADEN
    return SignalDecision.PROCEED if posts > 0 else None


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
