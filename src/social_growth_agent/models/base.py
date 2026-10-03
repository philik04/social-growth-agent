"""Shared base class and helpers for domain models."""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, strict base for domain objects.

    Frozen so that objects stored in graph state cannot be mutated in place;
    nodes must return new objects, which keeps checkpoints trustworthy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


def new_id(prefix: str) -> str:
    """Return a prefixed unique id such as ``cand_3f2a...``."""
    return f"{prefix}_{uuid4().hex[:12]}"


def utc_now() -> datetime:
    return datetime.now(UTC)
