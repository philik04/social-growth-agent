"""Label agreement metrics must use final verdicts and distinguish absent classes."""

import json

import pytest

from social_growth_agent.agents import CriticAgent, CriticReport, IssueDraft
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.evaluation import CriticEvalCase, CriticEvalReport, evaluate_critic
from social_growth_agent.models import CritiqueVerdict, IssueCategory, PostFormat
from social_growth_agent.policies import ContentPolicy
from social_growth_agent.providers.mocks import ScriptedLLMProvider
from tests.conftest import evaluation
from tests.test_agents import candidate

P, R, X = CritiqueVerdict


def test_asymmetric_confusion_matrix_metrics_use_candidate_ids(strategy):
    # Rows are expected, columns are actual. Unequal row/column totals catch
    # precision/recall transposition; both non-PASS rows contribute false PASSes.
    matrix = ((3, 2, 1), (4, 5, 2), (1, 3, 6))
    cases = []
    evaluations = []
    expected_confusion = {}
    for expected, row in zip(CritiqueVerdict, matrix, strict=True):
        for actual, count in zip(CritiqueVerdict, row, strict=True):
            expected_confusion[f"{expected}->{actual}"] = count
            for _ in range(count):
                post = candidate("A clear engineering observation.", f"case_{len(cases)}")
                cases.append(CriticEvalCase(candidate=post, expected=expected))
                evaluations.append(evaluation(post.id, actual))
    llm = ScriptedLLMProvider(
        {CriticReport: lambda _r: CriticReport(evaluations=list(reversed(evaluations)))}
    )

    report = evaluate_critic(CriticAgent(llm), strategy, cases)

    assert (report.total, report.correct, report.accuracy) == (27, 14, 14 / 27)
    assert report.confusion == expected_confusion
    assert report.false_pass_count == 5
    for verdict, tp, fp, fn, precision, recall in [
        (P, 3, 5, 3, 3 / 8, 3 / 6),
        (R, 5, 5, 6, 5 / 10, 5 / 11),
        (X, 6, 3, 4, 6 / 9, 6 / 10),
    ]:
        metrics = report.per_verdict[verdict]
        assert (metrics.true_positives, metrics.false_positives, metrics.false_negatives) == (
            tp,
            fp,
            fn,
        )
        assert metrics.precision == pytest.approx(precision)
        assert metrics.recall == pytest.approx(recall)
    assert len(llm.calls) == 1


@pytest.mark.parametrize("verdict", list(CritiqueVerdict))
def test_perfect_single_class_and_absent_labels(strategy, verdict):
    post = candidate("A clear engineering observation.")
    llm = ScriptedLLMProvider(
        {CriticReport: lambda _r: CriticReport(evaluations=[evaluation(post.id, verdict)])}
    )
    report = evaluate_critic(
        CriticAgent(llm), strategy, [CriticEvalCase(candidate=post, expected=verdict)]
    )

    assert report.accuracy == 1.0
    assert report.false_pass_count == 0
    for label, metrics in report.per_verdict.items():
        if label == verdict:
            assert metrics.true_positives == 1
            assert metrics.precision == metrics.recall == 1.0
        else:
            assert metrics.true_positives == 0
            assert metrics.precision is None and metrics.recall is None
        assert metrics.false_positives == metrics.false_negatives == 0


@pytest.mark.parametrize(
    ("expected", "actual"), [(e, a) for e in CritiqueVerdict for a in CritiqueVerdict if e != a]
)
def test_defined_zero_differs_from_undefined_and_false_pass_is_directional(
    strategy, expected, actual
):
    post = candidate("A clear engineering observation.")
    llm = ScriptedLLMProvider(
        {CriticReport: lambda _r: CriticReport(evaluations=[evaluation(post.id, actual)])}
    )
    report = evaluate_critic(
        CriticAgent(llm), strategy, [CriticEvalCase(candidate=post, expected=expected)]
    )

    assert report.correct == 0 and report.accuracy == 0.0
    assert report.false_pass_count == int(actual is P)
    assert report.per_verdict[actual].precision == 0.0
    assert report.per_verdict[actual].recall is None
    assert report.per_verdict[expected].precision is None
    assert report.per_verdict[expected].recall == 0.0


def test_empty_report_serializes_undefined_rates_as_null(strategy):
    report = evaluate_critic(CriticAgent(build_fake_llm()), strategy, [])

    assert (report.total, report.correct, report.confusion, report.accuracy) == (0, 0, {}, 0.0)
    assert report.false_pass_count == 0
    payload = json.loads(report.model_dump_json())
    assert set(payload["per_verdict"]) == {"pass", "revise", "reject"}
    for metrics in payload["per_verdict"].values():
        assert metrics == {
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": 0,
            "precision": None,
            "recall": None,
        }


def test_legacy_report_inputs_and_fields_remain_compatible():
    legacy = {"total": 2, "correct": 1, "confusion": {"pass->pass": 1, "reject->pass": 1}}
    report = CriticEvalReport.model_validate(legacy)

    assert report.model_dump(include=set(legacy)) == legacy
    assert report.accuracy == 0.5
    assert report.false_pass_count == 1
    payload = json.loads(report.model_dump_json())
    assert set(payload) == {*legacy, "per_verdict", "false_pass_count"}
    assert payload["per_verdict"]["pass"]["precision"] == 0.5
    assert payload["per_verdict"]["revise"]["precision"] is None
    # New derived values cannot override the source confusion counts on input.
    payload["false_pass_count"] = 999
    payload["per_verdict"] = {}
    restored = CriticEvalReport.model_validate_json(json.dumps(payload))
    assert restored.model_dump() == report.model_dump()


@pytest.mark.parametrize("kind", ["length", "format", "blank", "mention", "clarity"])
def test_report_scores_final_policy_and_clarity_vetoes(strategy, kind):
    post = candidate("A clear observation.")
    if kind == "length":
        post = candidate("x" * 21)
    elif kind == "format":
        post = candidate("A thread", fmt=PostFormat.THREAD)
    elif kind == "blank":
        post = candidate("   ")
    elif kind == "mention":
        post = candidate("Ask \uff20grok")  # Full-width mention must also be vetoed.
    assessment = evaluation(post.id, P, score=0.99)
    if kind == "clarity":
        assessment = assessment.model_copy(
            update={"issues": [IssueDraft(category=IssueCategory.UNCLEAR, detail="No main point")]}
        )
    llm = ScriptedLLMProvider({CriticReport: lambda _r: CriticReport(evaluations=[assessment])})

    report = evaluate_critic(
        CriticAgent(llm),
        strategy,
        [CriticEvalCase(candidate=post, expected=R)],
        policy=ContentPolicy(max_post_length=20),
    )

    assert report.confusion == {"revise->revise": 1}
    assert report.false_pass_count == 0
    assert report.per_verdict[R].recall == 1.0
    assert report.per_verdict[P].precision is None
