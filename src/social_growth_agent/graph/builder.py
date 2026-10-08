"""Assembles the workflow graph.

    START -> retrieve --(route_after_retrieve)--> research -> generate -> critic -+
              ^  |  retry / broaden (bounded)                   ^              |
              +--+                                              +-- (retry) ---+
    critic --(route_after_critique)--> request_review -> human_review | generate | failed -> END

    human_review --(route_after_review)--> END (approve / reject)
                                       \\-> critique_edit -> request_review (edit)
                                       \\-> generate (regenerate with notes, bounded)

Provider-calling nodes (retrieve, research, generate, critic, critique_edit) retry
``TransientProviderError`` and route any provider failure left after retries to ``failed``.
``retrieve`` is the only node that talks to the social platform. It has no RetryPolicy:
it loops back to itself (retry after a transient error, or a deterministically broadened
query when the sample is thin) only while ``max_research_attempts`` and the provider's
``max_requests_per_run`` allow, with every attempt recorded in state. Rate limits are never
retried (``RateLimitedError`` is not transient) and nothing sleeps.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

from social_growth_agent.errors import TransientProviderError
from social_growth_agent.graph.checkpointing import in_memory_checkpointer
from social_growth_agent.graph.dependencies import AgentName, Dependencies
from social_growth_agent.graph.instrumentation import (
    OperationLabel,
    instrument,
    provider_error_handler,
)
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

    sink = deps.usage_sink
    ledger = deps.operation_ledger

    def label(provider: str, operation: str) -> OperationLabel | None:
        return None if ledger is None else OperationLabel(ledger, provider, operation)

    def llm(agent: AgentName) -> str:
        settings = deps.agent_settings.for_agent(agent)
        return f"{settings.provider}:{settings.model}"

    # retrieve has no RetryPolicy: it retries inside the graph loop, where every
    # attempt is recorded in state and counted against the request budget.
    for name, fn, policy, operation in (
        ("retrieve", nodes.retrieve, None, label(deps.research_provider.source_name, "search")),
        ("research", nodes.research, retry_policy, label(llm("research"), "llm")),
        ("generate", nodes.generate, retry_policy, label(llm("content"), "llm")),
        ("critic", nodes.critic, retry_policy, label(llm("critic"), "llm")),
        ("critique_edit", nodes.critique_edit, retry_policy, label(llm("critic"), "llm")),
    ):
        # LangGraph documents `(state, error: NodeError)` error handlers but its
        # `StateNode` type alias does not include that signature yet.
        graph.add_node(  # type: ignore[call-overload]
            name,
            instrument(name, fn, sink, operation),
            retry_policy=policy,
            error_handler=provider_error_handler(name),
        )
    graph.add_node("request_review", instrument("request_review", nodes.request_review, sink))
    graph.add_node("human_review", instrument("human_review", nodes.human_review, sink))
    graph.add_node("failed", instrument("failed", nodes.failed, sink))

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
