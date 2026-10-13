"""Content discovery (Phase 9): sources, evidence items, opportunities and audience.

Discovery is retrieval plus deterministic analysis. Nothing in these models is written
by a model: verification status, claim hints, scores and audience signals are computed
in code from what the providers returned, and every one of them points back at the items
that support it.

Two words are kept apart on purpose:

- *credibility* describes the source (an official vendor feed, a research archive, a
  community forum). It is not a statement about whether any claim is true.
- *verification status* describes what the evidence shows about a claim, and never goes
  beyond "a primary source says so" or "independent primary sources state the same
  specific facts". There is no "true" value.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from social_growth_agent.models.account import Account, ContentStrategy
from social_growth_agent.models.base import DomainModel, new_id, utc_now
from social_growth_agent.models.content import ContentIntent

type SourcePlatform = Literal["x", "feed", "hn", "github"]


class SourceKind(StrEnum):
    X_SEARCH = "x_search"
    """X recent search for a topic query (the Phase 3 provider)."""
    X_ACCOUNTS = "x_accounts"
    """The same X endpoint, one ``from:`` query over the curated handles."""
    FEED = "feed"
    """An RSS 2.0 or Atom feed published by the source itself."""
    GITHUB_RELEASES = "github_releases"
    """GitHub REST API: the latest releases of one repository."""
    HN_SEARCH = "hn_search"
    """Hacker News search (Algolia HN API)."""


class SourceCategory(StrEnum):
    PRIMARY = "primary"
    """Evidence published by the subject or its authors: announcements, releases, papers,
    documentation, engineering blogs."""
    DISCUSSION = "discussion"
    """People talking about something: X posts, Hacker News threads."""


class CredibilityTier(StrEnum):
    """How much a source is trusted *as a publisher*. Not factual correctness."""

    OFFICIAL = "official"
    PRIMARY_RESEARCH = "primary_research"
    COMMUNITY = "community"
    UNKNOWN = "unknown"


class EvidenceType(StrEnum):
    OFFICIAL_ANNOUNCEMENT = "official_announcement"
    RELEASE = "release"
    PAPER = "paper"
    DOCUMENTATION = "documentation"
    ENGINEERING_BLOG = "engineering_blog"
    DISCUSSION_POST = "discussion_post"
    DISCUSSION_THREAD = "discussion_thread"


PRIMARY_EVIDENCE = frozenset(
    {
        EvidenceType.OFFICIAL_ANNOUNCEMENT,
        EvidenceType.RELEASE,
        EvidenceType.PAPER,
        EvidenceType.DOCUMENTATION,
        EvidenceType.ENGINEERING_BLOG,
    }
)


class VerificationStatus(StrEnum):
    """What the evidence establishes, never more.

    ``primary_source``: published by the subject itself (a vendor's release notes, the
    authors' paper). It says the source *states* something, not that it is correct.
    ``corroborated``: claim level only. Two primary sources from different publishers
    contain the same specific anchors (numbers, versions) the claim relies on. Without such
    anchors corroboration cannot be established deterministically and is never claimed.
    """

    UNVERIFIED = "unverified"
    PRIMARY_SOURCE = "primary_source"
    CORROBORATED = "corroborated"


class ClaimKind(StrEnum):
    """What kind of statement a finding is. Validated in code against its evidence."""

    REPORTED_FACT = "reported_fact"
    """Stated by a cited primary source. Attributed, never asserted as settled truth."""
    UNVERIFIED_CLAIM = "unverified_claim"
    """Presented as fact but supported only by discussion sources."""
    OPINION = "opinion"
    PREDICTION = "prediction"
    OPEN_QUESTION = "open_question"
    DISPUTED = "disputed"
    """Cited sources take different positions. Represented, never manufactured."""


class ClaimHintKind(StrEnum):
    """Deterministic, text-surface hints computed before any model sees an item."""

    QUESTION = "question"
    OPINION = "opinion"
    PREDICTION = "prediction"
    DISAGREEMENT = "disagreement"
    ANNOUNCEMENT = "announcement"


# --- registry ----------------------------------------------------------------------


class SourceSpec(DomainModel):
    """One configured source (``discovery.toml``). Sources come from configuration only,
    never from retrieved content."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,47}$")
    kind: SourceKind
    category: SourceCategory
    publisher: str = Field(min_length=1, max_length=80)
    tier: CredibilityTier
    evidence_type: EvidenceType
    enabled: bool = True
    url: str | None = Field(default=None, max_length=512, description="Feed URL (feed).")
    repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
    query: str | None = Field(default=None, max_length=512)
    handles: list[str] = Field(default_factory=list, max_length=25)
    topics: list[str] = Field(default_factory=list)
    evergreen: bool = Field(
        default=False,
        description="Material stays relevant beyond the freshness window (documentation).",
    )

    @model_validator(mode="after")
    def _kind_fields(self) -> "SourceSpec":
        needs = {
            SourceKind.FEED: self.url,
            SourceKind.GITHUB_RELEASES: self.repo,
            SourceKind.X_SEARCH: self.query,
            SourceKind.HN_SEARCH: self.query,
        }
        if self.kind in needs and not needs[self.kind]:
            raise ValueError(f"source {self.id} ({self.kind.value}) is missing its target")
        if self.kind is SourceKind.FEED and not (self.url or "").startswith("https://"):
            raise ValueError(f"source {self.id}: feeds must use https")
        for handle in self.handles:
            if not handle or len(handle) > 15 or not handle.replace("_", "").isalnum():
                raise ValueError(f"source {self.id}: invalid X handle {handle!r}")
        return self


class RankingWeights(DomainModel):
    topic_relevance: float = Field(default=0.25, ge=0, le=1)
    timeliness: float = Field(default=0.15, ge=0, le=1)
    source_credibility: float = Field(default=0.15, ge=0, le=1)
    evidence_quality: float = Field(default=0.15, ge=0, le=1)
    novelty: float = Field(default=0.10, ge=0, le=1)
    discussion_potential: float = Field(default=0.05, ge=0, le=1)
    strategy_fit: float = Field(default=0.15, ge=0, le=1)


class DiscoveryPolicy(DomainModel):
    """Freshness, deduplication and suppression thresholds (``discovery.toml``)."""

    timeliness_half_life_hours: float = Field(default=72.0, gt=0)
    cluster_similarity: float = Field(default=0.6, gt=0, le=1)
    own_post_similarity: float = Field(default=0.6, gt=0, le=1)
    novelty_window_days: int = Field(default=30, ge=1, le=365)
    fingerprint_suppression_days: int = Field(default=14, ge=0, le=365)
    dismissed_suppression_days: int = Field(default=30, ge=0, le=365)
    opportunity_ttl_days: int = Field(default=7, ge=1, le=60)
    grok_freshness_days: int = Field(default=14, ge=1, le=90)
    grok_dedup_days: int = Field(default=90, ge=1, le=365)
    grok_similarity: float = Field(default=0.5, gt=0, le=1)
    max_opportunities: int = Field(default=10, ge=1, le=50)
    shortlist_size: int = Field(
        default=5, ge=1, le=50, description="How many top opportunities the diversity cap shapes."
    )
    max_per_project_in_shortlist: int = Field(
        default=2,
        ge=1,
        le=50,
        description="At most this many shortlist places per software project (GitHub "
        "owner/repo); the others follow in score order, none is dropped.",
    )
    max_audience_entries: int = Field(default=10, ge=0, le=50)


class Registry(DomainModel):
    version: str = Field(min_length=1, max_length=64)
    vocabulary: list[str] = Field(min_length=1)
    generic_terms: list[str] = Field(
        default_factory=list,
        description="Vocabulary terms too broad to make a subject specific (Grok criterion 3).",
    )
    sources: list[SourceSpec]
    weights: RankingWeights = Field(default_factory=RankingWeights)
    policy: DiscoveryPolicy = Field(default_factory=DiscoveryPolicy)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Registry":
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        return self

    def source(self, source_id: str) -> SourceSpec | None:
        return next((s for s in self.sources if s.id == source_id), None)


# --- limits ------------------------------------------------------------------------

HARD_LIMITS: dict[str, int] = {
    "x_requests": 5,
    "posts_per_x_request": 100,
    "user_reads": 100,
    "feeds": 20,
    "items_per_feed": 50,
    "github_requests": 10,
    "hn_requests": 2,
}


class DiscoveryLimits(DomainModel):
    """Caps for one discovery session. Defaults are the approved D9 budget; the maxima are
    hard limits a request can never raise. Nothing paginates and nothing retries."""

    x_requests: int = Field(default=2, ge=0, le=HARD_LIMITS["x_requests"])
    posts_per_x_request: int = Field(default=10, ge=10, le=HARD_LIMITS["posts_per_x_request"])
    user_reads: int = Field(default=20, ge=0, le=HARD_LIMITS["user_reads"])
    feeds: int = Field(default=8, ge=0, le=HARD_LIMITS["feeds"])
    items_per_feed: int = Field(default=20, ge=1, le=HARD_LIMITS["items_per_feed"])
    github_requests: int = Field(default=4, ge=0, le=HARD_LIMITS["github_requests"])
    hn_requests: int = Field(default=1, ge=0, le=HARD_LIMITS["hn_requests"])
    cache_hours: int = Field(default=6, ge=0, le=72)


# --- items -------------------------------------------------------------------------


class DiscoveredItem(DomainModel):
    """One normalized item as a provider returned it (before persistence).

    ``provider_item_id`` is the platform's own id (X post id, HN object id, GitHub
    release id) or, for feeds, the entry id/link. Metrics are None when not returned.
    """

    source_id: str
    platform: SourcePlatform
    kind: SourceKind
    category: SourceCategory
    evidence_type: EvidenceType
    tier: CredibilityTier
    publisher: str
    provider_item_id: str = Field(min_length=1, max_length=512)
    url: str | None = None
    title: str | None = None
    text: str = Field(description="Sanitized text or excerpt (bounded).")
    author: str | None = None
    author_id: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    likes: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    impressions: int | None = Field(default=None, ge=0)
    points: int | None = Field(default=None, ge=0, description="Hacker News points.")
    comments: int | None = Field(default=None, ge=0, description="Hacker News comments.")
    evergreen: bool = False


class ItemView(DomainModel):
    """A stored item as the API shows it. Purged items keep ids, URL and hashes only."""

    id: str
    source_id: str
    platform: SourcePlatform
    category: SourceCategory
    evidence_type: EvidenceType
    tier: CredibilityTier
    verification_status: VerificationStatus
    publisher: str
    provider_item_id: str
    url: str | None
    title: str | None
    text: str | None
    author: str | None
    published_at: datetime | None
    first_retrieved_at: datetime
    last_retrieved_at: datetime
    keywords: list[str]
    metrics: dict[str, int | None]
    claim_hints: list[ClaimHintKind]
    purged: bool
    purge_reason: str | None = None


# --- opportunities -----------------------------------------------------------------


class ScoreComponent(DomainModel):
    name: str
    value: float = Field(ge=0, le=1)
    weight: float = Field(ge=0, le=1)
    detail: str


class ClaimHint(DomainModel):
    item_id: str
    hint: ClaimHintKind
    method: Literal["heuristic"] = "heuristic"


class GrokEligibility(DomainModel):
    """Opportunity-level eligibility (criteria 1, 2, 3, 5 and 7). Criteria 4 (a rationale
    for asking Grok) and 6 (only ``@grok``, once) are enforced on each candidate."""

    eligible: bool
    passed: list[str]
    failed: list[str]
    similar_to: str | None = Field(
        default=None, description="Earlier Grok question this one is too close to."
    )


class OpportunityStatus(StrEnum):
    OPEN = "open"
    USED = "used"
    DISMISSED = "dismissed"


class EvidenceRole(StrEnum):
    PRIMARY = "primary"
    DISCUSSION = "discussion"


class OpportunityEvidence(DomainModel):
    item_id: str
    role: EvidenceRole
    url: str | None
    title: str | None
    publisher: str
    evidence_type: EvidenceType
    verification_status: VerificationStatus
    published_at: datetime | None
    purged: bool = False


class OpportunityView(DomainModel):
    id: str
    session_id: str
    account_id: str
    rank: int
    score: float
    fingerprint: str
    keywords: list[str]
    headline: str = Field(description="Primary item title, or the keywords for X-only topics.")
    components: list[ScoreComponent]
    reasons: list[str]
    claim_hints: list[ClaimHint]
    claim_summary: dict[str, int]
    suggested_intents: list[ContentIntent]
    grok: GrokEligibility
    evidence: list[OpportunityEvidence]
    audience_entry_ids: list[str]
    status: OpportunityStatus
    expires_at: datetime
    run_ids: list[str] = Field(default_factory=list)
    synthetic: bool


class AudienceKind(StrEnum):
    ACCOUNT = "account"
    DISCUSSION = "discussion"


class AudienceSignal(DomainModel):
    name: str
    value: float | int | None
    detail: str


class AudienceEntryView(DomainModel):
    """A read-only suggestion of where a contribution could be useful. Never an action."""

    id: str
    session_id: str
    kind: AudienceKind
    ref: str = Field(description="@handle for accounts; the thread URL for discussions.")
    platform: SourcePlatform
    score: float
    signals: list[AudienceSignal]
    matched_topics: list[str]
    explanation: str
    evidence_item_ids: list[str]
    curated: bool


# --- sessions ----------------------------------------------------------------------


class DiscoverySessionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    """Finished, but at least one source failed (see the fetch ledger)."""
    FAILED = "failed"


class DiscoveryFetchOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    CACHED = "cached"
    """Served from items retrieved within ``cache_hours``: no external call."""
    NOT_MODIFIED = "not_modified"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Not called because a cap was reached (the reason is recorded)."""


class DiscoveryFetchView(DomainModel):
    id: str
    source_id: str
    kind: SourceKind
    provider: str
    outcome: DiscoveryFetchOutcome
    request: dict[str, str | int | None]
    requests_made: int
    items_returned: int
    post_reads: int
    user_reads: int
    http_status: int | None
    error_category: str | None
    error_message: str | None
    rate_limit_reset_at: datetime | None
    latency_ms: float | None
    started_at: datetime
    finished_at: datetime | None


class DiscoveryRequest(DomainModel):
    account_id: str = Field(min_length=1, max_length=64)
    sources: list[str] | None = Field(
        default=None, description="Registry source ids; None means every enabled source."
    )
    limits: DiscoveryLimits = Field(default_factory=DiscoveryLimits)
    refresh: bool = Field(default=False, description="Ignore the cache window.")
    requested_by: str = Field(min_length=1, max_length=128)


class PlannedCall(DomainModel):
    source_id: str
    kind: SourceKind
    calls: int
    max_post_reads: int
    max_user_reads: int
    note: str


class DiscoveryPlan(DomainModel):
    """What a session would call, and its maximum X read cost. Calls nothing."""

    account_id: str
    calls: list[PlannedCall]
    skipped: list[str]
    max_x_post_reads: int
    max_x_user_reads: int
    estimated_max_cost: str | None = Field(
        description="Discovery reads only, at the configured prices; None when a price is "
        "unknown. Excludes later research, generation, publishing and analytics."
    )
    missing_prices: list[str]
    pricing_id: str


def new_session_id() -> str:
    return new_id("disc")


def new_opportunity_id() -> str:
    return new_id("opp")


class DiscoverySessionView(DomainModel):
    id: str
    account_id: str
    status: DiscoverySessionStatus
    synthetic: bool
    request: dict[str, object]
    limits: DiscoveryLimits
    registry_version: str
    registry_sha256: str
    strategy_version_id: str | None
    pricing_id: str | None
    requested_by: str
    auth_method: str
    summary: dict[str, object] | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    fetches: list[DiscoveryFetchView] = Field(default_factory=list)


# --- runs, manual publication and retention ------------------------------------------


class StartRunFromOpportunity(DomainModel):
    """Start a normal run from an opportunity. ``strategy`` is only needed when the
    account has no active strategy version (Phase 7)."""

    account: Account
    intent: ContentIntent
    strategy: ContentStrategy | None = None
    reviewer: str = Field(min_length=1, max_length=128)
    max_generation_attempts: int = Field(default=3, ge=1, le=10)
    candidates_per_attempt: int = Field(default=2, ge=1, le=10)


class ManualExportView(DomainModel):
    """An approved candidate handed to a person to post by hand."""

    id: str
    run_id: str
    candidate_id: str
    content: str
    content_sha256: str
    content_intent: ContentIntent | None
    manual_only: bool
    exported_by: str
    auth_method: str
    created_at: datetime
    instructions: list[str]
    intent_url: str = Field(
        description="x.com compose link pre-filled with the text; you press Post yourself."
    )


class TextMatch(StrEnum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    """Equal after whitespace, Unicode and t.co link normalization."""


class ManualPublicationView(DomainModel):
    id: str
    publication_id: str
    export_id: str
    run_id: str
    candidate_id: str
    platform: str
    provider_post_id: str
    provider_post_url: str
    author_id: str
    author_username: str | None
    provider_created_at: datetime | None
    text_match: TextMatch
    verification_provider: str
    registered_by: str
    auth_method: str
    created_at: datetime
    already_registered: bool = False
    analytics_jobs: int = 0


class PurgeReason(StrEnum):
    DELETION_REQUEST = "deletion_request"
    """The content was deleted or made unavailable on the platform, or a person asked."""
    RETENTION = "retention"
    """Older than the retention window."""


class PurgeRequest(DomainModel):
    platform: SourcePlatform = "x"
    provider_item_ids: list[str] = Field(min_length=1, max_length=500)
    reason: PurgeReason = PurgeReason.DELETION_REQUEST
    requested_by: str = Field(min_length=1, max_length=128)


class PurgeResult(DomainModel):
    platform: SourcePlatform
    provider_item_ids: list[str]
    reason: PurgeReason
    discovery_items: int
    source_posts: int
    checkpoint_blobs: int
    checkpoint_writes: int
    tombstones: int
