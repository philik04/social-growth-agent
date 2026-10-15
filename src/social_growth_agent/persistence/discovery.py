"""Discovery persistence (Phase 9): sessions, the fetch ledger, items, opportunities,
audience entries, manual exports and manual publications.

Every function takes an open session; callers own the transaction. Item text is read
only for items that are not purged and not tombstoned, so nothing removed under the
retention or deletion rules can reach analysis, a run or an API response again.
"""

import hashlib
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import and_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from social_growth_agent.discovery.analysis import AnalysisItem, PriorOpportunity
from social_growth_agent.discovery.claims import claim_hints, item_verification
from social_growth_agent.discovery.text import canonical_url, matched_terms
from social_growth_agent.errors import DiscoveryNotFoundError
from social_growth_agent.models import ContentIntent, PublicationStatus, new_id
from social_growth_agent.models.discovery import (
    AudienceEntryView,
    AudienceKind,
    AudienceSignal,
    ClaimHint,
    ClaimHintKind,
    CredibilityTier,
    DiscoveredItem,
    DiscoveryFetchOutcome,
    DiscoveryFetchView,
    DiscoveryLimits,
    DiscoverySessionStatus,
    DiscoverySessionView,
    EvidenceRole,
    EvidenceType,
    GrokEligibility,
    ItemView,
    ManualExportView,
    ManualPublicationView,
    OpportunityEvidence,
    OpportunityStatus,
    OpportunityView,
    ScoreComponent,
    SourceCategory,
    SourceKind,
    SourcePlatform,
    TextMatch,
    VerificationStatus,
)
from social_growth_agent.persistence.tables import (
    AudienceEntryRow,
    ContentCandidateRow,
    ContentPurgeRow,
    DiscoveryFetchRow,
    DiscoveryItemRow,
    DiscoveryOpportunityRow,
    DiscoverySessionRow,
    DiscoverySightingRow,
    ManualExportRow,
    ManualPublicationRow,
    OpportunityEvidenceRow,
    PublicationRow,
    RunRow,
)

MAX_ITEM_KEY = 600
WEB_INTENT = "https://x.com/intent/post?text="
"""Opens X's own compose box pre-filled; the person still presses Post."""
EXPORT_INSTRUCTIONS = [
    "Post this text yourself, exactly as shown, from the account configured in "
    "X_ACCOUNT_USER_ID / X_ACCOUNT_USERNAME.",
    "Nothing posts it for you: mentions are never published automatically.",
    "Afterwards, register the post URL or id so its performance is measured like any other "
    "published post (POST .../manual-publication).",
]


# --- sessions and fetches -------------------------------------------------------------


def get_session_row(session: Session, session_id: str) -> DiscoverySessionRow:
    row = session.get(DiscoverySessionRow, session_id)
    if row is None:
        raise DiscoveryNotFoundError(f"discovery session not found: {session_id}")
    return row


def fetches_of(session: Session, session_id: str) -> list[DiscoveryFetchRow]:
    return list(
        session.scalars(
            select(DiscoveryFetchRow)
            .where(DiscoveryFetchRow.session_id == session_id)
            .order_by(DiscoveryFetchRow.started_at, DiscoveryFetchRow.id)
        )
    )


def latest_fetch(
    session: Session,
    source_id: str,
    outcomes: Sequence[DiscoveryFetchOutcome],
    *,
    provider: str,
) -> DiscoveryFetchRow | None:
    """The newest such fetch of a source **by this provider**. The provider name tells
    the implementations apart (``mock_feed`` vs ``feed``, ``mock_fixtures`` vs ``x``), so
    synthetic and real fetches never share a cache or conditional-request validators."""
    return session.scalars(
        select(DiscoveryFetchRow)
        .where(DiscoveryFetchRow.source_id == source_id)
        .where(DiscoveryFetchRow.provider == provider)
        .where(DiscoveryFetchRow.outcome.in_([o.value for o in outcomes]))
        .order_by(DiscoveryFetchRow.started_at.desc())
        .limit(1)
    ).first()


def items_seen_in_fetch(session: Session, fetch_id: str) -> list[str]:
    return list(
        session.scalars(
            select(DiscoverySightingRow.item_id)
            .where(DiscoverySightingRow.fetch_id == fetch_id)
            .order_by(DiscoverySightingRow.position)
        )
    )


def last_items_fetch(
    session: Session, source_id: str, *, provider: str
) -> DiscoveryFetchRow | None:
    """The newest fetch of a source by this provider that returned items (cache, 304s)."""
    return latest_fetch(session, source_id, (DiscoveryFetchOutcome.SUCCEEDED,), provider=provider)


def session_view(session: Session, row: DiscoverySessionRow) -> DiscoverySessionView:
    return DiscoverySessionView(
        id=row.id,
        account_id=row.account_id,
        status=DiscoverySessionStatus(row.status),
        synthetic=row.synthetic,
        request=dict(row.request),
        limits=DiscoveryLimits.model_validate(row.limits),
        registry_version=row.registry_version,
        registry_sha256=row.registry_sha256,
        strategy_version_id=row.strategy_version_id,
        pricing_id=row.pricing_id,
        requested_by=row.requested_by,
        auth_method=row.auth_method,
        summary=None if row.summary is None else dict(row.summary),
        error_message=row.error_message,
        started_at=row.started_at,
        finished_at=row.finished_at,
        fetches=[fetch_view(f) for f in fetches_of(session, row.id)],
    )


def fetch_view(row: DiscoveryFetchRow) -> DiscoveryFetchView:
    return DiscoveryFetchView(
        id=row.id,
        source_id=row.source_id,
        kind=SourceKind(row.kind),
        provider=row.provider,
        outcome=DiscoveryFetchOutcome(row.outcome),
        request=dict(row.request),
        requests_made=row.requests_made,
        items_returned=row.items_returned,
        post_reads=row.post_reads,
        user_reads=row.user_reads,
        http_status=row.http_status,
        error_category=row.error_category,
        error_message=row.error_message,
        rate_limit_reset_at=row.rate_limit_reset_at,
        latency_ms=row.latency_ms,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def list_sessions(
    session: Session, *, account_id: str | None = None, limit: int = 50
) -> list[DiscoverySessionRow]:
    query = select(DiscoverySessionRow).order_by(DiscoverySessionRow.started_at.desc())
    if account_id is not None:
        query = query.where(DiscoverySessionRow.account_id == account_id)
    return list(session.scalars(query.limit(limit)))


# --- items ----------------------------------------------------------------------------


SYNTHETIC_KEY = "synthetic|"
"""Prefix of synthetic items' keys: a fixture and a real item never share a row."""
LIVE_KEY = "live|"
"""Prefix used only when an unprefixed key already belongs to a synthetic row stored
before keys were scoped by mode (such rows keep their identity and stay synthetic)."""


def item_key(item: DiscoveredItem, *, synthetic: bool) -> str:
    """Identity across sessions: the platform's own id, or for feeds the canonical URL
    (the same article in two feeds is one item), within one mode: synthetic items are
    keyed apart from real ones."""
    if item.platform == "feed":
        key = f"feed:{canonical_url(item.url) or item.provider_item_id}"
    else:
        key = f"{item.platform}:{item.provider_item_id}"
    if len(key) > MAX_ITEM_KEY - len(SYNTHETIC_KEY):
        key = f"{item.platform}:sha256:{hashlib.sha256(key.encode()).hexdigest()}"
    return SYNTHETIC_KEY + key if synthetic else key


def _real_key(session: Session, key: str) -> str:
    """The key for a real item: the plain key, unless a legacy synthetic row holds it."""
    holder = session.scalars(
        select(DiscoveryItemRow.synthetic).where(DiscoveryItemRow.item_key == key)
    ).first()
    return LIVE_KEY + key if holder is True else key


def content_digest(item: DiscoveredItem) -> str:
    basis = f"{item.title or ''}\n{item.text}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def tombstoned(session: Session, platform: str, provider_item_ids: Iterable[str]) -> set[str]:
    ids = list(dict.fromkeys(provider_item_ids))
    if not ids:
        return set()
    return set(
        session.scalars(
            select(ContentPurgeRow.provider_item_id)
            .where(ContentPurgeRow.platform == platform)
            .where(ContentPurgeRow.provider_item_id.in_(ids))
        )
    )


def lock_source_identities(
    session: Session, platform: str, provider_item_ids: Iterable[str]
) -> None:
    """Serialize ingestion/enrichment with deletion, including versions not yet inserted."""
    for provider_id in sorted(set(provider_item_ids)):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": f"discovery:{platform}:{provider_id}"},
        )


def upsert_items(
    session: Session,
    *,
    session_id: str,
    fetch_id: str,
    items: Sequence[DiscoveredItem],
    vocabulary: Sequence[str],
    synthetic: bool,
    now: datetime,
    versioned: bool = False,
) -> tuple[list[str], int]:
    """Store new items, refresh metrics of known ones and record the sightings.

    Returns (item ids in provider order, items skipped because they are tombstoned).
    The first retrieved text is kept: an item's identity and content hash never change.
    """
    blocked: set[tuple[str, str]] = set()
    for platform in sorted({i.platform for i in items}):
        lock_source_identities(
            session, platform, (i.provider_item_id for i in items if i.platform == platform)
        )
        blocked |= {
            (platform, pid)
            for pid in tombstoned(
                session, platform, (i.provider_item_id for i in items if i.platform == platform)
            )
        }
    ids: list[str] = []
    skipped = 0
    for item in items:
        if (item.platform, item.provider_item_id) in blocked:
            skipped += 1
            continue
        metrics = _metrics(item)
        key = item_key(item, synthetic=synthetic)
        if versioned:
            # Explicit article enrichment creates a content-addressed version; normal
            # discovery still keeps its original identity and first retrieved text.
            key = f"enrichment:{hashlib.sha256(key.encode()).hexdigest()}:{content_digest(item)}"
        if not synthetic:
            key = _real_key(session, key)
        verification = item_verification(item.category, item.evidence_type, item.tier)
        stmt = (
            insert(DiscoveryItemRow)
            .values(
                id=new_id("item"),
                item_key=key,
                source_id=item.source_id,
                platform=item.platform,
                kind=item.kind.value,
                category=item.category.value,
                evidence_type=item.evidence_type.value,
                tier=item.tier.value,
                verification_status=verification.value,
                publisher=item.publisher,
                provider_item_id=item.provider_item_id,
                canonical_url=canonical_url(item.url),
                title=item.title,
                text=item.text,
                author=item.author,
                author_id=item.author_id,
                published_at=item.published_at,
                first_retrieved_at=item.retrieved_at,
                last_retrieved_at=item.retrieved_at,
                keywords=matched_terms(f"{item.title or ''}\n{item.text}", vocabulary),
                claim_hints=[h.value for h in claim_hints(item.title, item.text, item.category)],
                metrics=metrics,
                content_sha256=content_digest(item),
                evergreen=item.evergreen,
                synthetic=synthetic,
                first_session_id=session_id,
            )
            .on_conflict_do_update(
                index_elements=[DiscoveryItemRow.item_key],
                set_={"last_retrieved_at": item.retrieved_at, "metrics": metrics},
            )
            .returning(DiscoveryItemRow.id)
        )
        item_id = session.execute(stmt).scalar_one()
        if item_id not in ids:
            ids.append(item_id)
    add_sightings(session, session_id=session_id, fetch_id=fetch_id, item_ids=ids, now=now)
    return ids, skipped


def add_sightings(
    session: Session, *, session_id: str, fetch_id: str, item_ids: Sequence[str], now: datetime
) -> None:
    if not item_ids:
        return
    session.execute(
        insert(DiscoverySightingRow)
        .values(
            [
                {
                    "session_id": session_id,
                    "item_id": item_id,
                    "fetch_id": fetch_id,
                    "position": position,
                    "seen_at": now,
                }
                for position, item_id in enumerate(item_ids)
            ]
        )
        .on_conflict_do_nothing()
    )


def _metrics(item: DiscoveredItem) -> dict[str, int | None]:
    names = ("likes", "reposts", "replies", "quotes", "impressions", "points", "comments")
    return {n: getattr(item, n) for n in names if getattr(item, n) is not None}


def session_items(session: Session, session_id: str) -> list[DiscoveryItemRow]:
    """Unpurged items sighted in a session, in sighting order."""
    return list(
        session.scalars(
            select(DiscoveryItemRow)
            .join(DiscoverySightingRow, DiscoverySightingRow.item_id == DiscoveryItemRow.id)
            .where(DiscoverySightingRow.session_id == session_id)
            .where(DiscoveryItemRow.purged_at.is_(None))
            .order_by(DiscoverySightingRow.fetch_id, DiscoverySightingRow.position)
        )
    )


def items_by_id(session: Session, item_ids: Sequence[str]) -> list[DiscoveryItemRow]:
    if not item_ids:
        return []
    query = select(DiscoveryItemRow).where(DiscoveryItemRow.id.in_(list(item_ids)))
    rows = {r.id: r for r in session.scalars(query)}
    return [rows[i] for i in item_ids if i in rows]


def usable(
    session: Session, rows: Sequence[DiscoveryItemRow], *, synthetic: bool | None = None
) -> list[DiscoveryItemRow]:
    """Rows whose text may be used: not purged, and not tombstoned since. With
    ``synthetic`` given, only rows of that mode (cache reuse never crosses modes)."""
    live = [
        r
        for r in rows
        if r.purged_at is None
        and r.text is not None
        and (synthetic is None or r.synthetic == synthetic)
    ]
    blocked: set[tuple[str, str]] = set()
    for platform in {r.platform for r in live}:
        ids = [r.provider_item_id for r in live if r.platform == platform]
        blocked |= {(platform, pid) for pid in tombstoned(session, platform, ids)}
    return [r for r in live if (r.platform, r.provider_item_id) not in blocked]


def analysis_item(row: DiscoveryItemRow, *, curated_handles: frozenset[str]) -> AnalysisItem:
    metrics: dict[str, Any] = dict(row.metrics or {})
    return AnalysisItem(
        id=row.id,
        source_id=row.source_id,
        platform=_platform(row.platform),
        category=SourceCategory(row.category),
        evidence_type=EvidenceType(row.evidence_type),
        tier=CredibilityTier(row.tier),
        verification=VerificationStatus(row.verification_status),
        publisher=row.publisher,
        url=row.canonical_url,
        title=row.title,
        text=row.text or "",
        author=row.author,
        published_at=row.published_at,
        hints=tuple(ClaimHintKind(h) for h in row.claim_hints),
        provider_item_id=row.provider_item_id,
        evergreen=row.evergreen,
        curated=row.kind == SourceKind.X_ACCOUNTS.value
        or (row.author or "").lower() in curated_handles,
        likes=metrics.get("likes"),
        reposts=metrics.get("reposts"),
        replies=metrics.get("replies"),
        quotes=metrics.get("quotes"),
        points=metrics.get("points"),
        comments=metrics.get("comments"),
    )


_PLATFORMS: tuple[SourcePlatform, ...] = ("x", "feed", "hn", "github")


def _platform(value: str) -> SourcePlatform:
    for platform in _PLATFORMS:
        if platform == value:
            return platform
    raise ValueError(f"unknown platform in a stored row: {value!r}")


def item_view(row: DiscoveryItemRow) -> ItemView:
    return ItemView(
        id=row.id,
        source_id=row.source_id,
        platform=_platform(row.platform),
        category=SourceCategory(row.category),
        evidence_type=EvidenceType(row.evidence_type),
        tier=CredibilityTier(row.tier),
        verification_status=VerificationStatus(row.verification_status),
        publisher=row.publisher,
        provider_item_id=row.provider_item_id,
        url=row.canonical_url,
        title=row.title,
        text=row.text,
        author=row.author,
        published_at=row.published_at,
        first_retrieved_at=row.first_retrieved_at,
        last_retrieved_at=row.last_retrieved_at,
        keywords=list(row.keywords),
        metrics=dict(row.metrics or {}),
        claim_hints=[ClaimHintKind(h) for h in row.claim_hints],
        purged=row.purged_at is not None,
        purge_reason=row.purge_reason,
    )


# --- history used by ranking ------------------------------------------------------------


def prior_opportunities(
    session: Session, account_id: str, since: datetime, *, synthetic: bool
) -> list[PriorOpportunity]:
    """Earlier opportunities of the same mode: synthetic topics never suppress or lower
    the novelty of real ones, and the other way round."""
    rows = session.scalars(
        select(DiscoveryOpportunityRow)
        .where(DiscoveryOpportunityRow.account_id == account_id)
        .where(DiscoveryOpportunityRow.created_at >= since)
        .where(DiscoveryOpportunityRow.synthetic == synthetic)
    )
    return [
        PriorOpportunity(
            fingerprint=r.fingerprint,
            keywords=frozenset(r.keywords),
            created_at=r.created_at,
            dismissed=r.status == OpportunityStatus.DISMISSED.value,
        )
        for r in rows
    ]


SYNTHETIC_PUBLISHERS = ("mock_publisher",)
"""Publication providers that post nowhere (``MockPublisher``)."""


def own_published_posts(
    session: Session, account_id: str, since: datetime, *, synthetic: bool
) -> list[str]:
    """Texts this account published (by the worker or registered manually) since. A real
    session ignores posts the mock publisher "published": they were never on X."""
    query = (
        select(PublicationRow.content)
        .join(RunRow, RunRow.id == PublicationRow.run_id)
        .where(RunRow.account["id"].as_string() == account_id)
        .where(PublicationRow.status == PublicationStatus.PUBLISHED.value)
        .where(PublicationRow.published_at >= since)
        .order_by(PublicationRow.published_at)
    )
    if not synthetic:
        query = query.where(PublicationRow.provider.not_in(SYNTHETIC_PUBLISHERS))
    return list(session.scalars(query))


def recent_grok_questions(session: Session, account_id: str, since: datetime) -> list[str]:
    """Grok-question drafts of this account since (drafted, not only published)."""
    return list(
        session.scalars(
            select(ContentCandidateRow.content)
            .join(RunRow, RunRow.id == ContentCandidateRow.run_id)
            .where(RunRow.account["id"].as_string() == account_id)
            .where(ContentCandidateRow.content_intent == ContentIntent.GROK_QUESTION.value)
            .where(ContentCandidateRow.created_at >= since)
            .order_by(ContentCandidateRow.created_at)
        )
    )


# --- opportunities and audience ---------------------------------------------------------


def get_opportunity(
    session: Session, opportunity_id: str, *, lock: bool = False
) -> DiscoveryOpportunityRow:
    query = select(DiscoveryOpportunityRow).where(DiscoveryOpportunityRow.id == opportunity_id)
    if lock:
        query = query.with_for_update()
    row = session.scalars(query).first()
    if row is None:
        raise DiscoveryNotFoundError(f"opportunity not found: {opportunity_id}")
    return row


def evidence_rows(session: Session, opportunity_id: str) -> list[DiscoveryItemRow]:
    """The opportunity's evidence items in order, purged ones included (as metadata)."""
    return list(
        session.scalars(
            select(DiscoveryItemRow)
            .join(OpportunityEvidenceRow, OpportunityEvidenceRow.item_id == DiscoveryItemRow.id)
            .where(OpportunityEvidenceRow.opportunity_id == opportunity_id)
            .order_by(OpportunityEvidenceRow.position)
        )
    )


def opportunities_of(session: Session, session_id: str) -> list[DiscoveryOpportunityRow]:
    return list(
        session.scalars(
            select(DiscoveryOpportunityRow)
            .where(DiscoveryOpportunityRow.session_id == session_id)
            .order_by(DiscoveryOpportunityRow.rank)
        )
    )


def runs_of_opportunity(session: Session, opportunity_id: str) -> list[str]:
    return list(
        session.scalars(
            select(RunRow.id)
            .where(RunRow.opportunity_id == opportunity_id)
            .order_by(RunRow.created_at)
        )
    )


def opportunity_view(session: Session, row: DiscoveryOpportunityRow) -> OpportunityView:
    rows = session.execute(
        select(OpportunityEvidenceRow.item_id, OpportunityEvidenceRow.role).where(
            OpportunityEvidenceRow.opportunity_id == row.id
        )
    )
    roles = {item_id: role for item_id, role in rows}
    evidence = [
        OpportunityEvidence(
            item_id=item.id,
            role=EvidenceRole(roles[item.id]),
            url=item.canonical_url,
            title=item.title,
            publisher=item.publisher,
            evidence_type=EvidenceType(item.evidence_type),
            verification_status=VerificationStatus(item.verification_status),
            published_at=item.published_at,
            purged=item.purged_at is not None,
        )
        for item in evidence_rows(session, row.id)
    ]
    return OpportunityView(
        id=row.id,
        session_id=row.session_id,
        account_id=row.account_id,
        rank=row.rank,
        score=row.score,
        fingerprint=row.fingerprint,
        keywords=list(row.keywords),
        headline=row.headline,
        components=[ScoreComponent.model_validate(c) for c in row.components],
        reasons=list(row.reasons),
        claim_hints=[ClaimHint.model_validate(h) for h in row.claim_hints],
        claim_summary={str(k): int(v) for k, v in row.claim_summary.items()},
        suggested_intents=[ContentIntent(i) for i in row.suggested_intents],
        grok=GrokEligibility.model_validate(row.grok),
        evidence=evidence,
        audience_entry_ids=list(row.audience_entry_ids),
        status=OpportunityStatus(row.status),
        expires_at=row.expires_at,
        run_ids=runs_of_opportunity(session, row.id),
        synthetic=row.synthetic,
    )


def audience_of(session: Session, session_id: str) -> list[AudienceEntryRow]:
    return list(
        session.scalars(
            select(AudienceEntryRow)
            .where(AudienceEntryRow.session_id == session_id)
            .order_by(AudienceEntryRow.position)
        )
    )


def audience_view(row: AudienceEntryRow) -> AudienceEntryView:
    return AudienceEntryView(
        id=row.id,
        session_id=row.session_id,
        kind=AudienceKind(row.kind),
        ref=row.ref,
        platform=_platform(row.platform),
        score=row.score,
        signals=[AudienceSignal.model_validate(s) for s in row.signals],
        matched_topics=list(row.matched_topics),
        explanation=row.explanation,
        evidence_item_ids=list(row.evidence_item_ids),
        curated=row.curated,
    )


def expiry(created_at: datetime, ttl_days: int) -> datetime:
    return created_at + timedelta(days=ttl_days)


# --- manual exports and publications ------------------------------------------------------


def latest_export(session: Session, run_id: str, candidate_id: str) -> ManualExportRow | None:
    return session.scalars(
        select(ManualExportRow)
        .where(ManualExportRow.run_id == run_id)
        .where(ManualExportRow.candidate_id == candidate_id)
        .order_by(ManualExportRow.created_at.desc())
        .limit(1)
    ).first()


def has_export(session: Session, run_id: str, candidate_id: str) -> bool:
    return latest_export(session, run_id, candidate_id) is not None


def export_view(row: ManualExportRow, content: str) -> ManualExportView:
    return ManualExportView(
        id=row.id,
        run_id=row.run_id,
        candidate_id=row.candidate_id,
        content=content,
        content_sha256=row.content_sha256,
        content_intent=None if row.content_intent is None else ContentIntent(row.content_intent),
        manual_only=row.manual_only,
        exported_by=row.exported_by,
        auth_method=row.auth_method,
        created_at=row.created_at,
        instructions=list(EXPORT_INSTRUCTIONS),
        intent_url=WEB_INTENT + quote(content, safe=""),
    )


def manual_publication_for_post(
    session: Session, platform: str, provider_post_id: str
) -> ManualPublicationRow | None:
    return session.scalars(
        select(ManualPublicationRow)
        .where(ManualPublicationRow.platform == platform)
        .where(ManualPublicationRow.provider_post_id == provider_post_id)
    ).first()


def publications_for_post(
    session: Session, platform: str, provider_post_id: str
) -> list[PublicationRow]:
    return list(
        session.scalars(
            select(PublicationRow)
            .where(PublicationRow.platform == platform)
            .where(PublicationRow.provider_post_id == provider_post_id)
        )
    )


def manual_publication_for_publication(
    session: Session, publication_id: str
) -> ManualPublicationRow | None:
    return session.scalars(
        select(ManualPublicationRow).where(ManualPublicationRow.publication_id == publication_id)
    ).first()


def manual_publication_view(
    row: ManualPublicationRow,
    *,
    provider_post_url: str,
    already_registered: bool = False,
    analytics_jobs: int = 0,
) -> ManualPublicationView:
    return ManualPublicationView(
        id=row.id,
        publication_id=row.publication_id,
        export_id=row.export_id,
        run_id=row.run_id,
        candidate_id=row.candidate_id,
        platform=row.platform,
        provider_post_id=row.provider_post_id,
        provider_post_url=provider_post_url,
        author_id=row.author_id,
        author_username=row.author_username,
        provider_created_at=row.provider_created_at,
        text_match=TextMatch(row.text_match),
        verification_provider=row.verification_provider,
        registered_by=row.registered_by,
        auth_method=row.auth_method,
        created_at=row.created_at,
        already_registered=already_registered,
        analytics_jobs=analytics_jobs,
    )


def item_ids_for(
    session: Session, platform: str, provider_item_ids: Sequence[str]
) -> list[DiscoveryItemRow]:
    if not provider_item_ids:
        return []
    return list(
        session.scalars(
            select(DiscoveryItemRow).where(
                and_(
                    DiscoveryItemRow.platform == platform,
                    DiscoveryItemRow.provider_item_id.in_(list(provider_item_ids)),
                )
            )
        )
    )


def expired_x_items(session: Session, before: datetime, limit: int) -> list[tuple[str, str]]:
    """(platform, provider_item_id) of X items first retrieved before ``before`` whose
    text is still stored."""
    rows = session.execute(
        select(DiscoveryItemRow.platform, DiscoveryItemRow.provider_item_id)
        .where(DiscoveryItemRow.platform == "x")
        .where(DiscoveryItemRow.purged_at.is_(None))
        .where(DiscoveryItemRow.first_retrieved_at < before)
        .order_by(DiscoveryItemRow.first_retrieved_at)
        .limit(limit)
    )
    return [(platform, provider_item_id) for platform, provider_item_id in rows]
