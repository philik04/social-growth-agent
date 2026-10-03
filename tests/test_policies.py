import pytest

from social_growth_agent.errors import InvalidReviewError
from social_growth_agent.models import CandidateOrigin, CritiqueVerdict, PolicyViolation, PostFormat
from social_growth_agent.policies import (
    ContentPolicy,
    CriticGate,
    edited_candidate,
    final_verdict,
    find_violations,
    is_material_edit,
)
from tests.conftest import make_critique
from tests.test_agents import candidate

P, R, X = CritiqueVerdict.PASS, CritiqueVerdict.REVISE, CritiqueVerdict.REJECT
V = PolicyViolation(rule="r", detail="d")


def test_default_policy_is_a_standard_x_post():
    policy = ContentPolicy()
    assert policy.max_post_length == 280
    assert find_violations(candidate("x" * 280), policy) == []
    assert [v.rule for v in find_violations(candidate("x" * 281), policy)] == ["max_post_length"]


def test_blank_and_format_rules():
    policy = ContentPolicy()
    assert [v.rule for v in find_violations(candidate("   "), policy)] == ["non_blank_content"]
    thread = candidate("ok", fmt=PostFormat.THREAD)
    assert [v.rule for v in find_violations(thread, policy)] == ["allowed_format"]
    allow_threads = ContentPolicy(allowed_formats=frozenset(PostFormat))
    assert find_violations(thread, allow_threads) == []


@pytest.mark.parametrize(
    ("recommended", "violations", "expected"),
    [(P, [], P), (P, [V], R), (R, [V], R), (X, [V], X), (X, [], X)],
)
def test_final_verdict_never_passes_a_violation(recommended, violations, expected):
    assert final_verdict(recommended, violations) is expected


def test_gate_orders_reviewable_candidates_by_score():
    critiques = [
        make_critique("a", 1, P, 0.6),
        make_critique("b", 1, R),
        make_critique("c", 1, P, 0.9),
    ]
    gate = CriticGate()
    assert [k.candidate_id for k in gate.reviewable(critiques)] == ["c", "a"]
    assert gate.is_open(critiques)
    assert not gate.is_open([make_critique("b", 1, R)])


def test_material_edit_detection():
    assert not is_material_edit("Hello  world", " Hello world\n")
    assert is_material_edit("Hello world", "Hello, world")


def test_edited_candidate_is_a_new_traceable_candidate():
    original = candidate("original text", "cand_a")
    edited = edited_candidate(original, "better text")
    assert edited.id != original.id
    assert edited.origin is CandidateOrigin.HUMAN_EDIT
    assert edited.revises_candidate_id == "cand_a"
    assert edited.research_finding_ids == original.research_finding_ids
    with pytest.raises(InvalidReviewError):
        edited_candidate(original, "original   text")
