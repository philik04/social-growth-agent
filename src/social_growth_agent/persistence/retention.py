"""Deletion and retention of third-party content (Phase 9, D10).

X's developer terms require removing content that was deleted on X, and this project keeps
X text for a limited time (``DISCOVERY_X_RETENTION_DAYS``). A purge removes the text from
every place it was copied to, in one transaction:

- ``discovery_items``: text, title and author become NULL (ids, URL, hash and timestamps
  stay as audit metadata; a trigger forbids restoring them);
- ``source_posts``: the per-run copy used as research evidence (text replaced by a
  marker, username removed);
- LangGraph checkpoints (``checkpoint_blobs`` and ``checkpoint_writes``): every stored
  ``SourcePost`` with that id is rewritten in place, so a resumed or replayed run cannot
  bring the text back;
- ``content_purges``: a tombstone that stops the same item from being stored again by a
  later discovery session and from being replayed into a run.

Text a model already derived from the item (findings, candidates) is the application's
own output and is not rewritten; it never contains more than the model chose to quote.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel
from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from social_growth_agent.graph.checkpointing import checkpoint_types
from social_growth_agent.graph.state import GraphState
from social_growth_agent.models import SourcePost, new_id, utc_now
from social_growth_agent.models.discovery import PurgeReason, PurgeResult, SourcePlatform
from social_growth_agent.persistence import discovery as ddb
from social_growth_agent.persistence.tables import (
    ContentPurgeRow,
    DiscoveryItemRow,
    OpportunityEvidenceRow,
    RunRow,
    SourcePostRow,
)

PURGED_TEXT = "[removed: deleted on the platform or past retention]"
MAX_SWEEP = 500


@dataclass(frozen=True)
class Actor:
    name: str
    auth_method: str


SYSTEM = Actor(name="retention", auth_method="system")


def purge(
    session: Session,
    *,
    platform: SourcePlatform,
    provider_item_ids: Sequence[str],
    reason: PurgeReason,
    actor: Actor,
    now: datetime | None = None,
) -> PurgeResult:
    now = now or utc_now()
    ids = list(dict.fromkeys(i.strip() for i in provider_item_ids if i.strip()))
    items = ddb.item_ids_for(session, platform, ids)
    item_ids = [r.id for r in items]
    if item_ids:
        session.execute(
            update(DiscoveryItemRow)
            .where(DiscoveryItemRow.id.in_(item_ids))
            .where(DiscoveryItemRow.purged_at.is_(None))
            .values(text=None, title=None, author=None, purged_at=now, purge_reason=reason.value)
        )
    source_ids = set(item_ids)
    if platform == "x":
        source_ids |= {f"x_{i}" for i in ids}
    posts = _redact_source_posts(session, source_ids)
    runs = _affected_runs(session, source_ids, item_ids)
    blobs, writes = redact_checkpoints(session, runs, source_ids)
    tombstones = 0
    for provider_item_id in ids:
        inserted = session.execute(
            insert(ContentPurgeRow)
            .values(
                id=new_id("purge"),
                platform=platform,
                provider_item_id=provider_item_id,
                reason=reason.value,
                requested_by=actor.name,
                auth_method=actor.auth_method,
                counts={
                    "discovery_items": sum(
                        1 for r in items if r.provider_item_id == provider_item_id
                    ),
                    "runs": len(runs),
                },
                created_at=now,
            )
            .on_conflict_do_nothing()
            .returning(ContentPurgeRow.id)
        ).first()
        tombstones += inserted is not None
    return PurgeResult(
        platform=platform,
        provider_item_ids=ids,
        reason=reason,
        discovery_items=len(item_ids),
        source_posts=posts,
        checkpoint_blobs=blobs,
        checkpoint_writes=writes,
        tombstones=tombstones,
    )


def expired_x_ids(session: Session, *, retention_days: int, now: datetime) -> list[str]:
    """X post ids whose text is older than the retention window, wherever it is stored:
    discovery items (by first retrieval) and research evidence of runs (by retrieval)."""
    before = now - timedelta(days=retention_days)
    ids = [pid for _, pid in ddb.expired_x_items(session, before, MAX_SWEEP)]
    stale_posts = session.scalars(
        select(SourcePostRow.source_id)
        .where(SourcePostRow.platform == "x")
        .where(SourcePostRow.retrieved_at < before)
        .where(SourcePostRow.text != PURGED_TEXT)
        .distinct()
        .limit(MAX_SWEEP)
    )
    ids += [sid.removeprefix("x_") for sid in stale_posts if sid.startswith("x_")]
    return list(dict.fromkeys(ids))[:MAX_SWEEP]


def _redact_source_posts(session: Session, source_ids: Iterable[str]) -> int:
    ids = sorted(source_ids)
    if not ids:
        return 0
    result = session.execute(
        update(SourcePostRow)
        .where(SourcePostRow.source_id.in_(ids))
        .where(or_(SourcePostRow.text != PURGED_TEXT, SourcePostRow.author_username.is_not(None)))
        .values(text=PURGED_TEXT, author_username=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _affected_runs(session: Session, source_ids: set[str], item_ids: list[str]) -> list[str]:
    runs: set[str] = set()
    if source_ids:
        runs |= set(
            session.scalars(
                select(SourcePostRow.run_id).where(SourcePostRow.source_id.in_(sorted(source_ids)))
            )
        )
    if item_ids:
        runs |= set(
            session.scalars(
                select(RunRow.id)
                .join(
                    OpportunityEvidenceRow,
                    OpportunityEvidenceRow.opportunity_id == RunRow.opportunity_id,
                )
                .where(OpportunityEvidenceRow.item_id.in_(item_ids))
            )
        )
    return sorted(runs)


def redact_checkpoints(
    session: Session, run_ids: Sequence[str], source_ids: set[str]
) -> tuple[int, int]:
    """Rewrite stored checkpoint values of these runs (threads) so that no ``SourcePost``
    with one of ``source_ids`` keeps its text. Returns (blobs, writes) rewritten."""
    if not run_ids or not source_ids:
        return 0, 0
    # The run's own allowlist plus GraphState (the graph's input write), so every value
    # round-trips with its type; a blob is decoded only if it mentions a purged id.
    serde = JsonPlusSerializer(allowed_msgpack_modules=[*checkpoint_types(), GraphState])
    needles = [s.encode("utf-8") for s in source_ids]
    blobs = session.execute(
        text(
            "SELECT thread_id, checkpoint_ns, channel, version, type, blob "
            "FROM checkpoint_blobs WHERE thread_id = ANY(:threads) AND blob IS NOT NULL"
        ),
        {"threads": list(run_ids)},
    ).all()
    changed_blobs = 0
    for thread_id, ns, channel, version, kind, blob in blobs:
        if not any(n in bytes(blob) for n in needles):
            continue
        value = serde.loads_typed((kind, bytes(blob)))
        redacted, changed = _redact(value, source_ids)
        if not changed:
            continue
        new_kind, new_blob = serde.dumps_typed(redacted)
        session.execute(
            text(
                "UPDATE checkpoint_blobs SET type = :type, blob = :blob WHERE thread_id = :t "
                "AND checkpoint_ns = :ns AND channel = :channel AND version = :version"
            ),
            {
                "type": new_kind,
                "blob": new_blob,
                "t": thread_id,
                "ns": ns,
                "channel": channel,
                "version": version,
            },
        )
        changed_blobs += 1
    writes = session.execute(
        text(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, type, blob "
            "FROM checkpoint_writes WHERE thread_id = ANY(:threads) AND blob IS NOT NULL"
        ),
        {"threads": list(run_ids)},
    ).all()
    changed_writes = 0
    for thread_id, ns, checkpoint_id, task_id, idx, kind, blob in writes:
        if not any(n in bytes(blob) for n in needles):
            continue
        value = serde.loads_typed((kind, bytes(blob)))
        redacted, changed = _redact(value, source_ids)
        if not changed:
            continue
        new_kind, new_blob = serde.dumps_typed(redacted)
        session.execute(
            text(
                "UPDATE checkpoint_writes SET type = :type, blob = :blob WHERE thread_id = :t "
                "AND checkpoint_ns = :ns AND checkpoint_id = :cid AND task_id = :task "
                "AND idx = :idx"
            ),
            {
                "type": new_kind,
                "blob": new_blob,
                "t": thread_id,
                "ns": ns,
                "cid": checkpoint_id,
                "task": task_id,
                "idx": idx,
            },
        )
        changed_writes += 1
    return changed_blobs, changed_writes


def _redact(value: Any, source_ids: set[str]) -> tuple[Any, bool]:
    if isinstance(value, SourcePost):
        if value.source_id not in source_ids or (
            value.text == PURGED_TEXT and value.author_username is None and value.title is None
        ):
            return value, False
        return value.model_copy(
            update={"text": PURGED_TEXT, "author_username": None, "title": None}
        ), True
    if isinstance(value, list):
        out = [_redact(v, source_ids) for v in value]
        return [v for v, _ in out], any(c for _, c in out)
    if isinstance(value, tuple):
        out = [_redact(v, source_ids) for v in value]
        return tuple(v for v, _ in out), any(c for _, c in out)
    if isinstance(value, dict):
        pairs = {k: _redact(v, source_ids) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, any(c for _, c in pairs.values())
    if isinstance(value, BaseModel):
        updates = {}
        for name in type(value).model_fields:
            new, changed = _redact(getattr(value, name), source_ids)
            if changed:
                updates[name] = new
        if updates:
            return value.model_copy(update=updates), True
    return value, False
