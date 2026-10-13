"""Evidence semantics: item verification status, claim hints and finding validation.

Two layers, both deterministic:

1. Before any model call, every item gets a *verification status* from its source alone
   (``primary_source`` only for primary evidence from an official or research publisher;
   everything else ``unverified``) and surface *claim hints* (a question mark, "I think",
   "will"...). Hints are labelled ``heuristic``; they help ranking and Grok eligibility,
   and are never shown as facts.
2. After the Research Agent labels each finding with a ``ClaimKind``, the label is checked
   against the cited items. A ``reported_fact`` without a cited primary source becomes an
   ``unverified_claim``; ``disputed`` needs cited items on at least two sides. Claim-level
   ``corroborated`` is assigned only when two primary sources from different publishers
   contain the same specific anchors (numbers, versions) the finding states; without
   anchors, corroboration cannot be established and the status stays ``primary_source``.

Popularity never raises verification: engagement counts are not read here at all.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from social_growth_agent.discovery.text import anchors_in
from social_growth_agent.models.discovery import (
    PRIMARY_EVIDENCE,
    ClaimHintKind,
    ClaimKind,
    CredibilityTier,
    EvidenceType,
    SourceCategory,
    VerificationStatus,
)

_TRUSTED_PUBLISHERS = frozenset({CredibilityTier.OFFICIAL, CredibilityTier.PRIMARY_RESEARCH})

_QUESTION_START = re.compile(
    r"^(ask hn:|why|how|should|can|could|is|are|does|do|will|what|when|where|which|who)\b",
    re.IGNORECASE,
)
_OPINION = re.compile(
    r"\b(i think|i believe|imo|imho|in my (opinion|view|experience)|hot take|unpopular "
    r"opinion|i'd argue|i would argue|overrated|underrated)\b",
    re.IGNORECASE,
)
_PREDICTION = re.compile(
    r"\b(will (replace|become|be|kill|make|change)|by 20\d\d|in (the next|five|ten|\d+) "
    r"years|predict|prediction|forecast|is going to|are going to)\b",
    re.IGNORECASE,
)
_DISAGREEMENT = re.compile(
    r"\b(disagree|not true|isn't true|is wrong|are wrong|misleading|overhyped|debunk|"
    r"contrary to|push back|pushback|skeptic|sceptic|doesn't hold|does not hold|"
    r"counterpoint|actually,)\b",
    re.IGNORECASE,
)
_ANNOUNCEMENT = re.compile(
    r"\b(announc|introduc|launch|releas|now available|general availability|ga\b|v\d)",
    re.IGNORECASE,
)


def item_verification(
    category: SourceCategory, evidence: EvidenceType, tier: CredibilityTier
) -> VerificationStatus:
    """What an item alone establishes about the claims it makes."""
    if (
        category is SourceCategory.PRIMARY
        and evidence in PRIMARY_EVIDENCE
        and tier in _TRUSTED_PUBLISHERS
    ):
        return VerificationStatus.PRIMARY_SOURCE
    return VerificationStatus.UNVERIFIED


def claim_hints(title: str | None, text: str, category: SourceCategory) -> list[ClaimHintKind]:
    """Surface hints only (labelled heuristic wherever they are shown)."""
    body = f"{title or ''}\n{text}".strip()
    hints: list[ClaimHintKind] = []
    first = (title or text).strip()
    if "?" in body or _QUESTION_START.match(first):
        hints.append(ClaimHintKind.QUESTION)
    if _OPINION.search(body):
        hints.append(ClaimHintKind.OPINION)
    if _PREDICTION.search(body):
        hints.append(ClaimHintKind.PREDICTION)
    if _DISAGREEMENT.search(body):
        hints.append(ClaimHintKind.DISAGREEMENT)
    if category is SourceCategory.PRIMARY and _ANNOUNCEMENT.search(body):
        hints.append(ClaimHintKind.ANNOUNCEMENT)
    return hints


@dataclass(frozen=True)
class EvidenceFacts:
    """What validation needs to know about one cited item."""

    source_id: str
    text: str
    publisher: str
    verification: VerificationStatus
    category: SourceCategory


@dataclass(frozen=True)
class ValidatedClaim:
    kind: ClaimKind
    verification: VerificationStatus
    notes: tuple[str, ...]


def validate_claim(
    proposed: ClaimKind, finding_text: str, cited: Sequence[EvidenceFacts]
) -> ValidatedClaim:
    """Downgrade a model-proposed claim kind to what the cited evidence supports."""
    notes: list[str] = []
    primaries = [e for e in cited if e.verification is not VerificationStatus.UNVERIFIED]
    kind = proposed
    if proposed is ClaimKind.REPORTED_FACT and not primaries:
        kind = ClaimKind.UNVERIFIED_CLAIM
        notes.append("reported_fact without a cited primary source; kept as unverified_claim")
    if proposed is ClaimKind.DISPUTED and len({e.source_id for e in cited}) < 2:
        kind = ClaimKind.OPEN_QUESTION
        notes.append("disputed needs at least two cited items; kept as open_question")
    return ValidatedClaim(
        kind=kind, verification=claim_verification(finding_text, cited), notes=tuple(notes)
    )


def claim_verification(finding_text: str, cited: Sequence[EvidenceFacts]) -> VerificationStatus:
    """``corroborated`` only when the same specific anchors appear in two primary sources
    from different publishers. Credibility alone never corroborates anything."""
    primaries = [e for e in cited if e.verification is not VerificationStatus.UNVERIFIED]
    if not primaries:
        return VerificationStatus.UNVERIFIED
    anchors = anchors_in(finding_text)
    if not anchors:
        # Nothing specific to check: the finding cites a primary source, no more.
        return VerificationStatus.PRIMARY_SOURCE
    supporting = [e for e in primaries if anchors <= anchors_in(e.text)]
    if len({e.publisher.lower() for e in supporting}) >= 2:
        return VerificationStatus.CORROBORATED
    if supporting:
        return VerificationStatus.PRIMARY_SOURCE
    # The specific numbers/versions stated are not in any cited primary source.
    return VerificationStatus.UNVERIFIED
