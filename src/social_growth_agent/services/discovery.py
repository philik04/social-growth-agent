"""Content discovery service (Phase 9).

A *session* reads the configured sources once, within hard caps, stores what it found
with full provenance, and ranks content opportunities deterministically. Nothing here
posts, follows, likes, replies or messages anyone. Every external call is preceded by a
committed ``started`` row in the fetch ledger, so a crash never hides a request.

An opportunity becomes content only through a normal run (``start_run``): the run's
research replays the stored evidence (``EvidenceReplayProvider``), and its content
policy carries the intent, the grounding (links and numbers from the evidence) and the
recent Grok questions. Posts that mention anyone are manual-only: ``export`` hands an
approved candidate to a person, and ``register_manual_publication`` links the post they
published back to it after checking that it is the configured account's own post.
"""

import html
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from social_growth_agent.accounting import PriceList, UsageCounts, estimate_cost
from social_growth_agent.accounting.costs import ResearchUsage
from social_growth_agent.discovery.analysis import AnalysisContext, AnalysisResult, analyze
from social_growth_agent.discovery.registry import load_registry, registry_digest
from social_growth_agent.errors import (
    AnalyticsError,
    CandidateNotFoundError,
    DiscoveryStateError,
    InvalidDiscoveryRequestError,
    ManualPublicationError,
    ManualPublicationNotConfiguredError,
    NotPublishableError,
    ProviderError,
    RateLimitedError,
)
from social_growth_agent.graph.state import RunConfig
from social_growth_agent.models import (
    ContentIntent,
    ContentStrategy,
    PlatformPost,
    PublicationStatus,
    ResearchQuery,
    new_id,
    utc_now,
)
from social_growth_agent.models.discovery import (
    AudienceEntryView,
    DiscoveryFetchOutcome,
    DiscoveryLimits,
    DiscoveryPlan,
    DiscoveryRequest,
    DiscoverySessionStatus,
    DiscoverySessionView,
    ItemView,
    ManualExportView,
    ManualPublicationView,
    OpportunityStatus,
    OpportunityView,
    PlannedCall,
    PurgeReason,
    PurgeRequest,
    PurgeResult,
    Registry,
    SourceKind,
    SourceSpec,
    StartRunFromOpportunity,
    TextMatch,
    new_opportunity_id,
    new_session_id,
)
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.observability import get_logger
from social_growth_agent.persistence import Database, retention
from social_growth_agent.persistence import discovery as ddb
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence import strategies as sdb
from social_growth_agent.persistence.tables import (
    AudienceEntryRow,
    ContentCandidateRow,
    DiscoveryFetchRow,
    DiscoveryOpportunityRow,
    DiscoverySessionRow,
    ManualExportRow,
    ManualPublicationRow,
    OpportunityEvidenceRow,
    PublicationRow,
    RunRow,
)
from social_growth_agent.persistence.views import RunSummary
from social_growth_agent.policies import content_sha256, idempotency_key, publish_refusals
from social_growth_agent.policies.content_policy import ContentPolicy
from social_growth_agent.policies.grounding import grounding_from
from social_growth_agent.policies.mentions import MANUAL_ONLY_REASON, is_manual_only
from social_growth_agent.providers.discovery import Conditional, FetchResult, ProviderFactory
from social_growth_agent.providers.social import PostVerifier
from social_growth_agent.services.analytics import AnalyticsSchedule
from social_growth_agent.services.runs import RunService

_log = get_logger("discovery")

PLATFORM = "x"
X_KINDS = frozenset({SourceKind.X_SEARCH, SourceKind.X_ACCOUNTS})
EXPORT_CLOCK_TOLERANCE = timedelta(minutes=10)
_POST_URL = re.compile(
    r"^https://(?:www\.|mobile\.)?(?:x|twitter)\.com/(?:[A-Za-z0-9_]{1,15}|i/web|i)"
    r"/status(?:es)?/(\d{1,19})(?:[/?#].*)?$"
)
_POST_ID = re.compile(r"^\d{1,19}$")
_LINK = re.compile(r"https?://\S+")
_MAX_ERROR = 300


@dataclass(frozen=True)
class DiscoveryConfig:
    registry_path: Path = Path("discovery.toml")
    x_retention_days: int = 30
    account_user_id: str | None = None
    account_username: str | None = None


class DismissBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class ExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exported_by: str = Field(min_length=1, max_length=128)


class ManualPublicationBody(BaseModel):
    """``post``: the URL of the post you published (x.com or twitter.com), or its id."""

    model_config = ConfigDict(extra="forbid")

    post: str = Field(min_length=1, max_length=300)
    registered_by: str = Field(min_length=1, max_length=128)


class OpportunityRunCreated(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity: OpportunityView
    run: RunSummary


class DiscoveryService:
    def __init__(
        self,
        db: Database,
        *,
        runs: RunService,
        prices: PriceList,
        providers: ProviderFactory,
        config: DiscoveryConfig | None = None,
        verifier: Callable[[], PostVerifier] | None = None,
        analytics: AnalyticsSchedule | None = None,
        registry: Registry | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._db = db
        self._runs = runs
        self._prices = prices
        self._providers = providers
        self._config = config or DiscoveryConfig()
        self._verifier = verifier
        self._analytics = analytics
        self._registry = registry
        self._clock = clock

    # --- registry and plan -------------------------------------------------------------

    def registry(self) -> Registry:
        """Loaded on demand (and cached), so a broken registry never stops the API."""
        if self._registry is None:
            self._registry = load_registry(self._config.registry_path)
        return self._registry

    def plan(self, request: DiscoveryRequest) -> DiscoveryPlan:
        """What a session would call and its maximum X read cost. Calls nothing."""
        registry = self.registry()
        planned, skipped = self._plan_calls(registry, request)
        calls = [call for _, call in planned]
        post_reads = sum(c.max_post_reads for c in calls)
        user_reads = sum(c.max_user_reads for c in calls)
        x_provider = self._x_provider_name()
        estimate = estimate_cost(
            UsageCounts(
                research=[
                    ResearchUsage(
                        provider=x_provider,
                        fetches=sum(1 for c in calls if c.kind in X_KINDS),
                        requests=sum(c.calls for c in calls if c.kind in X_KINDS),
                        post_reads=post_reads,
                        user_reads=user_reads,
                    )
                ]
            ),
            self._prices,
            basis="current_pricing",
        )
        return DiscoveryPlan(
            account_id=request.account_id,
            calls=calls,
            skipped=[f"{spec.id}: {reason}" for spec, reason in skipped],
            max_x_post_reads=post_reads,
            max_x_user_reads=user_reads,
            estimated_max_cost=None if estimate.total is None else str(estimate.total),
            missing_prices=estimate.missing_prices,
            pricing_id=self._prices.id,
        )

    def _x_provider_name(self) -> str:
        provider = self._providers(SourceKind.X_SEARCH)
        return provider.provider_name if provider is not None else "x"

    def _plan_calls(
        self, registry: Registry, request: DiscoveryRequest
    ) -> tuple[list[tuple[SourceSpec, PlannedCall]], list[tuple[SourceSpec, str]]]:
        if request.sources is not None:
            unknown = [s for s in request.sources if registry.source(s) is None]
            if unknown:
                raise InvalidDiscoveryRequestError(f"unknown source ids: {', '.join(unknown)}")
            specs = [s for s in registry.sources if s.id in set(request.sources)]
        else:
            specs = [s for s in registry.sources if s.enabled]
        limits = request.limits
        budget = {
            "x": limits.x_requests,
            "user_reads": limits.user_reads,
            "feed": limits.feeds,
            "github": limits.github_requests,
            "hn": limits.hn_requests,
        }
        planned: list[tuple[SourceSpec, PlannedCall]] = []
        skipped: list[tuple[SourceSpec, str]] = []
        for spec in specs:
            if not spec.enabled:
                skipped.append((spec, "disabled in the registry"))
                continue
            if self._providers(spec.kind) is None:
                skipped.append((spec, "no provider configured for this kind"))
                continue
            if spec.kind is SourceKind.X_ACCOUNTS and not spec.handles:
                skipped.append((spec, "no curated handles configured"))
                continue
            if spec.kind in X_KINDS:
                posts = limits.posts_per_x_request
                if budget["x"] < 1:
                    skipped.append((spec, f"X request cap reached ({limits.x_requests})"))
                    continue
                if budget["user_reads"] < posts:
                    skipped.append((spec, f"user read cap reached ({limits.user_reads})"))
                    continue
                budget["x"] -= 1
                budget["user_reads"] -= posts
                note = "one X recent search, no pagination"
                planned.append(
                    (
                        spec,
                        PlannedCall(
                            source_id=spec.id,
                            kind=spec.kind,
                            calls=1,
                            max_post_reads=posts,
                            max_user_reads=posts,
                            note=note,
                        ),
                    )
                )
                continue
            key = {
                SourceKind.FEED: "feed",
                SourceKind.GITHUB_RELEASES: "github",
                SourceKind.HN_SEARCH: "hn",
            }[spec.kind]
            if budget[key] < 1:
                skipped.append((spec, f"{key} request cap reached"))
                continue
            budget[key] -= 1
            planned.append(
                (
                    spec,
                    PlannedCall(
                        source_id=spec.id,
                        kind=spec.kind,
                        calls=1,
                        max_post_reads=0,
                        max_user_reads=0,
                        note=f"one GET, at most {min(limits.items_per_feed, 20)} items",
                    ),
                )
            )
        return planned, skipped

    # --- sessions ------------------------------------------------------------------------

    def run_session(
        self, request: DiscoveryRequest, *, auth: ReviewAuthMethod
    ) -> DiscoverySessionView:
        """Run one bounded session now (synchronously) and return it."""
        registry = self.registry()
        planned, skipped = self._plan_calls(registry, request)
        now = self._clock()
        session_id = new_session_id()
        synthetic = any(
            getattr(self._providers(spec.kind), "synthetic", False) for spec, _ in planned
        )
        with self._db.transaction() as session:
            pricing_id = repo.save_price_list(session, self._prices)
            session.add(
                DiscoverySessionRow(
                    id=session_id,
                    account_id=request.account_id,
                    status=DiscoverySessionStatus.RUNNING.value,
                    synthetic=synthetic,
                    request=request.model_dump(mode="json"),
                    limits=request.limits.model_dump(mode="json"),
                    registry_version=registry.version,
                    registry_sha256=registry_digest(registry),
                    strategy_version_id=sdb.active_version_id(session, request.account_id),
                    pricing_id=pricing_id,
                    requested_by=request.requested_by,
                    auth_method=auth.value,
                    started_at=now,
                )
            )
        try:
            for spec, reason in skipped:
                self._record_skip(session_id, spec, reason)
            x_blocked: str | None = None
            for spec, _ in planned:
                if spec.kind in X_KINDS and x_blocked is not None:
                    self._record_skip(session_id, spec, x_blocked)
                    continue
                outcome = self._fetch_source(session_id, spec, request, registry)
                if outcome == "rate_limited" and spec.kind in X_KINDS:
                    x_blocked = "X rate limit reached earlier in this session"
            self._analyze(session_id, request, registry)
        except Exception as exc:
            with self._db.transaction() as session:
                session.execute(
                    update(DiscoverySessionRow)
                    .where(DiscoverySessionRow.id == session_id)
                    .values(
                        status=DiscoverySessionStatus.FAILED.value,
                        error_message=f"internal error ({type(exc).__name__})",
                        finished_at=self._clock(),
                    )
                )
            raise
        _log.info("discovery session finished", extra={"session_id": session_id})
        return self.get_session(session_id)

    def _record_skip(self, session_id: str, spec: SourceSpec, reason: str) -> None:
        now = self._clock()
        with self._db.transaction() as session:
            session.add(
                DiscoveryFetchRow(
                    id=new_id("dfetch"),
                    session_id=session_id,
                    source_id=spec.id,
                    kind=spec.kind.value,
                    provider=self._provider_name(spec),
                    outcome=DiscoveryFetchOutcome.SKIPPED.value,
                    request={},
                    requests_made=0,
                    items_returned=0,
                    post_reads=0,
                    user_reads=0,
                    error_message=reason,
                    started_at=now,
                    finished_at=now,
                )
            )

    def _provider_name(self, spec: SourceSpec) -> str:
        provider = self._providers(spec.kind)
        return provider.provider_name if provider is not None else "none"

    def _fetch_source(
        self, session_id: str, spec: SourceSpec, request: DiscoveryRequest, registry: Registry
    ) -> str:
        provider = self._providers(spec.kind)
        assert provider is not None  # planned sources have a provider
        limits = request.limits
        now = self._clock()
        fetch_id = new_id("dfetch")
        with self._db.transaction() as session:
            cached = (
                None
                if request.refresh
                else self._cached(session, spec, provider.provider_name, limits, now)
            )
            if cached is not None:
                item_ids = ddb.items_seen_in_fetch(session, cached.id)
                rows = ddb.items_by_id(session, item_ids)
                live = [r.id for r in ddb.usable(session, rows, synthetic=provider.synthetic)]
                session.add(
                    self._fetch_row(
                        fetch_id,
                        session_id,
                        spec,
                        provider.provider_name,
                        now,
                        DiscoveryFetchOutcome.CACHED,
                        {"from_fetch": cached.id},
                    )
                )
                session.flush()
                ddb.add_sightings(
                    session, session_id=session_id, fetch_id=fetch_id, item_ids=live, now=now
                )
                return "cached"
            previous = ddb.latest_fetch(
                session,
                spec.id,
                (DiscoveryFetchOutcome.SUCCEEDED, DiscoveryFetchOutcome.NOT_MODIFIED),
                provider=provider.provider_name,
            )
            conditional = (
                Conditional(etag=previous.etag, last_modified=previous.last_modified)
                if previous is not None
                else None
            )
            # Committed before the call: the ledger always shows what was attempted.
            session.add(
                self._fetch_row(
                    fetch_id,
                    session_id,
                    spec,
                    provider.provider_name,
                    now,
                    DiscoveryFetchOutcome.STARTED,
                    {},
                )
            )
        failure: ProviderError | None = None
        result: FetchResult | None = None
        try:
            result = provider.fetch(spec, limits, conditional=conditional)
        except ProviderError as exc:
            failure = exc
        finished = self._clock()
        with self._db.transaction() as session:
            row = session.get(DiscoveryFetchRow, fetch_id)
            assert row is not None
            row.finished_at = finished
            if failure is not None or result is None:
                row.outcome = DiscoveryFetchOutcome.FAILED.value
                row.error_category = (
                    failure.category.value if failure is not None and failure.category else None
                )
                row.error_message = " ".join(str(failure).split())[:_MAX_ERROR]
                if isinstance(failure, RateLimitedError):
                    row.rate_limit_reset_at = failure.reset_at
                    return "rate_limited"
                return "failed"
            row.request = dict(result.request)
            row.requests_made = result.requests_made
            row.post_reads = result.post_reads
            row.user_reads = result.user_reads
            row.http_status = result.http_status
            row.latency_ms = result.latency_ms
            etag = result.validators.etag
            row.etag = None if etag is None else etag[:256]
            row.last_modified = result.validators.last_modified
            if result.not_modified:
                row.outcome = DiscoveryFetchOutcome.NOT_MODIFIED.value
                last = ddb.last_items_fetch(session, spec.id, provider=provider.provider_name)
                if last is not None:
                    rows = ddb.items_by_id(session, ddb.items_seen_in_fetch(session, last.id))
                    live = [r.id for r in ddb.usable(session, rows, synthetic=provider.synthetic)]
                    session.flush()
                    ddb.add_sightings(
                        session,
                        session_id=session_id,
                        fetch_id=fetch_id,
                        item_ids=live,
                        now=finished,
                    )
                return "not_modified"
            session.flush()
            ids, blocked = ddb.upsert_items(
                session,
                session_id=session_id,
                fetch_id=fetch_id,
                items=result.items,
                vocabulary=registry.vocabulary,
                synthetic=provider.synthetic,
                now=finished,
            )
            row.items_returned = len(result.items)
            row.outcome = (
                DiscoveryFetchOutcome.SUCCEEDED.value if ids else DiscoveryFetchOutcome.EMPTY.value
            )
            notes = [result.note] if result.note else []
            if blocked:
                notes.append(f"{blocked} item(s) not stored: purged earlier (tombstone)")
            if notes:
                row.error_message = "; ".join(notes)[:_MAX_ERROR]
            return row.outcome

    @staticmethod
    def _fetch_row(
        fetch_id: str,
        session_id: str,
        spec: SourceSpec,
        provider: str,
        now: datetime,
        outcome: DiscoveryFetchOutcome,
        request: dict[str, str | int | None],
    ) -> DiscoveryFetchRow:
        done = outcome is not DiscoveryFetchOutcome.STARTED
        return DiscoveryFetchRow(
            id=fetch_id,
            session_id=session_id,
            source_id=spec.id,
            kind=spec.kind.value,
            provider=provider,
            outcome=outcome.value,
            request=request,
            requests_made=0,
            items_returned=0,
            post_reads=0,
            user_reads=0,
            started_at=now,
            finished_at=now if done else None,
        )

    @staticmethod
    def _cached(
        session: Session,
        spec: SourceSpec,
        provider: str,
        limits: DiscoveryLimits,
        now: datetime,
    ) -> DiscoveryFetchRow | None:
        """The same source fetched by the same provider within the cache window. A
        different provider (mock vs real) never counts, whatever the source id."""
        if limits.cache_hours == 0:
            return None
        last = ddb.last_items_fetch(session, spec.id, provider=provider)
        if last is None or last.finished_at is None:
            return None
        return last if now - last.finished_at < timedelta(hours=limits.cache_hours) else None

    def _analyze(self, session_id: str, request: DiscoveryRequest, registry: Registry) -> None:
        now = self._clock()
        policy = registry.policy
        with self._db.transaction() as session:
            row = ddb.get_session_row(session, session_id)
            strategy = self._strategy(session, request.account_id, None)
            items = ddb.usable(session, ddb.session_items(session, session_id))
            # The label follows the evidence: any synthetic item makes the session (and
            # its opportunities) synthetic. Scoped caches and keys keep real sessions
            # free of synthetic items; this is the backstop, never a silent upgrade.
            if any(i.synthetic for i in items) and not row.synthetic:
                _log.warning(
                    "synthetic items in a real discovery session; labelled synthetic",
                    extra={"session_id": session_id},
                )
                row.synthetic = True
            curated = frozenset(
                h.lower()
                for s in registry.sources
                if s.kind is SourceKind.X_ACCOUNTS
                for h in s.handles
            )
            history = max(
                policy.novelty_window_days,
                policy.fingerprint_suppression_days,
                policy.dismissed_suppression_days,
            )
            ctx = AnalysisContext(
                now=now,
                vocabulary=registry.vocabulary,
                generic_terms=frozenset(t.lower() for t in registry.generic_terms),
                pillars=list(strategy.pillars) if strategy else [],
                avoid_topics=list(strategy.avoid_topics) if strategy else [],
                weights=registry.weights,
                policy=policy,
                prior_opportunities=ddb.prior_opportunities(
                    session,
                    request.account_id,
                    now - timedelta(days=history),
                    synthetic=row.synthetic,
                ),
                own_posts=ddb.own_published_posts(
                    session,
                    request.account_id,
                    now - timedelta(days=policy.novelty_window_days),
                    synthetic=row.synthetic,
                ),
                prior_grok_questions=ddb.recent_grok_questions(
                    session, request.account_id, now - timedelta(days=policy.grok_dedup_days)
                ),
            )
            result = analyze([ddb.analysis_item(i, curated_handles=curated) for i in items], ctx)
            self._store_analysis(session, row, result, now, policy.opportunity_ttl_days)
            fetches = ddb.fetches_of(session, session_id)
            failed = [f for f in fetches if f.outcome == DiscoveryFetchOutcome.FAILED.value]
            if failed and len(failed) == len(
                [f for f in fetches if f.outcome != DiscoveryFetchOutcome.SKIPPED.value]
            ):
                status = DiscoverySessionStatus.FAILED
            elif failed:
                status = DiscoverySessionStatus.PARTIAL
            else:
                status = DiscoverySessionStatus.SUCCEEDED
            row.status = status.value
            row.finished_at = self._clock()
            row.summary = self._summary(fetches, items, result, strategy is not None)

    def _store_analysis(
        self,
        session: Session,
        row: DiscoverySessionRow,
        result: AnalysisResult,
        now: datetime,
        ttl_days: int,
    ) -> None:
        audience_ids: list[tuple[str, set[str]]] = []
        for position, entry in enumerate(result.audience):
            entry_id = new_id("aud")
            session.add(
                AudienceEntryRow(
                    id=entry_id,
                    session_id=row.id,
                    kind=entry.kind.value,
                    ref=entry.ref,
                    platform=entry.platform,
                    score=entry.score,
                    signals=[s.model_dump(mode="json") for s in entry.signals],
                    matched_topics=list(entry.matched_topics),
                    explanation=entry.explanation,
                    evidence_item_ids=list(entry.evidence_item_ids),
                    curated=entry.curated,
                    position=position,
                )
            )
            audience_ids.append((entry_id, set(entry.evidence_item_ids)))
        session.flush()
        for rank, opp in enumerate(result.opportunities, start=1):
            opportunity_id = new_opportunity_id()
            session.add(
                DiscoveryOpportunityRow(
                    id=opportunity_id,
                    session_id=row.id,
                    account_id=row.account_id,
                    fingerprint=opp.fingerprint,
                    rank=rank,
                    score=opp.score,
                    keywords=list(opp.keywords),
                    headline=opp.headline,
                    components=[c.model_dump(mode="json") for c in opp.components],
                    reasons=list(opp.reasons),
                    claim_hints=[h.model_dump(mode="json") for h in opp.hints],
                    claim_summary=dict(opp.claim_summary),
                    suggested_intents=[i.value for i in opp.intents],
                    grok=opp.grok.model_dump(mode="json"),
                    grok_eligible=opp.grok.eligible,
                    audience_entry_ids=[a for a, refs in audience_ids if refs & set(opp.item_ids)],
                    synthetic=row.synthetic,
                    status=OpportunityStatus.OPEN.value,
                    expires_at=ddb.expiry(now, ttl_days),
                    created_at=now,
                )
            )
            session.flush()
            for position, item_id in enumerate(opp.item_ids):
                session.add(
                    OpportunityEvidenceRow(
                        opportunity_id=opportunity_id,
                        item_id=item_id,
                        role=opp.roles[item_id].value,
                        position=position,
                    )
                )
        session.flush()

    def _summary(
        self,
        fetches: Sequence[DiscoveryFetchRow],
        items: Sequence[object],
        result: AnalysisResult,
        has_strategy: bool,
    ) -> dict[str, object]:
        x_fetches = [f for f in fetches if f.kind in {k.value for k in X_KINDS}]
        usage = UsageCounts(
            research=[
                ResearchUsage(
                    provider=provider,
                    fetches=sum(1 for f in x_fetches if f.provider == provider),
                    requests=sum(f.requests_made for f in x_fetches if f.provider == provider),
                    post_reads=sum(f.post_reads for f in x_fetches if f.provider == provider),
                    user_reads=sum(f.user_reads for f in x_fetches if f.provider == provider),
                )
                for provider in sorted({f.provider for f in x_fetches})
            ]
        )
        estimate = estimate_cost(usage, self._prices, basis="session_pricing")
        outcomes: dict[str, int] = {}
        for f in fetches:
            outcomes[f.outcome] = outcomes.get(f.outcome, 0) + 1
        notes = [
            "Scores are a deterministic heuristic; whether an idea is worth posting is "
            "decided at review.",
            "Cost covers discovery reads only, not later research, generation, publishing "
            "or analytics.",
        ]
        if not has_strategy:
            notes.append("No active strategy version: pillars and avoided topics not applied.")
        return {
            "fetch_outcomes": dict(sorted(outcomes.items())),
            "requests": {
                kind: sum(f.requests_made for f in fetches if f.kind == kind)
                for kind in sorted({f.kind for f in fetches})
            },
            "x_post_reads": sum(f.post_reads for f in x_fetches),
            "x_user_reads": sum(f.user_reads for f in x_fetches),
            "items": len(items),
            "clusters": result.clusters,
            "opportunities": len(result.opportunities),
            "grok_eligible": sum(1 for o in result.opportunities if o.grok.eligible),
            "suppressed": [{"headline": s.headline, "reason": s.reason} for s in result.suppressed],
            "diversity_moves": [
                {
                    "headline": m.headline,
                    "project": m.project,
                    "rank_by_score": m.score_rank,
                    "rank": m.rank,
                }
                for m in result.moved
            ],
            "audience_entries": len(result.audience),
            "estimated_cost": None if estimate.total is None else str(estimate.total),
            "missing_prices": estimate.missing_prices,
            "notes": notes,
        }

    # --- reads ---------------------------------------------------------------------------

    def get_session(self, session_id: str) -> DiscoverySessionView:
        with self._db.transaction() as session:
            return ddb.session_view(session, ddb.get_session_row(session, session_id))

    def list_sessions(
        self, *, account_id: str | None = None, limit: int = 50
    ) -> list[DiscoverySessionView]:
        with self._db.transaction() as session:
            return [
                ddb.session_view(session, r)
                for r in ddb.list_sessions(session, account_id=account_id, limit=limit)
            ]

    def opportunities(self, session_id: str) -> list[OpportunityView]:
        with self._db.transaction() as session:
            ddb.get_session_row(session, session_id)
            return [
                ddb.opportunity_view(session, r) for r in ddb.opportunities_of(session, session_id)
            ]

    def opportunity(self, opportunity_id: str) -> OpportunityView:
        with self._db.transaction() as session:
            return ddb.opportunity_view(session, ddb.get_opportunity(session, opportunity_id))

    def opportunity_items(self, opportunity_id: str) -> list[ItemView]:
        with self._db.transaction() as session:
            ddb.get_opportunity(session, opportunity_id)
            return [ddb.item_view(r) for r in ddb.evidence_rows(session, opportunity_id)]

    def audience(self, session_id: str) -> list[AudienceEntryView]:
        with self._db.transaction() as session:
            ddb.get_session_row(session, session_id)
            return [ddb.audience_view(r) for r in ddb.audience_of(session, session_id)]

    # --- opportunity actions -------------------------------------------------------------

    def dismiss(
        self, opportunity_id: str, body: DismissBody, *, auth: ReviewAuthMethod
    ) -> OpportunityView:
        del auth  # dismissal is recorded by name; the token only gates the action
        with self._db.transaction() as session:
            row = ddb.get_opportunity(session, opportunity_id, lock=True)
            if row.status == OpportunityStatus.DISMISSED.value:
                return ddb.opportunity_view(session, row)
            row.status = OpportunityStatus.DISMISSED.value
            row.dismissed_by = body.reviewer
            row.dismissed_at = self._clock()
            row.dismiss_reason = body.reason
            session.flush()
            return ddb.opportunity_view(session, row)

    def start_run(
        self, opportunity_id: str, body: StartRunFromOpportunity, *, auth: ReviewAuthMethod
    ) -> OpportunityRunCreated:
        """Insert a normal run that researches this opportunity's stored evidence. The
        run starts (and may call models) only after the commit."""
        del auth
        now = self._clock()
        registry = self.registry()
        with self._db.transaction() as session:
            opp = ddb.get_opportunity(session, opportunity_id, lock=True)
            if opp.status == OpportunityStatus.DISMISSED.value:
                raise DiscoveryStateError(f"opportunity {opp.id} was dismissed")
            if now >= opp.expires_at:
                raise DiscoveryStateError(
                    f"opportunity {opp.id} expired; run a new discovery session"
                )
            if body.account.id != opp.account_id:
                raise InvalidDiscoveryRequestError("account.id must be the opportunity's account")
            if body.intent.value not in opp.suggested_intents:
                raise InvalidDiscoveryRequestError(
                    f"intent {body.intent.value} is not suggested for this opportunity "
                    f"(suggested: {', '.join(opp.suggested_intents)})"
                )
            grok = body.intent is ContentIntent.GROK_QUESTION
            if grok and not opp.grok_eligible:
                raise InvalidDiscoveryRequestError("this opportunity is not eligible for @grok")
            evidence = ddb.usable(session, ddb.evidence_rows(session, opp.id))
            if not evidence:
                raise DiscoveryStateError("the opportunity's evidence was purged")
            strategy = self._strategy(session, opp.account_id, body.strategy)
            if strategy is None:
                raise InvalidDiscoveryRequestError(
                    "the account has no active strategy version; pass `strategy`"
                )
            recent = ddb.recent_grok_questions(
                session,
                opp.account_id,
                now - timedelta(days=registry.policy.grok_dedup_days),
            )
            policy = ContentPolicy(
                content_intent=body.intent,
                grounding=grounding_from(
                    [f"{r.title or ''}\n{r.text or ''}" for r in evidence],
                    [r.canonical_url for r in evidence],
                ),
                recent_grok_questions=tuple(recent[-50:]),
                grok_similarity=registry.policy.grok_similarity,
            )
            config = RunConfig(
                max_generation_attempts=body.max_generation_attempts,
                candidates_per_attempt=1 if grok else body.candidates_per_attempt,
                max_research_attempts=1,
                min_signal_posts=1,
                research_query=ResearchQuery(
                    text=opp.headline[:1024], discovery_opportunity_id=opp.id
                ),
                content_policy=policy,
            )
            run_id = self._runs.insert_run(session, body.account, strategy, config)
            session.flush()
            session.execute(update(RunRow).where(RunRow.id == run_id).values(opportunity_id=opp.id))
            if opp.status == OpportunityStatus.OPEN.value:
                opp.status = OpportunityStatus.USED.value
            session.flush()
        self._runs.start_inserted(run_id)
        _log.info(
            "run started from opportunity",
            extra={"opportunity_id": opportunity_id, "run_id": run_id, "intent": body.intent.value},
        )
        return OpportunityRunCreated(
            opportunity=self.opportunity(opportunity_id), run=self._runs.get_summary(run_id)
        )

    @staticmethod
    def _strategy(
        session: Session, account_id: str, fallback: ContentStrategy | None
    ) -> ContentStrategy | None:
        version_id = sdb.active_version_id(session, account_id)
        if version_id is None:
            return fallback
        return ContentStrategy.model_validate(sdb.get_version(session, version_id).content)

    # --- manual export and registration ---------------------------------------------------

    def export(
        self, run_id: str, candidate_id: str, body: ExportBody, *, auth: ReviewAuthMethod
    ) -> ManualExportView:
        """Hand an approved candidate to a person to post by hand. Same checks as an API
        publish request (approval, passing critique, content policy), except that a
        mention is what makes it manual. Idempotent for unchanged content."""
        with self._db.transaction() as session:
            run_row = repo.lock_run(session, run_id)
            candidate = _candidate(session, run_id, candidate_id)
            facts = pubs.publish_facts(session, run_row, candidate, PLATFORM)
            refusals = [r for r in publish_refusals(facts) if r != MANUAL_ONLY_REASON]
            if refusals:
                raise NotPublishableError("; ".join(refusals))
            digest = content_sha256(candidate.content)
            prior = ddb.latest_export(session, run_id, candidate_id)
            if prior is not None and prior.content_sha256 == digest:
                return ddb.export_view(prior, candidate.content)
            row = ManualExportRow(
                id=new_id("mexp"),
                run_id=run_id,
                candidate_id=candidate_id,
                content_sha256=digest,
                content_intent=candidate.content_intent,
                manual_only=is_manual_only(candidate.content),
                exported_by=body.exported_by,
                auth_method=auth.value,
                created_at=self._clock(),
            )
            session.add(row)
            session.flush()
            return ddb.export_view(row, candidate.content)

    def register_manual_publication(
        self,
        run_id: str,
        candidate_id: str,
        body: ManualPublicationBody,
        *,
        auth: ReviewAuthMethod,
    ) -> ManualPublicationView:
        """Link a post the user published by hand to its exported candidate.

        The post is read once from X to check that it belongs to the configured account,
        that its text is the exported text and that it was created after the export.
        Only then is it recorded as published (``provider = 'manual'``) and the usual
        analytics jobs scheduled. Registering the same post again returns the existing
        record; a post can never be linked to two candidates.
        """
        post_id = parse_post_ref(body.post)
        account_id, account_username = self._account()
        with self._db.transaction() as session:
            candidate = _candidate(session, run_id, candidate_id)
            export = ddb.latest_export(session, run_id, candidate_id)
            if export is None:
                raise ManualPublicationError(
                    "export the approved candidate first; only exported candidates can be "
                    "registered"
                )
            known = self._known(session, run_id, candidate_id, post_id)
            if known is not None:
                return known
            content = candidate.content
        if self._verifier is None:
            raise ManualPublicationNotConfiguredError(
                "manual registration needs a post lookup (ANALYTICS_PROVIDER)"
            )
        failure: AnalyticsError | None = None
        post: PlatformPost | None = None
        verifier = self._verifier()
        try:
            post = verifier.lookup_post(post_id)
        except AnalyticsError as exc:
            failure = exc
        if failure is not None:
            raise failure
        if post is None:
            raise ManualPublicationError(f"post {post_id} was not found or is not visible")
        if account_id is not None and post.author_id != account_id:
            raise ManualPublicationError(
                "the post belongs to another account than X_ACCOUNT_USER_ID"
            )
        if (
            account_id is None
            and (post.author_username or "").lower() != (account_username or "").lower()
        ):
            raise ManualPublicationError(
                "the post belongs to another account than X_ACCOUNT_USERNAME"
            )
        match = text_match(content, post.text)
        if match is None:
            raise ManualPublicationError(
                "the post's text differs from the exported candidate; edit it through review "
                "and export again"
            )
        if (
            post.created_at is not None
            and post.created_at < export.created_at - EXPORT_CLOCK_TOLERANCE
        ):
            raise ManualPublicationError("the post was created before the candidate was exported")
        try:
            return self._record_manual(
                run_id, candidate_id, export, post, match, body, auth, verifier.provider_name
            )
        except IntegrityError:
            # A concurrent registration of the same post won; report it.
            with self._db.transaction() as session:
                known = self._known(session, run_id, candidate_id, post_id)
            if known is None:
                raise ManualPublicationError("a concurrent registration conflicted") from None
            return known

    def _record_manual(
        self,
        run_id: str,
        candidate_id: str,
        export: ManualExportRow,
        post: PlatformPost,
        match: TextMatch,
        body: ManualPublicationBody,
        auth: ReviewAuthMethod,
        verification_provider: str,
    ) -> ManualPublicationView:
        url = _post_url(post)
        with self._db.transaction() as session:
            repo.lock_run(session, run_id)
            known = self._known(session, run_id, candidate_id, post.provider_post_id)
            if known is not None:
                return known
            existing = pubs.find_for_target(session, run_id, candidate_id, PLATFORM)
            if existing is not None and existing.status != PublicationStatus.CANCELLED.value:
                raise ManualPublicationError(
                    f"this candidate already has a publication ({existing.status})"
                )
            candidate = _candidate(session, run_id, candidate_id)
            if existing is None:
                publication_id = pubs.insert_intent(
                    session,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    platform=PLATFORM,
                    idempotency_key=idempotency_key(PLATFORM, run_id, candidate_id),
                    content=candidate.content,
                    content_sha256=content_sha256(candidate.content),
                    status=PublicationStatus.READY,
                    scheduled_for=None,
                    requested_by=body.registered_by,
                )
                if publication_id is None:
                    raise ManualPublicationError("a concurrent publication request won")
            else:
                publication_id = existing.id
            pubs.mark_published(
                session,
                publication_id,
                provider_post_id=post.provider_post_id,
                provider_post_url=url,
                resolved_by=body.registered_by,
                resolution_note="published manually; registered and verified",
            )
            session.execute(
                update(PublicationRow)
                .where(PublicationRow.id == publication_id)
                .values(provider="manual", provider_created_at=post.created_at)
            )
            row = ManualPublicationRow(
                id=new_id("mpub"),
                publication_id=publication_id,
                export_id=export.id,
                run_id=run_id,
                candidate_id=candidate_id,
                platform=PLATFORM,
                provider_post_id=post.provider_post_id,
                author_id=post.author_id,
                author_username=post.author_username,
                provider_created_at=post.created_at,
                text_match=match.value,
                verification_provider=verification_provider,
                verification_post_reads=post.post_reads,
                verification_user_reads=post.user_reads,
                registered_by=body.registered_by,
                auth_method=auth.value,
                created_at=self._clock(),
            )
            session.add(row)
            session.flush()
            jobs = (
                self._analytics.enqueue_on_publish_in(session, publication_id)
                if self._analytics is not None
                else []
            )
            return ddb.manual_publication_view(row, provider_post_url=url, analytics_jobs=len(jobs))

    def _known(
        self, session: Session, run_id: str, candidate_id: str, post_id: str
    ) -> ManualPublicationView | None:
        """The existing registration of this post, if any. Linked to another candidate
        or another post already linked to this candidate: refused."""
        prior = ddb.manual_publication_for_post(session, PLATFORM, post_id)
        if prior is not None:
            if (prior.run_id, prior.candidate_id) != (run_id, candidate_id):
                raise ManualPublicationError(
                    f"post {post_id} is already registered for another candidate"
                )
            pub = session.get(PublicationRow, prior.publication_id)
            return ddb.manual_publication_view(
                prior,
                provider_post_url=(pub.provider_post_url if pub else None) or _url_for(post_id),
                already_registered=True,
            )
        for pub in ddb.publications_for_post(session, PLATFORM, post_id):
            raise ManualPublicationError(
                f"post {post_id} is already recorded for publication {pub.id}"
            )
        target = pubs.find_for_target(session, run_id, candidate_id, PLATFORM)
        if target is not None and target.status == PublicationStatus.PUBLISHED.value:
            raise ManualPublicationError(
                f"this candidate is already published as post {target.provider_post_id}"
            )
        return None

    def _account(self) -> tuple[str | None, str | None]:
        cfg = self._config
        if cfg.account_user_id is None and cfg.account_username is None:
            raise ManualPublicationNotConfiguredError(
                "set X_ACCOUNT_USER_ID (or X_ACCOUNT_USERNAME) to the account you post as"
            )
        return cfg.account_user_id, cfg.account_username

    # --- retention -------------------------------------------------------------------------

    def purge(self, body: PurgeRequest, *, auth: ReviewAuthMethod) -> PurgeResult:
        with self._db.transaction() as session:
            return retention.purge(
                session,
                platform=body.platform,
                provider_item_ids=body.provider_item_ids,
                reason=body.reason,
                actor=retention.Actor(name=body.requested_by, auth_method=auth.value),
                now=self._clock(),
            )

    def purge_expired(self) -> PurgeResult:
        """Purge X text older than DISCOVERY_X_RETENTION_DAYS (explicit command)."""
        now = self._clock()
        with self._db.transaction() as session:
            ids = retention.expired_x_ids(
                session, retention_days=self._config.x_retention_days, now=now
            )
            return retention.purge(
                session,
                platform="x",
                provider_item_ids=ids,
                reason=PurgeReason.RETENTION,
                actor=retention.SYSTEM,
                now=now,
            )


# --- helpers ---------------------------------------------------------------------------------


def _candidate(session: Session, run_id: str, candidate_id: str) -> ContentCandidateRow:
    row = session.get(ContentCandidateRow, candidate_id)
    if row is None or row.run_id != run_id:
        raise CandidateNotFoundError(f"{candidate_id} is not a candidate of run {run_id}")
    return row


def parse_post_ref(ref: str) -> str:
    """A post id from an x.com/twitter.com status URL or a bare id."""
    value = ref.strip()
    if _POST_ID.match(value):
        return value
    match = _POST_URL.match(value)
    if match is None:
        raise InvalidDiscoveryRequestError(
            "post must be an x.com/twitter.com status URL or a numeric post id"
        )
    return match.group(1)


def _normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(text))
    value = _LINK.sub("<link>", value)  # X rewrites every link to t.co
    return " ".join(value.split())


def text_match(candidate: str, posted: str) -> TextMatch | None:
    if candidate == posted:
        return TextMatch.EXACT
    if _normalized(candidate) == _normalized(posted):
        return TextMatch.NORMALIZED
    return None


def _url_for(post_id: str) -> str:
    return f"https://x.com/i/web/status/{post_id}"


def _post_url(post: PlatformPost) -> str:
    if post.author_username:
        return f"https://x.com/{post.author_username}/status/{post.provider_post_id}"
    return _url_for(post.provider_post_id)


__all__ = [
    "DiscoveryConfig",
    "DiscoveryService",
    "DismissBody",
    "ExportBody",
    "ManualPublicationBody",
    "OpportunityRunCreated",
    "parse_post_ref",
    "text_match",
]
