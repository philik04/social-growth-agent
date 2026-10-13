"""Deterministic validation of a model's strategy proposal (Phase 7).

The model's output is untrusted. A proposal is accepted only if *every* rule holds;
otherwise it fails with the list of broken rules. Nothing is repaired or downgraded on the
model's behalf.

- Only ``preferred_hooks`` and ``length_guidance`` may change (enum allowlist).
  ``current_value`` comes from the stored strategy, never from the model.
- Every evidence reference must be a comparison of *this* evaluation, along the dimension
  the change is about.
- An insufficient-evidence evaluation can only produce ``no_change``.
- ``medium`` confidence needs at least twice the minimum total sample.
- Every number in the free text must appear in the evaluation (rounding and percent
  forms allowed): the model cannot invent statistics.
- Free text is length-bounded. It is stored and shown as data, never executed.
- The resulting strategy must validate, and the length range stays within the platform
  limit. The content policy remains the only enforced limit at generation time.
"""

import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import JsonValue, ValidationError

from social_growth_agent.agents.strategy import ChangeDraft, StrategyProposalDraft
from social_growth_agent.analytics.evaluation import EvaluationConfig, canonical_json
from social_growth_agent.errors import ProposalValidationError
from social_growth_agent.models import ContentStrategy, HookType, LengthGuidance
from social_growth_agent.models.strategy import (
    FIELD_EVIDENCE,
    Comparison,
    Dimension,
    EvaluationStatus,
    EvaluationSummary,
    ProposableField,
    ProposalConfidence,
    ProposalOutcome,
    StrategyChange,
    ValidatedProposal,
)
from social_growth_agent.policies.content_policy import DEFAULT_MAX_POST_LENGTH

MAX_SUMMARY = 600
MAX_RATIONALE = 600
MAX_ITEM = 300
MAX_ITEMS = 8
MAX_REFS = 10
MAX_NOTE = 400
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def validate_proposal(
    draft: StrategyProposalDraft,
    *,
    summary: EvaluationSummary,
    config: EvaluationConfig,
    base: ContentStrategy,
) -> ValidatedProposal:
    reasons: list[str] = []
    evidence = {c.comparison_id: c for c in summary.comparisons}
    allowed_numbers = _number_forms(canonical_json(summary.model_dump(mode="json")))

    if draft.outcome is ProposalOutcome.CHANGE and not draft.changes:
        reasons.append("outcome 'change' without any change")
    if draft.outcome is ProposalOutcome.NO_CHANGE and draft.changes:
        reasons.append("outcome 'no_change' must not list changes")
    if summary.status is EvaluationStatus.INSUFFICIENT_EVIDENCE and (
        draft.outcome is not ProposalOutcome.NO_CHANGE or draft.posting_time_note
    ):
        reasons.append("insufficient evidence allows only 'no_change' without notes")
    _text(reasons, "summary", draft.summary, MAX_SUMMARY)

    seen: set[ProposableField] = set()
    changes: list[StrategyChange] = []
    updates: dict[str, JsonValue] = {}
    texts = [draft.summary]
    for i, change in enumerate(draft.changes):
        label = f"changes[{i}]"
        if change.field in seen:
            reasons.append(f"{label}: field {change.field.value} changed twice")
        seen.add(change.field)
        _refs(reasons, label, change.evidence_refs, evidence, FIELD_EVIDENCE[change.field])
        if (
            change.confidence is ProposalConfidence.MEDIUM
            and summary.eligible_count < 2 * config.min_total_n
        ):
            reasons.append(
                f"{label}: medium confidence needs at least twice the minimum total sample"
            )
        _text(reasons, f"{label}.rationale", change.rationale, MAX_RATIONALE)
        _items(reasons, f"{label}.limitations", change.limitations)
        _items(reasons, f"{label}.confounders", change.confounders)
        texts += [change.rationale, *change.limitations, *change.confounders]
        current, proposed = _values(reasons, label, change, base)
        if proposed is None:
            continue
        allowed_numbers |= _number_forms(canonical_json(proposed))
        updates[change.field.value] = proposed
        changes.append(
            StrategyChange(
                field=change.field,
                current_value=current,
                proposed_value=proposed,
                evidence_refs=list(change.evidence_refs),
                expected_metric=change.expected_metric,
                expected_direction=change.expected_direction,
                hypothesis=(
                    f"hypothesis: {change.expected_metric.value} may "
                    f"{change.expected_direction.value} (not a prediction)"
                ),
                confidence=change.confidence,
                rationale=change.rationale,
                limitations=list(change.limitations),
                confounders=list(change.confounders),
            )
        )

    note = draft.posting_time_note
    if note is None:
        if draft.posting_time_evidence_refs:
            reasons.append("posting_time_evidence_refs without a posting_time_note")
    else:
        _text(reasons, "posting_time_note", note, MAX_NOTE)
        _refs(
            reasons,
            "posting_time_note",
            draft.posting_time_evidence_refs,
            evidence,
            Dimension.HOUR_BLOCK_UTC,
        )
        texts.append(note)

    for text in texts:
        invented = sorted({t for t in _NUMBER.findall(text) if not _known(t, allowed_numbers)})
        if invented:
            reasons.append(f"numbers not found in the evaluation: {', '.join(invented[:5])}")

    proposed_strategy = None
    if not reasons and updates:
        try:
            proposed_strategy = ContentStrategy.model_validate(
                {**base.model_dump(mode="json"), **updates}
            )
        except ValidationError as exc:
            reasons.append(f"resulting strategy is invalid: {exc.error_count()} error(s)")
    if reasons:
        raise ProposalValidationError(reasons)
    return ValidatedProposal(
        outcome=draft.outcome,
        summary=draft.summary,
        changes=changes,
        posting_time_note=note,
        posting_time_evidence_refs=list(draft.posting_time_evidence_refs),
        proposed_strategy=proposed_strategy,
    )


def changed_fields(base: ContentStrategy, proposed: ContentStrategy) -> set[str]:
    """Fields whose values differ, ignoring the identity fields set at approval."""
    a = base.model_dump(mode="json")
    b = proposed.model_dump(mode="json")
    return {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)} - {"id", "version"}


ALLOWED_FIELD_NAMES = frozenset(f.value for f in ProposableField)


def _values(
    reasons: list[str], label: str, change: ChangeDraft, base: ContentStrategy
) -> tuple[JsonValue, JsonValue | None]:
    if change.field is ProposableField.PREFERRED_HOOKS:
        current: JsonValue = [h.value for h in base.preferred_hooks]
        hooks = change.proposed_hooks
        if hooks is None or change.proposed_length is not None:
            reasons.append(f"{label}: preferred_hooks needs proposed_hooks and nothing else")
            return current, None
        if not 1 <= len(hooks) <= len(HookType) or len(set(hooks)) != len(hooks):
            reasons.append(f"{label}: proposed_hooks must be 1-{len(HookType)} distinct hooks")
            return current, None
        proposed: JsonValue = [h.value for h in hooks]
        if proposed == current:
            reasons.append(f"{label}: proposed_hooks equals the current value")
            return current, None
        return current, proposed
    current = base.length_guidance.model_dump(mode="json") if base.length_guidance else None
    length = change.proposed_length
    if length is None or change.proposed_hooks is not None:
        reasons.append(f"{label}: length_guidance needs proposed_length and nothing else")
        return current, None
    if not 1 <= length.min_chars <= length.max_chars <= DEFAULT_MAX_POST_LENGTH:
        reasons.append(
            f"{label}: length range must satisfy 1 <= min <= max <= {DEFAULT_MAX_POST_LENGTH}"
        )
        return current, None
    proposed = LengthGuidance(min_chars=length.min_chars, max_chars=length.max_chars).model_dump(
        mode="json"
    )
    if proposed == current:
        reasons.append(f"{label}: proposed_length equals the current value")
        return current, None
    return current, proposed


def _refs(
    reasons: list[str],
    label: str,
    refs: list[str],
    evidence: dict[str, Comparison],
    dimension: Dimension,
) -> None:
    if not 1 <= len(refs) <= MAX_REFS or len(set(refs)) != len(refs):
        reasons.append(f"{label}: needs 1-{MAX_REFS} distinct evidence references")
    for ref in refs:
        comparison = evidence.get(ref)
        if comparison is None:
            reasons.append(f"{label}: unknown evidence reference {ref[:80]!r}")
        elif comparison.dimension is not dimension:
            reasons.append(f"{label}: {ref[:80]!r} is not a {dimension.value} comparison")


def _text(reasons: list[str], label: str, text: str, limit: int) -> None:
    if not text.strip():
        reasons.append(f"{label} is empty")
    elif len(text) > limit:
        reasons.append(f"{label} is longer than {limit} characters")


def _items(reasons: list[str], label: str, items: list[str]) -> None:
    if not 1 <= len(items) <= MAX_ITEMS:
        reasons.append(f"{label} needs 1-{MAX_ITEMS} entries")
    for item in items:
        _text(reasons, label, item, MAX_ITEM)


def _number_forms(text: str) -> set[Decimal]:
    """Every number in ``text`` plus its rounded and percent forms."""
    forms: set[Decimal] = {Decimal(0), Decimal(1), Decimal(2)}
    for token in _NUMBER.findall(text):
        x = Decimal(token)
        forms.add(x.normalize())
        for scaled, places in ((x, range(5)), (x * 100, range(3))):
            for k in places:
                quantum = Decimal(1).scaleb(-k)
                for mode in (ROUND_HALF_EVEN, ROUND_HALF_UP):
                    forms.add(scaled.quantize(quantum, rounding=mode).normalize())
    return forms


def _known(token: str, forms: set[Decimal]) -> bool:
    try:
        return Decimal(token).normalize() in forms
    except InvalidOperation:
        return False
