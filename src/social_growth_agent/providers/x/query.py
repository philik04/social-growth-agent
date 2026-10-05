"""Translate a ``ResearchQuery`` into X recent-search parameters.

The original query text is never modified; the effective query is a separate value.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from social_growth_agent.models import ResearchQuery

X_MIN_RESULTS = 10
"""The recent-search endpoint rejects ``max_results`` below 10."""
X_MAX_QUERY_LENGTH = 4096

# Only what normalization needs; every extra field costs payload and may cost reads.
POST_FIELDS = "created_at,author_id,lang,public_metrics"
EXPANSIONS = "author_id"
USER_FIELDS = "username"
SORT_ORDER = "relevancy"


@dataclass(frozen=True)
class XSearchRequest:
    original_query: str
    effective_query: str
    max_results: int
    params: dict[str, str]


def effective_query(query: ResearchQuery) -> str:
    """Append operators to a copy of the query text.

    A query containing OR is parenthesized first: in X's syntax implicit AND binds
    tighter than OR, so ``a OR b -is:retweet`` would only filter the ``b`` branch.
    """
    text = query.text.strip()
    lowered = text.lower()
    base = f"({text})" if " or " in f" {lowered} " else text
    operators = []
    if query.language and "lang:" not in lowered:
        operators.append(f"lang:{query.language}")
    if query.author_username and "from:" not in lowered:
        operators.append(f"from:{query.author_username}")
    if query.exclude_reposts and "-is:retweet" not in lowered:
        operators.append("-is:retweet")
    return " ".join([base, *operators])


def build_search_request(
    query: ResearchQuery, *, max_results_cap: int, now: datetime
) -> XSearchRequest:
    requested = query.max_results or max_results_cap
    max_results = max(X_MIN_RESULTS, min(requested, max_results_cap))
    effective = effective_query(query)
    params = {
        "query": effective,
        "max_results": str(max_results),
        "tweet.fields": POST_FIELDS,
        "expansions": EXPANSIONS,
        "user.fields": USER_FIELDS,
        "sort_order": SORT_ORDER,
    }
    if query.recency_hours is not None:
        start = now.astimezone(UTC) - timedelta(hours=query.recency_hours)
        params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    return XSearchRequest(
        original_query=query.text,
        effective_query=effective,
        max_results=max_results,
        params=params,
    )
