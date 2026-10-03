"""Opt-in live test against the OpenAI API. Never runs in the normal suite.

RUN_LIVE_LLM_TESTS=1 OPENAI_API_KEY=... uv run pytest -m live
"""

import os

import pytest

from social_growth_agent.config import AppSettings
from social_growth_agent.models import LLMCallOutcome, RunStatus
from social_growth_agent.services import WorkflowService
from social_growth_agent.services.demo import demo_account
from social_growth_agent.services.factory import build_dependencies

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_LLM_TESTS") != "1" or not os.environ.get("OPENAI_API_KEY"),
        reason="set RUN_LIVE_LLM_TESTS=1 and OPENAI_API_KEY to run live LLM tests",
    ),
]


def test_live_run_reaches_review_or_fails_explicitly():
    deps = build_dependencies(AppSettings(llm_provider="openai"))
    account, strategy = demo_account()
    result = WorkflowService(deps).start_run(account, strategy)

    assert result.status in (RunStatus.AWAITING_REVIEW, RunStatus.FAILED)
    assert result.state.research_brief is not None
    assert result.state.llm_calls[0].outcome is LLMCallOutcome.SUCCESS
    if result.status is RunStatus.AWAITING_REVIEW:
        for cand in result.pending_review.candidates:
            assert len(cand.content) <= 280
            assert result.state.latest_critique(cand.id).passed
    else:
        assert result.state.errors
