"""Deterministic discovery analysis: topic clusters, opportunity ranking, Grok
eligibility and the audience shortlist.

Pure functions over plain data: same inputs, same output, no I/O, no model. Every score
keeps its components and the inputs behind them, so a reviewer can see why an idea was
proposed. Controversy, outrage and reply ratios are never rewarded: discussion counts have
a small, capped weight, and an opportunity without primary evidence is capped.
"""

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from social_growth_agent.discovery.text import (
    anchors_in,
    jaccard,
    matched_terms,
    token_set,
)
from social_growth_agent.models.content import ContentIntent
from social_growth_agent.models.discovery import (
    AudienceKind,
    AudienceSignal,
    ClaimHint,
    ClaimHintKind,
    CredibilityTier,
    DiscoveryPolicy,
    EvidenceRole,
    EvidenceType,
    GrokEligibility,
    RankingWeights,
    ScoreComponent,
    SourceCategory,
    SourcePlatform,
    VerificationStatus,
)

TIER_SCORE = {
    CredibilityTier.OFFICIAL: 1.0,
    CredibilityTier.PRIMARY_RESEARCH: 0.9,
    CredibilityTier.COMMUNITY: 0.4,
    CredibilityTier.UNKNOWN: 0.1,
}
DISCUSSION_ONLY_CAP = 0.5
OWN_POST_PENALTY = 0.5


@dataclass(frozen=True)
class AnalysisItem:
    """A stored, unpurged item as analysis sees it."""

    id: str
    source_id: str
    platform: SourcePlatform
    category: SourceCategory
    evidence_type: EvidenceType
    tier: CredibilityTier
    verification: VerificationStatus
    publisher: str
    url: str | None
    title: str | None
    text: str
    author: str | None
    published_at: datetime | None
    hints: tuple[ClaimHintKind, ...]
    provider_item_id: str = ""
    evergreen: bool = False
    curated: bool = False
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    quotes: int | None = None
    points: int | None = None
    comments: int | None = None

    @property
    def body(self) -> str:
        return f"{self.title or ''}\n{self.text}".strip()

    @property
    def is_primary(self) -> bool:
        return self.verification is not VerificationStatus.UNVERIFIED


@dataclass(frozen=True)
class PriorOpportunity:
    fingerprint: str
    keywords: frozenset[str]
    created_at: datetime
    dismissed: bool


@dataclass(frozen=True)
class AnalysisContext:
    now: datetime
    vocabulary: list[str]
    pillars: list[str]
    avoid_topics: list[str]
    weights: RankingWeights
    policy: DiscoveryPolicy
    prior_opportunities: Sequence[PriorOpportunity] = ()
    own_posts: Sequence[str] = ()
    """Texts of this account's posts published within the novelty window."""
    prior_grok_questions: Sequence[str] = ()
    """Grok-question candidates drafted within the dedup window."""
    generic_terms: frozenset[str] = frozenset()
    """Vocabulary terms too broad to make a subject specific (Grok criterion 3); from the
    registry, so each domain defines its own."""


@dataclass(frozen=True)
class RankedOpportunity:
    fingerprint: str
    item_ids: tuple[str, ...]
    roles: dict[str, EvidenceRole]
    keywords: tuple[str, ...]
    headline: str
    score: float
    components: tuple[ScoreComponent, ...]
    reasons: tuple[str, ...]
    hints: tuple[ClaimHint, ...]
    claim_summary: dict[str, int]
    intents: tuple[ContentIntent, ...]
    grok: GrokEligibility
    project: str | None = None
    """The software project the primary evidence belongs to (``owner/repo`` for GitHub
    links), used only by the shortlist diversity cap."""


@dataclass(frozen=True)
class DiversityMove:
    """An opportunity placed below the shortlist by the per-project cap. Its score and
    evidence are unchanged; only its position is."""

    fingerprint: str
    headline: str
    project: str
    score_rank: int
    rank: int


@dataclass(frozen=True)
class Suppressed:
    fingerprint: str
    headline: str
    reason: str


@dataclass(frozen=True)
class AudienceSuggestion:
    kind: AudienceKind
    ref: str
    platform: SourcePlatform
    score: float
    signals: tuple[AudienceSignal, ...]
    matched_topics: tuple[str, ...]
    explanation: str
    evidence_item_ids: tuple[str, ...]
    curated: bool


@dataclass(frozen=True)
class AnalysisResult:
    opportunities: tuple[RankedOpportunity, ...]
    suppressed: tuple[Suppressed, ...]
    audience: tuple[AudienceSuggestion, ...]
    clusters: int
    moved: tuple[DiversityMove, ...] = ()


# --- clustering --------------------------------------------------------------------


def cluster_items(items: Sequence[AnalysisItem], threshold: float) -> list[list[AnalysisItem]]:
    """Union-find over: the same canonical URL, or token-set Jaccard >= threshold."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_url: dict[str, int] = {}
    for i, item in enumerate(items):
        if item.url:
            if item.url in by_url:
                union(by_url[item.url], i)
            else:
                by_url[item.url] = i
    sets = [token_set(item.body) for item in items]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if jaccard(sets[i], sets[j]) >= threshold:
                union(i, j)
    groups: dict[int, list[AnalysisItem]] = defaultdict(list)
    for i, item in enumerate(items):
        groups[find(i)].append(item)
    return [groups[k] for k in sorted(groups)]


def fingerprint(cluster: Sequence[AnalysisItem], keywords: Sequence[str]) -> str:
    primary_urls = sorted({i.url for i in cluster if i.is_primary and i.url})
    basis = "|".join(primary_urls) if primary_urls else "|".join(sorted(keywords)[:5])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


# --- ranking -----------------------------------------------------------------------


def analyze(items: Sequence[AnalysisItem], ctx: AnalysisContext) -> AnalysisResult:
    clusters = cluster_items(items, ctx.policy.cluster_similarity)
    ranked: list[RankedOpportunity] = []
    suppressed: list[Suppressed] = []
    own_sets = [token_set(text) for text in ctx.own_posts]
    for cluster in clusters:
        outcome = _score_cluster(cluster, ctx, own_sets)
        if isinstance(outcome, Suppressed):
            suppressed.append(outcome)
        elif outcome is not None:
            ranked.append(outcome)
    ranked.sort(key=lambda o: (-o.score, o.fingerprint))
    ranked = _one_grok_question(ranked)[: ctx.policy.max_opportunities]
    ranked, moved = diversify(
        ranked, ctx.policy.shortlist_size, ctx.policy.max_per_project_in_shortlist
    )
    audience = build_audience(items, ctx)
    return AnalysisResult(
        opportunities=tuple(ranked),
        suppressed=tuple(suppressed),
        audience=audience,
        clusters=len(clusters),
        moved=moved,
    )


def diversify(
    ranked: Sequence[RankedOpportunity], size: int, per_project: int
) -> tuple[list[RankedOpportunity], tuple[DiversityMove, ...]]:
    """Fill the first ``size`` places in score order, admitting at most ``per_project``
    opportunities of one project; everything else follows in score order (ties keep
    the fingerprint order of ``analyze``). Nothing is
    dropped, no score changes, and the same input always gives the same order. A moved
    opportunity says why in its reasons."""
    top: list[RankedOpportunity] = []
    rest: list[RankedOpportunity] = []
    held: set[str] = set()
    counts: Counter[str] = Counter()
    for opp in ranked:
        project = opp.project
        if len(top) < size and (project is None or counts[project] < per_project):
            top.append(opp)
            if project is not None:
                counts[project] += 1
        else:
            if len(top) < size:
                held.add(opp.fingerprint)  # it would have made the shortlist on score
            rest.append(opp)
    score_rank = {o.fingerprint: i for i, o in enumerate(ranked, start=1)}
    out: list[RankedOpportunity] = []
    moved: list[DiversityMove] = []
    for rank, opp in enumerate(top + rest, start=1):
        project = opp.project
        if opp.fingerprint in held and project is not None:
            was = score_rank[opp.fingerprint]
            note = (
                f"shortlist diversity: placed below the top {size} because {per_project} "
                f"higher-ranked opportunit{'y' if per_project == 1 else 'ies'} from "
                f"{project} {'is' if per_project == 1 else 'are'} already there "
                f"(score unchanged; rank {was} by score)"
            )
            opp = replace(opp, reasons=(*opp.reasons, note))
            moved.append(DiversityMove(opp.fingerprint, opp.headline, project, was, rank))
        out.append(opp)
    return out, tuple(moved)


def project_of(url: str | None) -> str | None:
    """``owner/repo`` for a GitHub URL, else None (no cap applies)."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.hostname not in ("github.com", "www.github.com"):
        return None
    segments = [p for p in parts.path.split("/") if p]
    return "/".join(segments[:2]).lower() if len(segments) >= 2 else None


def _score_cluster(
    cluster: Sequence[AnalysisItem], ctx: AnalysisContext, own_sets: list[frozenset[str]]
) -> RankedOpportunity | Suppressed | None:
    text = "\n".join(i.body for i in cluster)
    keywords = matched_terms(text, ctx.vocabulary)
    if not keywords:
        return None  # off-topic: not an opportunity at all
    fp = fingerprint(cluster, keywords)
    primaries = [i for i in cluster if i.is_primary]
    headline = _headline(cluster, keywords)
    avoided = matched_terms(text, ctx.avoid_topics)
    if avoided:
        return Suppressed(fp, headline, f"matches avoided topics: {', '.join(avoided)}")
    blocked = _suppression(fp, primaries, ctx)
    if blocked:
        return Suppressed(fp, headline, blocked)
    cluster_set = token_set(text)
    own_overlap = max((jaccard(cluster_set, s) for s in own_sets), default=0.0)
    if own_overlap >= ctx.policy.own_post_similarity:
        return Suppressed(
            fp, headline, f"too similar to a recent post of this account ({own_overlap:.2f})"
        )

    w = ctx.weights
    newest = _newest(primaries or list(cluster))
    age_h = None if newest is None else max(0.0, (ctx.now - newest).total_seconds() / 3600)
    evergreen = any(i.evergreen for i in primaries)
    timeliness = (
        0.0 if age_h is None else math.pow(0.5, age_h / ctx.policy.timeliness_half_life_hours)
    )
    if evergreen:
        timeliness = max(timeliness, 0.5)
    publishers = {i.publisher.lower() for i in primaries}
    evidence_q = 0.1 if not primaries else (0.6 if len(publishers) < 2 else 1.0)
    credibility = max(TIER_SCORE[i.tier] for i in cluster)
    kw_set = frozenset(keywords)
    recent = [
        p
        for p in ctx.prior_opportunities
        if p.fingerprint != fp
        and p.created_at >= ctx.now - timedelta(days=ctx.policy.novelty_window_days)
    ]
    closest = max((jaccard(kw_set, p.keywords) for p in recent), default=0.0)
    novelty = 1.0 - closest
    threads = sum(1 for i in cluster if i.category is SourceCategory.DISCUSSION)
    questions = sum(1 for i in cluster if ClaimHintKind.QUESTION in i.hints)
    discussion = min(1.0, (threads + questions) / 4)
    pillar_hits = [p for p in ctx.pillars if token_set(p) & cluster_set]
    strategy_fit = len(pillar_hits) / len(ctx.pillars) if ctx.pillars else 0.0
    relevance = min(1.0, len(keywords) / 3)

    components = (
        ScoreComponent(
            name="topic_relevance",
            value=relevance,
            weight=w.topic_relevance,
            detail=f"matched {len(keywords)} vocabulary terms: {', '.join(keywords[:6])}",
        ),
        ScoreComponent(
            name="timeliness",
            value=round(timeliness, 4),
            weight=w.timeliness,
            detail="no publication time"
            if age_h is None
            else f"newest {'primary ' if primaries else ''}item {age_h:.0f} h old"
            + (" (evergreen)" if evergreen else ""),
        ),
        ScoreComponent(
            name="source_credibility",
            value=credibility,
            weight=w.source_credibility,
            detail="best publisher tier (credibility is not factual correctness)",
        ),
        ScoreComponent(
            name="evidence_quality",
            value=evidence_q,
            weight=w.evidence_quality,
            detail=f"{len(primaries)} primary item(s) from {len(publishers)} publisher(s)",
        ),
        ScoreComponent(
            name="novelty",
            value=round(novelty, 4),
            weight=w.novelty,
            detail=f"closest recent opportunity overlap {closest:.2f}",
        ),
        ScoreComponent(
            name="discussion_potential",
            value=round(discussion, 4),
            weight=w.discussion_potential,
            detail=f"{threads} discussion item(s), {questions} question-shaped (capped)",
        ),
        ScoreComponent(
            name="strategy_fit",
            value=round(strategy_fit, 4),
            weight=w.strategy_fit,
            detail=f"pillars matched: {', '.join(pillar_hits) or 'none'}",
        ),
        ScoreComponent(
            name="own_post_overlap",
            value=round(own_overlap, 4),
            weight=0.0,
            detail=f"penalty: score x (1 - {OWN_POST_PENALTY} x overlap)",
        ),
    )
    total_w = sum(c.weight for c in components) or 1.0
    score = sum(c.value * c.weight for c in components) / total_w
    score *= 1 - OWN_POST_PENALTY * own_overlap
    reasons = [f"on topic: {', '.join(keywords[:4])}"]
    if primaries:
        reasons.append(
            f"primary evidence from {', '.join(sorted({i.publisher for i in primaries}))}"
        )
    else:
        score = min(score, DISCUSSION_ONLY_CAP)
        reasons.append("discussion only: no primary evidence (score capped)")
    if threads:
        reasons.append(f"{threads} public discussion item(s)")
    if questions:
        reasons.append(f"{questions} item(s) ask a question (heuristic)")
    if age_h is not None and age_h <= 48:
        reasons.append("published within the last 48 hours")

    hints = tuple(ClaimHint(item_id=i.id, hint=h) for i in cluster for h in i.hints)
    summary = Counter(h.hint.value for h in hints)
    roles = {
        i.id: EvidenceRole.PRIMARY
        if i.category is SourceCategory.PRIMARY
        else EvidenceRole.DISCUSSION
        for i in cluster
    }
    grok = grok_eligibility(cluster, keywords, ctx)
    intents = suggest_intents(cluster, grok)
    return RankedOpportunity(
        fingerprint=fp,
        item_ids=tuple(i.id for i in cluster),
        roles=roles,
        keywords=tuple(keywords),
        headline=headline,
        score=round(score, 4),
        components=components,
        reasons=tuple(reasons),
        hints=hints,
        claim_summary=dict(sorted(summary.items())),
        intents=intents,
        grok=grok,
        project=next((p for i in primaries if (p := project_of(i.url)) is not None), None),
    )


def _suppression(fp: str, primaries: Sequence[AnalysisItem], ctx: AnalysisContext) -> str | None:
    for prior in ctx.prior_opportunities:
        if prior.fingerprint != fp:
            continue
        age = ctx.now - prior.created_at
        if prior.dismissed and age < timedelta(days=ctx.policy.dismissed_suppression_days):
            return f"dismissed {age.days} day(s) ago"
        if not prior.dismissed and age < timedelta(days=ctx.policy.fingerprint_suppression_days):
            newest = _newest(primaries)
            if newest is None or newest <= prior.created_at:
                return f"already proposed {age.days} day(s) ago with no newer primary evidence"
    return None


def _newest(items: Iterable[AnalysisItem]) -> datetime | None:
    times = [i.published_at for i in items if i.published_at is not None]
    return max(times) if times else None


def _headline(cluster: Sequence[AnalysisItem], keywords: Sequence[str]) -> str:
    """A primary item's own title when there is one; X-only topics show keywords, so no
    X text is copied into the opportunity."""
    for item in sorted(cluster, key=lambda i: (not i.is_primary, i.platform == "x")):
        if item.title and item.platform != "x":
            return item.title
    return "Topic: " + ", ".join(keywords[:5])


def suggest_intents(
    cluster: Sequence[AnalysisItem], grok: GrokEligibility
) -> tuple[ContentIntent, ...]:
    types = {i.evidence_type for i in cluster if i.is_primary}
    intents: list[ContentIntent] = []
    if types & {EvidenceType.RELEASE, EvidenceType.OFFICIAL_ANNOUNCEMENT}:
        intents.append(ContentIntent.NEWS_COMMENTARY)
    if EvidenceType.PAPER in types:
        intents.append(ContentIntent.RESEARCH_SUMMARY)
    if types & {EvidenceType.DOCUMENTATION, EvidenceType.ENGINEERING_BLOG, EvidenceType.RELEASE}:
        intents.append(ContentIntent.TECHNICAL_EXPLANATION)
    if any(
        h in (ClaimHintKind.QUESTION, ClaimHintKind.DISAGREEMENT) for i in cluster for h in i.hints
    ):
        intents.append(ContentIntent.OPEN_QUESTION)
    intents.append(ContentIntent.ORIGINAL_INSIGHT)
    if grok.eligible:
        intents.append(ContentIntent.GROK_QUESTION)
    return tuple(dict.fromkeys(intents))


# --- Grok eligibility ---------------------------------------------------------------


def grok_eligibility(
    cluster: Sequence[AnalysisItem], keywords: Sequence[str], ctx: AnalysisContext
) -> GrokEligibility:
    passed: list[str] = []
    failed: list[str] = []
    window = timedelta(days=ctx.policy.grok_freshness_days)
    fresh = [
        i
        for i in cluster
        if i.is_primary
        and (i.evergreen or (i.published_at is not None and ctx.now - i.published_at <= window))
    ]
    _check(
        bool(fresh),
        f"1 recent primary source (<= {ctx.policy.grok_freshness_days} days, or evergreen)",
        passed,
        failed,
    )
    unresolved = [
        i
        for i in cluster
        if ClaimHintKind.QUESTION in i.hints or ClaimHintKind.DISAGREEMENT in i.hints
    ]
    _check(
        bool(unresolved),
        "2 an open question or disagreement in the evidence (heuristic; re-validated "
        "from the research findings in the run)",
        passed,
        failed,
    )
    specific = [k for k in keywords if k not in ctx.generic_terms] or [
        a for i in cluster for a in sorted(anchors_in(i.title or ""))
    ]
    _check(bool(specific), "3 a specific subject", passed, failed)
    passed.append("4 a reason for asking Grok: required from the Content Agent per candidate")
    similar_to = None
    question_set = frozenset(keywords) | token_set("\n".join(i.title or "" for i in fresh))
    for prior in ctx.prior_grok_questions:
        if jaccard(question_set, token_set(prior)) >= ctx.policy.grok_similarity:
            similar_to = prior
            break
    _check(
        similar_to is None,
        f"5 differs from Grok questions of the last {ctx.policy.grok_dedup_days} days",
        passed,
        failed,
    )
    passed.append("6 only @grok, once: enforced on each candidate")
    return GrokEligibility(eligible=not failed, passed=passed, failed=failed, similar_to=similar_to)


def _check(ok: bool, label: str, passed: list[str], failed: list[str]) -> None:
    (passed if ok else failed).append(label)


def _one_grok_question(ranked: list[RankedOpportunity]) -> list[RankedOpportunity]:
    """Criterion 7: at most one Grok-eligible opportunity per session (the best ranked).
    A cap, not a quota: zero is the normal outcome."""
    seen = False
    out = []
    for opp in ranked:
        if opp.grok.eligible and seen:
            grok = GrokEligibility(
                eligible=False,
                passed=list(opp.grok.passed),
                failed=[
                    *opp.grok.failed,
                    "7 at most one Grok question per session (a higher-ranked one exists)",
                ],
                similar_to=opp.grok.similar_to,
            )
            intents = tuple(i for i in opp.intents if i is not ContentIntent.GROK_QUESTION)
            opp = replace(opp, grok=grok, intents=intents)
        elif opp.grok.eligible:
            seen = True
            opp = replace(
                opp,
                grok=GrokEligibility(
                    eligible=True,
                    passed=[*opp.grok.passed, "7 the only Grok question of this session"],
                    failed=[],
                ),
            )
        out.append(opp)
    return out


# --- audience ----------------------------------------------------------------------


@dataclass
class _Author:
    items: list[AnalysisItem] = field(default_factory=list)
    curated: bool = False


def build_audience(
    items: Sequence[AnalysisItem], ctx: AnalysisContext
) -> tuple[AudienceSuggestion, ...]:
    """Read-only shortlist: X authors seen in this session's results (curated handles
    always included) and discussion threads. Topic signals only; nothing about the person
    beyond their public posts here, and no profile data is fetched or stored."""
    authors: dict[str, _Author] = defaultdict(_Author)
    for item in items:
        if item.platform == "x" and item.author:
            entry = authors[item.author.lower()]
            entry.items.append(item)
            entry.curated = entry.curated or item.curated
    suggestions: list[AudienceSuggestion] = []
    for handle, entry in authors.items():
        suggestion = _account_suggestion(handle, entry, ctx)
        if suggestion is not None:
            suggestions.append(suggestion)
    for item in items:
        if item.category is SourceCategory.DISCUSSION and item.platform == "hn":
            suggestion = _thread_suggestion(item, ctx)
            if suggestion is not None:
                suggestions.append(suggestion)
    suggestions.sort(key=lambda s: (-s.score, s.ref))
    return tuple(suggestions[: ctx.policy.max_audience_entries])


def _account_suggestion(
    handle: str, entry: _Author, ctx: AnalysisContext
) -> AudienceSuggestion | None:
    posts = entry.items
    term_lists = [matched_terms(p.body, ctx.vocabulary) for p in posts]
    topics = list(dict.fromkeys(t for terms in term_lists for t in terms))
    if not topics and not entry.curated:
        return None
    relevance = sum(min(1.0, len(t) / 3) for t in term_lists) / len(posts)
    focus = sum(1 for t in term_lists if len(t) >= 2) / len(posts)
    questions = sum(1 for p in posts if ClaimHintKind.QUESTION in p.hints)
    convo = _conversation_ratio(posts)
    engagement = _sum_known([p.likes for p in posts] + [p.reposts for p in posts])
    pillars = [
        pl for pl in ctx.pillars if token_set(pl) & token_set("\n".join(p.body for p in posts))
    ]
    score = 0.5 * relevance + 0.2 * focus + 0.2 * min(1.0, len(posts) / 3) + 0.1 * (convo or 0.0)
    signals = (
        AudienceSignal(
            name="topic_relevance", value=round(relevance, 3), detail="vocabulary match per post"
        ),
        AudienceSignal(
            name="technical_focus",
            value=round(focus, 3),
            detail="share of posts with 2+ technical terms (expertise proxy)",
        ),
        AudienceSignal(
            name="recent_activity", value=len(posts), detail="posts in this session's results"
        ),
        AudienceSignal(
            name="conversation_quality",
            value=None if convo is None else round(convo, 3),
            detail="replies+quotes share of public engagement, when returned",
        ),
        AudienceSignal(
            name="public_engagement",
            value=engagement,
            detail="likes+reposts as returned (None if not returned)",
        ),
        AudienceSignal(
            name="recurring_questions", value=questions, detail="question-shaped posts (heuristic)"
        ),
        AudienceSignal(
            name="pillar_overlap", value=len(pillars), detail=", ".join(pillars) or "none"
        ),
    )
    about = ", ".join(topics[:4]) or "your pillars"
    explanation = (
        f"@{handle} posted {len(posts)} time(s) in these results about {about}"
        + (f", including {questions} question(s)" if questions else "")
        + ". A useful contribution would add primary evidence or a concrete example on "
        "those topics." + (" Curated account." if entry.curated else "")
    )
    return AudienceSuggestion(
        kind=AudienceKind.ACCOUNT,
        ref=f"@{handle}",
        platform="x",
        score=round(score, 4),
        signals=signals,
        matched_topics=tuple(topics),
        explanation=explanation,
        evidence_item_ids=tuple(p.id for p in posts),
        curated=entry.curated,
    )


def _thread_suggestion(item: AnalysisItem, ctx: AnalysisContext) -> AudienceSuggestion | None:
    topics = matched_terms(item.body, ctx.vocabulary)
    if not topics:
        return None
    comments = item.comments or 0
    questions = 1 if ClaimHintKind.QUESTION in item.hints else 0
    score = 0.5 * min(1.0, len(topics) / 3) + 0.3 * min(1.0, comments / 50) + 0.2 * questions
    signals = (
        AudienceSignal(
            name="topic_relevance",
            value=round(min(1.0, len(topics) / 3), 3),
            detail=", ".join(topics[:4]),
        ),
        AudienceSignal(name="comments", value=item.comments, detail="as returned"),
        AudienceSignal(name="points", value=item.points, detail="as returned"),
        AudienceSignal(name="asks_question", value=questions, detail="heuristic"),
    )
    return AudienceSuggestion(
        kind=AudienceKind.DISCUSSION,
        ref=f"https://news.ycombinator.com/item?id={item.provider_item_id}",
        platform=item.platform,
        score=round(score, 4),
        signals=signals,
        matched_topics=tuple(topics),
        explanation=(
            f"A {item.publisher} discussion on {', '.join(topics[:3])} with "
            f"{item.comments if item.comments is not None else 'an unknown number of'} comments. "
            "Read it before posting; reply only by hand, if at all."
        ),
        evidence_item_ids=(item.id,),
        curated=False,
    )


def _conversation_ratio(posts: Sequence[AnalysisItem]) -> float | None:
    convo = _sum_known([p.replies for p in posts] + [p.quotes for p in posts])
    total = _sum_known([v for p in posts for v in (p.likes, p.reposts, p.replies, p.quotes)])
    if convo is None or not total:
        return None
    return convo / total


def _sum_known(values: Iterable[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None
