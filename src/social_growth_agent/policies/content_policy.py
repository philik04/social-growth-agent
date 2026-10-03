"""Deterministic content policy: hard rules that application code enforces.

The Critic's LLM may recommend a verdict, but these rules have the final word.
Adding a rule means writing one small ``HardRule`` and adding it to ``DEFAULT_RULES``.
"""

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from social_growth_agent.models import (
    ContentCandidate,
    CritiqueVerdict,
    PolicyViolation,
    PostFormat,
)

DEFAULT_MAX_POST_LENGTH = 280


class ContentPolicy(BaseModel):
    """Per-run configurable limits. Defaults describe a standard X post."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_post_length: int = Field(default=DEFAULT_MAX_POST_LENGTH, ge=1)
    allowed_formats: frozenset[PostFormat] = frozenset({PostFormat.SINGLE})

    def prompt_payload(self) -> dict[str, JsonValue]:
        """The constraints as shown to agents (enforcement stays in code)."""
        formats: list[JsonValue] = [f.value for f in sorted(self.allowed_formats)]
        return {"max_post_length": self.max_post_length, "allowed_formats": formats}


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


DEFAULT_RULES: tuple[HardRule, ...] = (NonBlankContentRule(), MaxLengthRule(), AllowedFormatRule())


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
