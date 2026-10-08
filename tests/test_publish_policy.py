"""The deterministic publish policy, the idempotency key and the schedule window."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from social_growth_agent.models import (
    ContentCandidate,
    CritiqueVerdict,
    HookType,
    PostFormat,
    PublicationStatus,
    RunStatus,
)
from social_growth_agent.policies import (
    ContentPolicy,
    PublishFacts,
    ScheduleWindow,
    content_sha256,
    idempotency_key,
    is_due,
    publish_refusals,
    schedule_refusal,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def candidate(content: str = "A grounded post about agent evals.") -> ContentCandidate:
    return ContentCandidate(
        id="cand_1",
        run_id="run_1",
        generation_attempt=1,
        strategy_id="strat_1",
        strategy_version=1,
        research_finding_ids=["find_1"],
        topic="agent evals",
        hook_type=HookType.NUMBER,
        format=PostFormat.SINGLE,
        target_audience="engineers",
        content=content,
    )


def facts(**overrides) -> PublishFacts:
    cand = overrides.pop("candidate", None) or candidate()
    base = PublishFacts(
        run_status=RunStatus.APPROVED,
        candidate=cand,
        approved_candidate_id=cand.id,
        approval_count=1,
        review_candidate_ids=(cand.id,),
        latest_critique_verdict=CritiqueVerdict.PASS,
        content_policy=ContentPolicy(),
    )
    return replace(base, **overrides) if overrides else base


def test_an_approved_candidate_with_a_passing_critique_is_publishable():
    assert publish_refusals(facts()) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"run_status": RunStatus.AWAITING_REVIEW}, "not approved"),
        ({"approved_candidate_id": None, "approval_count": 0}, "0 approve decisions"),
        ({"approved_candidate_id": "cand_other"}, "not the approved candidate"),
        ({"review_candidate_ids": ("cand_other",)}, "not part of the review request"),
        ({"latest_critique_verdict": CritiqueVerdict.REVISE}, "not pass"),
        ({"latest_critique_verdict": None}, "missing"),
        ({"candidate": candidate("x" * 281)}, "max_post_length"),
        ({"candidate": candidate("   ")}, "non_blank_content"),
        ({"existing_publication_status": PublicationStatus.PUBLISHED}, "already exists"),
        ({"existing_publication_status": PublicationStatus.FAILED}, "already exists"),
    ],
)
def test_every_guarantee_is_refused_on_its_own(overrides, expected):
    refusals = publish_refusals(facts(**overrides))
    assert any(expected in r for r in refusals), refusals


def test_a_decision_without_a_review_snapshot_is_not_checked_against_it():
    """Decisions recorded before Phase 5 carry no snapshot; they were validated against
    the pending review when they were made, so the policy does not refuse them."""
    assert publish_refusals(facts(review_candidate_ids=())) == []


def test_a_cancelled_publication_does_not_block_a_new_request():
    assert publish_refusals(facts(existing_publication_status=PublicationStatus.CANCELLED)) == []


def test_two_approvals_are_refused_even_if_the_latest_matches():
    refusals = publish_refusals(facts(approval_count=2))
    assert any("exactly one required" in r for r in refusals)


# --- idempotency key -------------------------------------------------------------------


def test_the_key_is_stable_and_scoped_to_platform_run_and_candidate():
    key = idempotency_key("x", "run_1", "cand_1")
    assert key == idempotency_key("x", "run_1", "cand_1")
    assert len(key) == 64
    assert key != idempotency_key("x", "run_1", "cand_2")
    assert key != idempotency_key("x", "run_2", "cand_1")
    assert key != idempotency_key("mastodon", "run_1", "cand_1")


def test_content_hash_detects_a_changed_candidate():
    assert content_sha256("a") != content_sha256("a ")


# --- scheduling ------------------------------------------------------------------------


def test_a_naive_datetime_is_refused():
    assert schedule_refusal(NOW.replace(tzinfo=None), NOW, ScheduleWindow()) == (
        "scheduled_for must include a timezone"
    )


def test_bounds_are_the_past_tolerance_and_thirty_days():
    window = ScheduleWindow()
    assert schedule_refusal(None, NOW, window) is None
    assert schedule_refusal(NOW - timedelta(minutes=4), NOW, window) is None
    assert "in the past" in str(schedule_refusal(NOW - timedelta(minutes=6), NOW, window))
    assert schedule_refusal(NOW + timedelta(days=30), NOW, window) is None
    assert "30 days ahead" in str(schedule_refusal(NOW + timedelta(days=31), NOW, window))


def test_absent_or_slightly_past_schedules_mean_publish_now():
    assert is_due(None, NOW) is True
    assert is_due(NOW - timedelta(minutes=4), NOW) is True
    assert is_due(NOW + timedelta(minutes=1), NOW) is False


def test_a_window_can_be_configured_per_deployment():
    window = ScheduleWindow(max_ahead=timedelta(days=1), past_tolerance=timedelta(0))
    assert "1 days ahead" in str(schedule_refusal(NOW + timedelta(days=2), NOW, window))
    assert schedule_refusal(NOW - timedelta(seconds=1), NOW, window) is not None
