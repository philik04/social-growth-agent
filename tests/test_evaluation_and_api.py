from fastapi.testclient import TestClient

from social_growth_agent.agents import CriticAgent
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.api import create_app
from social_growth_agent.evaluation import CriticEvalCase, evaluate_critic
from social_growth_agent.models import CritiqueVerdict
from tests.test_agents import candidate


def test_critic_eval_reports_accuracy_and_confusion(strategy):
    cases = [
        CriticEvalCase(
            candidate=candidate("Measured: 40% faster.", "a"), expected=CritiqueVerdict.PASS
        ),
        CriticEvalCase(
            candidate=candidate("100% guaranteed growth", "b"), expected=CritiqueVerdict.REVISE
        ),
        CriticEvalCase(
            candidate=candidate("Plain but fine.", "c"), expected=CritiqueVerdict.REJECT
        ),
        CriticEvalCase(candidate=candidate("z" * 300, "d"), expected=CritiqueVerdict.REVISE),
    ]
    report = evaluate_critic(CriticAgent(build_fake_llm()), strategy, cases)
    assert report.total == 4
    assert report.correct == 3
    assert report.confusion["reject->pass"] == 1


def test_health_endpoint():
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
