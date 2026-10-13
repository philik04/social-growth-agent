"""Deterministic content policy: hard rules that application code enforces.

The Critic's LLM may recommend a verdict, but these rules have the final word.
Adding a rule means writing one small ``HardRule`` and adding it to ``DEFAULT_RULES``.
"""

from collections.abc import Sequence
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from social_growth_agent.discovery.text import jaccard, token_set
from social_growth_agent.models import (
    ContentCandidate,
    ContentIntent,
    CritiqueVerdict,
    HookType,
    PolicyViolation,
    PostFormat,
)
from social_growth_agent.policies.grounding import (
    EvidenceGrounding,
    unsupported_numbers,
    unsupported_urls,
)
from social_growth_agent.policies.mentions import GROK_HANDLE, find_mentions

DEFAULT_MAX_POST_LENGTH = 280


class ContentPolicy(BaseModel):
    """Per-run configurable limits. Defaults describe a standard X post."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_post_length: int = Field(default=DEFAULT_MAX_POST_LENGTH, ge=1)
    allowed_formats: frozenset[PostFormat] = frozenset({PostFormat.SINGLE})
    # Experiment treatments (Phase 8). Unset outside experiments, so runs and stored
    # checkpoints of Phases 1-7 are unchanged.
    min_post_length: int | None = Field(default=None, ge=1)
    required_hook_type: HookType | None = Field(
        default=None,
        description="Every candidate must DECLARE this hook type. The rule checks the "
        "declared label only; it cannot verify that the text really uses the hook.",
    )
    # Discovery runs (Phase 9). Unset for every other run, so earlier runs and their
    # checkpoints are unchanged.
    content_intent: ContentIntent | None = None
    grounding: EvidenceGrounding | None = Field(
        default=None,
        description="Links and numbers a post may use: those found in the run's evidence.",
    )
    recent_grok_questions: tuple[str, ...] = Field(
        default=(), description="Grok questions drafted recently (no near-repeats)."
    )
    grok_similarity: float = Field(default=0.5, gt=0, le=1)

    @model_validator(mode="after")
    def _length_range(self) -> Self:
        if self.min_post_length is not None and self.min_post_length > self.max_post_length:
            raise ValueError("min_post_length must not exceed max_post_length")
        return self

    def prompt_payload(self) -> dict[str, JsonValue]:
        """The constraints as shown to agents (enforcement stays in code)."""
        formats: list[JsonValue] = [f.value for f in sorted(self.allowed_formats)]
        payload: dict[str, JsonValue] = {
            "max_post_length": self.max_post_length,
            "allowed_formats": formats,
        }
        if self.min_post_length is not None:
            payload["min_post_length"] = self.min_post_length
        if self.required_hook_type is not None:
            payload["required_hook_type"] = self.required_hook_type.value
        if self.content_intent is not None:
            payload["content_intent"] = self.content_intent.value
            payload["mentions"] = (
                "exactly one @grok, nothing else"
                if self.content_intent is ContentIntent.GROK_QUESTION
                else "none"
            )
        if self.grounding is not None:
            links: list[JsonValue] = list(sorted(self.grounding.allowed_urls))
            payload["allowed_links"] = links
            payload["numbers"] = "only numbers that appear in the cited evidence"
        return payload


class HardRule(Protocol):
    name: str

    def check(
        self, candidate: ContentCandidate, policy: ContentPolicy
    ) -> PolicyViolation | None: ...


class MaxLengthRule:
    name = "max_post_length"

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        length = len(candidate.content)
        if length <= policy.max_post_length:
            return None
        return PolicyViolation(
            rule=self.name,
            detail=f"{length} characters exceeds the limit of {policy.max_post_length}",
        )


class AllowedFormatRule:
    name = "allowed_format"

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        if candidate.format in policy.allowed_formats:
            return None
        allowed = ", ".join(sorted(policy.allowed_formats))
        return PolicyViolation(
            rule=self.name, detail=f"format '{candidate.format}' is not allowed ({allowed})"
        )


class NonBlankContentRule:
    """Guards against malformed content (whitespace only) slipping through."""

    name = "non_blank_content"

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        if candidate.content.strip():
            return None
        return PolicyViolation(rule=self.name, detail="content is blank")


class MinLengthRule:
    """Experiment length arms (Phase 8): no-op unless ``min_post_length`` is set."""

    name = "min_post_length"

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        minimum = policy.min_post_length
        length = len(candidate.content)
        if minimum is None or length >= minimum:
            return None
        return PolicyViolation(
            rule=self.name, detail=f"{length} characters is below the minimum of {minimum}"
        )


class RequiredHookRule:
    """Experiment hook arms (Phase 8): the candidate's declared hook type must match.
    This enforces the label, not the writing: whether the text really uses the hook is
    only estimated later (``policies.treatment.hook_adherence``)."""

    name = "required_hook_type"

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        required = policy.required_hook_type
        if required is None or candidate.hook_type == required:
            return None
        return PolicyViolation(
            rule=self.name,
            detail=f"declared hook '{candidate.hook_type}' but this run requires '{required}'",
        )


class MentionRule:
    """Phase 9: no @mentions, except exactly one ``@grok`` in a ``grok_question`` run.

    Applies to every run, generated and human-edited content alike. Even an allowed
    ``@grok`` post is manual-only: publishing refuses any mention (``policies.mentions``).
    """

    name = "mentions"

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        mentions = find_mentions(candidate.content)
        if policy.content_intent is not ContentIntent.GROK_QUESTION:
            if not mentions:
                return None
            return PolicyViolation(
                rule=self.name,
                detail=f"mentions are not allowed here: {', '.join('@' + m for m in mentions)}",
            )
        if mentions != [GROK_HANDLE]:
            shown = ", ".join("@" + m for m in mentions) or "none"
            return PolicyViolation(
                rule=self.name,
                detail="a Grok question mentions exactly one account, @grok, once "
                f"(found: {shown})",
            )
        return None


class GrokQuestionRule:
    """Phase 9 Grok criteria checked per candidate: it asks something, says why asking
    Grok adds value (criterion 4) and is not a near-repeat of a recent Grok question
    (criterion 5). No-op for every other intent."""

    name = "grok_question"
    MIN_RATIONALE = 20

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        if policy.content_intent is not ContentIntent.GROK_QUESTION:
            return None
        if "?" not in candidate.content:
            return PolicyViolation(rule=self.name, detail="a Grok question must ask a question")
        if len(candidate.rationale.strip()) < self.MIN_RATIONALE:
            return PolicyViolation(
                rule=self.name, detail="missing the reason why asking Grok adds value"
            )
        mine = token_set(candidate.content)
        for earlier in policy.recent_grok_questions:
            overlap = jaccard(mine, token_set(earlier))
            if overlap >= policy.grok_similarity:
                return PolicyViolation(
                    rule=self.name,
                    detail=f"too similar to a recent Grok question ({overlap:.2f})",
                )
        return None


class GroundingRule:
    """Phase 9 discovery runs: links and numbers must come from the run's evidence."""

    name = "evidence_grounding"

    def check(self, candidate: ContentCandidate, policy: ContentPolicy) -> PolicyViolation | None:
        grounding = policy.grounding
        if grounding is None:
            return None
        urls = unsupported_urls(candidate.content, grounding)
        numbers = unsupported_numbers(candidate.content, grounding)
        if not urls and not numbers:
            return None
        parts = []
        if urls:
            parts.append(f"links not in the evidence: {', '.join(urls)}")
        if numbers:
            parts.append(f"numbers not in the evidence: {', '.join(numbers)}")
        return PolicyViolation(rule=self.name, detail="; ".join(parts))


DEFAULT_RULES: tuple[HardRule, ...] = (
    NonBlankContentRule(),
    MaxLengthRule(),
    AllowedFormatRule(),
    MinLengthRule(),
    RequiredHookRule(),
    MentionRule(),
    GrokQuestionRule(),
    GroundingRule(),
)


def find_violations(
    candidate: ContentCandidate,
    policy: ContentPolicy,
    rules: Sequence[HardRule] = DEFAULT_RULES,
) -> list[PolicyViolation]:
    return [v for rule in rules if (v := rule.check(candidate, policy)) is not None]


def final_verdict(
    recommended: CritiqueVerdict, violations: Sequence[PolicyViolation]
) -> CritiqueVerdict:
    """Hard violations downgrade PASS to REVISE (fixable by regeneration); REJECT stays REJECT."""
    if violations and recommended is CritiqueVerdict.PASS:
        return CritiqueVerdict.REVISE
    return recommended
