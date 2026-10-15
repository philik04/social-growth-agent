"""Explicit primary-source enrichment using existing audited discovery records.

Original evidence is never changed. A separate session, content-addressed item and
derived opportunity freeze a richer evidence version for subsequent replay. Purges
cover all versions because their platform/provider identity remains the same.
"""

import hashlib
import json
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from social_growth_agent.discovery.article import EXTRACTOR_VERSION
from social_growth_agent.discovery.registry import registry_digest
from social_growth_agent.discovery.text import canonical_url
from social_growth_agent.errors import (
    DiscoveryStateError,
    InvalidDiscoveryRequestError,
    SocialGrowthError,
)
from social_growth_agent.models import new_id, utc_now
from social_growth_agent.models.discovery import (
    DiscoveredItem,
    DiscoveryFetchOutcome,
    DiscoveryLimits,
    DiscoverySessionView,
    OpportunityView,
    SourceCategory,
    SourceKind,
    SourceSpec,
)
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.persistence import discovery as ddb
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence.tables import (
    DiscoveryFetchRow,
    DiscoveryOpportunityRow,
    DiscoverySessionRow,
    OpportunityEvidenceRow,
)
from social_growth_agent.providers.article import (
    ArticleReadError,
    PrimaryArticleProvider,
    validate_article_url,
)
from social_growth_agent.services.discovery import DiscoveryService


class EnrichmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=512)
    requested_by: str = Field(min_length=1, max_length=128)
    excerpts: list[Annotated[str, Field(min_length=40, max_length=500)]] | None = Field(
        default=None, min_length=2, max_length=12
    )
    refresh: bool = False


class EnrichmentResult(BaseModel):
    session: DiscoverySessionView
    opportunity: OpportunityView | None
    source_item_id: str
    already_enriched: bool


def enrich_primary_source(
    discovery: DiscoveryService,
    opportunity_id: str,
    body: EnrichmentBody,
    *,
    auth: ReviewAuthMethod,
    provider: PrimaryArticleProvider | None = None,
) -> EnrichmentResult:
    """One requested article only; no LLM/social provider, automatic retry or run creation."""
    db = discovery._db
    registry = discovery.registry()
    now = utc_now()
    limits = DiscoveryLimits(x_requests=0, user_reads=0, feeds=1, github_requests=0, hn_requests=0)
    key = hashlib.sha256(
        json.dumps(
            [opportunity_id, body.item_id, body.url, body.excerpts, EXTRACTOR_VERSION],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    # Lock the parent to serialize admission, then commit the started ledger before HTTP.
    with db.transaction() as session:
        parent = ddb.get_opportunity(session, opportunity_id, lock=True)
        usable = ddb.usable(session, ddb.evidence_rows(session, parent.id))
        source = next((r for r in usable if r.id == body.item_id), None)
        if source is None or source.category != "primary" or source.platform != "feed":
            raise InvalidDiscoveryRequestError(
                "select an unpurged primary feed article linked to this opportunity"
            )
        if source.synthetic or parent.synthetic:
            raise InvalidDiscoveryRequestError("synthetic evidence cannot be enriched as real")
        if parent.status == "dismissed" or parent.expires_at <= now:
            raise DiscoveryStateError("opportunity dismissed or expired")
        if canonical_url(body.url) != source.canonical_url:
            raise InvalidDiscoveryRequestError(
                "authorize the selected evidence item's canonical article URL"
            )
        validate_article_url(body.url, urlsplit(source.canonical_url or "").hostname or "")
        prior = session.scalars(
            select(DiscoverySessionRow)
            .where(
                DiscoverySessionRow.request["enrichment_key"].as_string() == key,
            )
            .order_by(DiscoverySessionRow.started_at.desc())
        ).first()
        if prior is not None and (not body.refresh or prior.status == "running"):
            opportunities = ddb.opportunities_of(session, prior.id)
            return EnrichmentResult(
                session=ddb.session_view(session, prior),
                opportunity=ddb.opportunity_view(session, opportunities[0])
                if opportunities
                else None,
                source_item_id=source.id,
                already_enriched=True,
            )
        source_id, publisher, provider_id = (
            source.source_id,
            source.publisher,
            source.provider_item_id,
        )
        category, tier, evidence_type = source.category, source.tier, source.evidence_type
        published_at = source.published_at
        session_id, fetch_id = new_id("disc"), new_id("dfetch")
        request = {
            "operation": "primary_source_enrichment",
            "enrichment_key": key,
            "parent_opportunity_id": parent.id,
            "source_item_id": source.id,
            "authorized_url": source.canonical_url,
            "extractor": EXTRACTOR_VERSION,
        }
        row = DiscoverySessionRow(
            id=session_id,
            account_id=parent.account_id,
            status="running",
            synthetic=False,
            request=request,
            limits=limits.model_dump(mode="json"),
            registry_version=registry.version,
            registry_sha256=registry_digest(registry),
            strategy_version_id=None,
            pricing_id=repo.save_price_list(session, discovery._prices),
            requested_by=body.requested_by,
            auth_method=auth.value,
            started_at=now,
        )
        session.add(row)
        session.flush()
        session.add(
            discovery._fetch_row(
                fetch_id,
                session_id,
                SourceSpec(
                    id=source_id,
                    kind=SourceKind.FEED,
                    category=SourceCategory.PRIMARY,
                    publisher=publisher,
                    tier=tier,
                    evidence_type=evidence_type,
                    url=body.url,
                ),
                "primary_article",
                now,
                DiscoveryFetchOutcome.STARTED,
                {"url": source.canonical_url},
            )
        )
    fetched = None
    failure = None
    try:
        fetched = (provider or PrimaryArticleProvider()).fetch(body.url, body.excerpts)
    except (SocialGrowthError, ValueError) as exc:
        failure = exc
    finished = utc_now()
    with db.transaction() as session:
        # Serialize with purge: a deletion during the network call must win over storage.
        ddb.lock_source_identities(session, "feed", [provider_id])
        parent = ddb.get_opportunity(session, opportunity_id, lock=True)
        rows = ddb.evidence_rows(session, opportunity_id)
        for original in rows:
            session.refresh(original, with_for_update=True)
        source = next((r for r in ddb.usable(session, rows) if r.id == body.item_id), None)
        ledger = session.get(DiscoveryFetchRow, fetch_id)
        row = ddb.get_session_row(session, session_id)
        assert ledger is not None
        row.finished_at = finished
        ledger.finished_at = finished
        if fetched is None or source is None:
            row.status = "failed"
            ledger.outcome = "failed"
            ledger.error_category = type(failure).__name__ if failure else "source_purged"
            if isinstance(failure, ArticleReadError):
                ledger.requests_made = failure.requests_made
                ledger.http_status = failure.http_status
            elif fetched is not None:
                ledger.requests_made = fetched.requests_made
                ledger.http_status = fetched.http_status
            ledger.error_message = (
                str(failure)
                if isinstance(failure, ArticleReadError)
                else "Article fetch/extraction failed; no evidence stored"
                if failure
                else "Source purged during enrichment"
            )
            row.summary = {
                "operation": "primary_source_enrichment",
                "opportunities": 0,
                "llm_calls": 0,
                "x_requests": 0,
            }
        else:
            article = fetched.evidence
            item = DiscoveredItem(
                source_id=source_id,
                platform="feed",
                kind=SourceKind.FEED,
                category=SourceCategory(category),
                tier=tier,
                evidence_type=evidence_type,
                publisher=publisher,
                provider_item_id=provider_id,
                url=source.canonical_url,
                title=article.title,
                text=article.evidence_text(publisher),
                author=article.author,
                published_at=article.published_at or published_at,
                retrieved_at=finished,
            )
            ids, skipped = ddb.upsert_items(
                session,
                session_id=session_id,
                fetch_id=fetch_id,
                items=[item],
                vocabulary=registry.vocabulary,
                synthetic=False,
                now=finished,
                versioned=True,
            )
            assert ids and not skipped
            opportunity = DiscoveryOpportunityRow(
                id=new_id("opp"),
                session_id=session_id,
                account_id=parent.account_id,
                fingerprint=hashlib.sha256(
                    f"{parent.fingerprint}:{ddb.content_digest(item)}".encode()
                ).hexdigest()[:16],
                rank=1,
                score=parent.score,
                keywords=list(parent.keywords),
                headline=parent.headline,
                components=list(parent.components),
                reasons=[
                    *parent.reasons,
                    "Explicitly enriched primary evidence; score inherited, not reranked",
                ],
                claim_hints=[],
                claim_summary={},
                suggested_intents=list(parent.suggested_intents),
                grok=dict(parent.grok),
                grok_eligible=False,
                audience_entry_ids=[],
                synthetic=False,
                status="open",
                expires_at=min(
                    parent.expires_at,
                    finished + timedelta(days=registry.policy.opportunity_ttl_days),
                ),
                created_at=finished,
            )
            # Enrichment must not manufacture new Grok eligibility or discussion.
            opportunity.grok = {**opportunity.grok, "eligible": False}
            session.add(opportunity)
            session.flush()
            session.add(
                OpportunityEvidenceRow(
                    opportunity_id=opportunity.id,
                    item_id=ids[0],
                    role="primary",
                    position=0,
                )
            )
            row.status = "succeeded"
            ledger.outcome = "succeeded"
            ledger.requests_made = fetched.requests_made
            ledger.http_status = fetched.http_status
            ledger.items_returned = 1
            row.summary = {
                "operation": "primary_source_enrichment",
                "parent_opportunity_id": parent.id,
                "source_item_id": source.id,
                "result_item_id": ids[0],
                "result_opportunity_id": opportunity.id,
                "final_url": fetched.final_url,
                "response_sha256": article.response_sha256,
                "content_sha256": ddb.content_digest(item),
                "excerpt_count": len(article.excerpts),
                "self_reported": True,
                "independently_verified": False,
                "opportunities": 1,
                "llm_calls": 0,
                "x_requests": 0,
            }
        session.flush()
    return EnrichmentResult(
        session=discovery.get_session(session_id),
        opportunity=discovery.opportunities(session_id)[0] if fetched and source else None,
        source_item_id=body.item_id,
        already_enriched=False,
    )
