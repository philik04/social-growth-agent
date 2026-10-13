"""Experiment treatments (Phase 8): how an arm constrains a run, and how well the published
text appears to follow a declared hook.

A treatment becomes part of the run's ``ContentPolicy``, so the existing hard-rule
machinery enforces it on every generation attempt, regeneration, human edit and again at
publish time. ``hook_adherence`` is a deliberately modest heuristic: it can say a text
looks consistent or inconsistent with a declared hook, or that it cannot tell. It never
verifies a hook, and it never blocks anything.
"""

import re

from social_growth_agent.models import HookType
from social_growth_agent.models.experiment import HookAdherence, Treatment
from social_growth_agent.policies.content_policy import ContentPolicy

_STEP = re.compile(r"(^|\n)\s*(\d+[.)]|step\s+\d|-\s)", re.IGNORECASE)


def apply_treatment(policy: ContentPolicy, treatment: Treatment) -> ContentPolicy:
    """The run's policy with the arm's hard constraint. A length arm narrows the range;
    it can never exceed the policy's own maximum."""
    if treatment.hook_type is not None:
        return policy.model_copy(update={"required_hook_type": treatment.hook_type})
    assert treatment.length is not None
    if treatment.length.max_chars > policy.max_post_length:
        raise ValueError(
            f"length arm maximum {treatment.length.max_chars} exceeds the content policy "
            f"maximum of {policy.max_post_length}"
        )
    return ContentPolicy.model_validate(
        {
            **policy.model_dump(),
            "max_post_length": treatment.length.max_chars,
            "min_post_length": treatment.length.min_chars,
        }
    )


def hook_adherence(content: str, hook: HookType) -> HookAdherence:
    """Heuristic only. ``question`` and ``number`` can be checked roughly from surface
    features; ``how_to`` can only look consistent; ``story`` and ``contrarian`` cannot be
    checked at all without judgement."""
    text = content.strip()
    opening = text[:140]
    if hook is HookType.QUESTION:
        if "?" in opening:
            return HookAdherence.HEURISTIC_CONSISTENT
        return HookAdherence.NOT_CHECKABLE if "?" in text else HookAdherence.HEURISTIC_INCONSISTENT
    if hook is HookType.NUMBER:
        if any(c.isdigit() for c in text[:100]):
            return HookAdherence.HEURISTIC_CONSISTENT
        return (
            HookAdherence.NOT_CHECKABLE
            if any(c.isdigit() for c in text)
            else HookAdherence.HEURISTIC_INCONSISTENT
        )
    if hook is HookType.HOW_TO:
        lowered = text.lower()
        if lowered.startswith("how") or "how to" in lowered or _STEP.search(text):
            return HookAdherence.HEURISTIC_CONSISTENT
        return HookAdherence.NOT_CHECKABLE
    return HookAdherence.NOT_CHECKABLE
