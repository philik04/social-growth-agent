"""Assembles the workflow graph.

    START -> retrieve -> research -> generate -> critic --(route_after_critique)--+
                                       ^                    |                     |
                                       +----- (retry) ------+   request_review <--+--> failed -> END
                                                                -> human_review

    human_review --(route_after_review)--> END (approve / reject / regenerate)
                                       \\-> critique_edit -> request_review (edit)

Provider-calling nodes (retrieve, research, generate, critic, critique_edit) retry
``TransientProviderError`` and route any provider failure left after retries to ``failed``.
``retrieve`` is the only node that talks to the social platform; its attempts are capped
at the provider's ``max_requests_per_run`` so retries can never exceed the request budget.
Rate limits are never retried (``RateLimitedError`` is not transient).
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

from social_growth_agent.errors import TransientProviderError
from social_growth_agent.graph.checkpointing import in_memory_checkpointer
from social_growth_agent.graph.dependencies import Dependencies
from social_growth_agent.graph.instrumentation import instrument, provider_error_handler
from social_growth_agent.graph.nodes import WorkflowNodes
from social_growth_agent.graph.routing import (
    route_after_critique,
    route_after_edit_critique,
    route_after_generate,
    route_after_research,
    route_after_retrieve,
    route_after_review,
)
from social_growth_agent.graph.state import GraphState

type WorkflowGraph = CompiledStateGraph[GraphState, None, GraphState, GraphState]

DEFAULT_RETRY_POLICY = RetryPolicy(
    max_attempts=3, initial_interval=0.5, retry_on=TransientProviderError
)


def retrieval_retry_policy(base: RetryPolicy, max_requests_per_run: int | None) -> RetryPolicy:
    """Total retrieval attempts (first try plus retries) never exceed the request budget."""
    if max_requests_per_run is None:
        return base
    return base._replace(max_attempts=max(1, min(base.max_attempts, max_requests_per_run)))


def build_graph(
    deps: Dependencies,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> WorkflowGraph:
    """Build and compile the graph.

    A checkpointer is required for human review (``interrupt``). The in-memory
    default is replaced by a Postgres saver in Phase 4.
    """
    nodes = WorkflowNodes(deps)
    graph = StateGraph(GraphState)

    retrieve_policy = retrieval_retry_policy(
        retry_policy, deps.research_provider.max_requests_per_run
    )
    for name, fn, policy in (
        ("retrieve", nodes.retrieve, retrieve_policy),
        ("research", nodes.research, retry_policy),
        ("generate", nodes.generate, retry_policy),
        ("critic", nodes.critic, retry_policy),
        ("critique_edit", nodes.critique_edit, retry_policy),
    ):
        # LangGraph documents `(state, error: NodeError)` error handlers but its
        # `StateNode` type alias does not include that signature yet.
        graph.add_node(  # type: ignore[call-overload]
            name,
            instrument(name, fn),
            retry_policy=policy,
            error_handler=provider_error_handler(name),
        )
    graph.add_node("request_review", instrument("request_review", nodes.request_review))
    graph.add_node("human_review", instrument("human_review", nodes.human_review))
    graph.add_node("failed", instrument("failed", nodes.failed))

    graph.add_edge(START, "retrieve")
    graph.add_conditional_edges("retrieve", route_after_retrieve)
    graph.add_conditional_edges("research", route_after_research)
    graph.add_conditional_edges("generate", route_after_generate)
    graph.add_conditional_edges("critic", route_after_critique)
    graph.add_edge("request_review", "human_review")
    graph.add_conditional_edges("human_review", route_after_review)
    graph.add_conditional_edges("critique_edit", route_after_edit_critique)
    graph.add_edge("failed", END)

    return graph.compile(checkpointer=checkpointer or in_memory_checkpointer())
