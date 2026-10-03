"""Checkpoint serialization with an explicit type allowlist.

LangGraph's default serializer will deserialize any importable class found in a
checkpoint (with a warning). Since checkpoints will live in a database in Phase 4,
we restrict deserialization to our own domain types up front.
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from social_growth_agent import models
from social_growth_agent.graph.state import RunConfig
from social_growth_agent.policies import ContentPolicy, CriticGate


def checkpoint_types() -> list[type]:
    exported = (getattr(models, name) for name in models.__all__)
    return [obj for obj in exported if isinstance(obj, type)] + [
        RunConfig,
        ContentPolicy,
        CriticGate,
    ]


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_types())


def in_memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=checkpoint_serializer())
