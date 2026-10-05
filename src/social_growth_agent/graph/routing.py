"""Pure routing functions. Each depends only on state, so routing is unit-testable.

The LLM never chooses the next node: routing reads validated state only, and the
critic gate (``RunConfig.critic_gate``) is the single owner of the pass decision.
"""

from typing import Literal

from social_growth_agent.graph.state import GraphState
from social_growth_agent.models import ReviewAction, RunStatus

AfterRetrieve = Literal["research", "failed"]
AfterResearch = Literal["generate", "failed"]
AfterGenerate = Literal["critic", "failed"]
AfterCritique = Literal["request_review", "generate", "failed"]
AfterReview = Literal["critique_edit", "__end__"]
AfterEditCritique = Literal["request_review", "failed"]


def route_after_retrieve(state: GraphState) -> AfterRetrieve:
    return "failed" if state.status is RunStatus.FAILED else "research"


def route_after_research(state: GraphState) -> AfterResearch:
    return "failed" if state.status is RunStatus.FAILED else "generate"


def route_after_generate(state: GraphState) -> AfterGenerate:
    return "failed" if state.status is RunStatus.FAILED else "critic"


def route_after_critique(state: GraphState) -> AfterCritique:
    """Open gate -> review; otherwise retry while attempts remain, else fail."""
    if state.status is RunStatus.FAILED:
        return "failed"
    if state.critic_gate_open():
        return "request_review"
    if state.generation_attempts < state.config.max_generation_attempts:
        return "generate"
    return "failed"


def route_after_review(state: GraphState) -> AfterReview:
    """A human edit must be critiqued again before it can be approved."""
    decision = state.review.decision
    if decision is not None and decision.action is ReviewAction.EDIT:
        return "critique_edit"
    return "__end__"  # langgraph.graph.END


def route_after_edit_critique(state: GraphState) -> AfterEditCritique:
    return "failed" if state.status is RunStatus.FAILED else "request_review"
