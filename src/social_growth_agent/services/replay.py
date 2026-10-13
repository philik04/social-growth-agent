"""Evidence replay (Phase 9): a run started from a discovery opportunity researches the
opportunity's stored evidence instead of searching again.

``EvidenceReplayProvider`` wraps the configured research provider. A query that carries
``discovery_opportunity_id`` is answered from the database with zero external calls;
every other query goes to the wrapped provider unchanged, so Phase 1-8 runs behave
exactly as before and the LangGraph topology is untouched. Purged or tombstoned items
are never replayed.
"""

from social_growth_agent.models import (
    FetchOutcome,
    ResearchFetch,
    ResearchQuery,
    SearchResult,
    SourcePost,
)
from social_growth_agent.models.discovery import (
    EvidenceType,
    SourceCategory,
    VerificationStatus,
)
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import discovery as ddb
from social_growth_agent.persistence.tables import DiscoveryItemRow
from social_growth_agent.providers.social import SocialResearchProvider

REPLAY_SOURCE = "discovery_replay"


class EvidenceReplayProvider:
    def __init__(self, inner: SocialResearchProvider, db: Database) -> None:
        self._inner = inner
        self._db = db
        self.source_name = inner.source_name
        self.synthetic = inner.synthetic
        self.max_requests_per_run = inner.max_requests_per_run

    @property
    def inner(self) -> SocialResearchProvider:
        return self._inner

    def search(self, query: ResearchQuery) -> SearchResult:
        if query.discovery_opportunity_id is None:
            return self._inner.search(query)
        with self._db.transaction() as session:
            rows = ddb.usable(session, ddb.evidence_rows(session, query.discovery_opportunity_id))
            posts = [_post(row, query.text) for row in rows]
            synthetic = any(row.synthetic for row in rows)
        fetch = ResearchFetch(
            provider=REPLAY_SOURCE,
            query=query.text,
            effective_query=f"opportunity:{query.discovery_opportunity_id}",
            max_results=len(posts),
            requests_made=0,
            posts_fetched=0,  # nothing read from a platform: no billable reads
            users_fetched=0,
            latency_ms=0.0,
            outcome=FetchOutcome.SUCCESS if posts else FetchOutcome.EMPTY,
        )
        return SearchResult(
            posts=posts, fetch=fetch, source_name=REPLAY_SOURCE, synthetic=synthetic
        )


def _post(row: DiscoveryItemRow, query: str) -> SourcePost:
    metrics = dict(row.metrics or {})
    return SourcePost(
        source_id=row.id,
        platform=ddb.item_view(row).platform,
        author_id=row.author_id or row.source_id,
        author_username=None if row.author is None else row.author[:64],
        text=row.text or "",
        created_at=row.published_at or row.first_retrieved_at,
        likes=metrics.get("likes"),
        reposts=metrics.get("reposts"),
        replies=metrics.get("replies"),
        quotes=metrics.get("quotes"),
        impressions=metrics.get("impressions"),
        query=query,
        retrieved_at=row.last_retrieved_at,
        url=row.canonical_url,
        title=row.title,
        publisher=row.publisher,
        source_category=SourceCategory(row.category),
        evidence_type=EvidenceType(row.evidence_type),
        verification_status=VerificationStatus(row.verification_status),
    )
