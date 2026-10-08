"""Domain model -> table row conversion, and the idempotent insert they all use."""

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from social_growth_agent.models import (
    ContentCandidate,
    Critique,
    LLMCall,
    NodeEvent,
    ResearchBrief,
    ResearchFetch,
    ResearchFinding,
    ReviewDecision,
    RunError,
    SourcePost,
)
from social_growth_agent.persistence.tables import Base

type Row = dict[str, Any]


def insert_ignore(session: Session, table: type[Base], rows: list[Row]) -> None:
    """Insert rows, skipping any whose primary key already exists (records are immutable)."""
    if rows:
        session.execute(insert(table).values(rows).on_conflict_do_nothing())


def research_fetch_row(run_id: str, f: ResearchFetch) -> Row:
    return {"run_id": run_id, **f.model_dump()}


def llm_call_row(run_id: str, c: LLMCall) -> Row:
    data = c.model_dump(exclude={"usage"})
    return {
        "run_id": run_id,
        **data,
        "input_tokens": c.usage.input_tokens if c.usage else None,
        "output_tokens": c.usage.output_tokens if c.usage else None,
    }


def source_post_row(run_id: str, p: SourcePost) -> Row:
    data = p.model_dump(exclude={"created_at"})
    return {"run_id": run_id, **data, "post_created_at": p.created_at}


def brief_row(run_id: str, b: ResearchBrief) -> Row:
    return {
        "id": b.id,
        "run_id": run_id,
        "topic": b.topic,
        "summary": b.summary,
        "confidence": b.confidence.value,
        "limitations": list(b.limitations),
        "opportunities": [o.model_dump(mode="json") for o in b.opportunities],
        "source": b.source.model_dump(mode="json"),
    }


def finding_rows(
    run_id: str, findings: list[ResearchFinding], brief_id: str | None
) -> tuple[list[Row], list[Row]]:
    rows, evidence = [], []
    for position, f in enumerate(findings):
        rows.append(
            {
                "id": f.id,
                "run_id": run_id,
                "brief_id": brief_id,
                "theme": f.theme,
                "summary": f.summary,
                "claim_type": f.claim_type.value,
                "signal_strength": f.signal_strength,
                "position": position,
            }
        )
        evidence += [
            {"finding_id": f.id, "source_id": sid, "run_id": run_id, "position": i}
            for i, sid in enumerate(dict.fromkeys(f.evidence_source_ids))
        ]
    return rows, evidence


def candidate_rows(run_id: str, candidates: list[ContentCandidate]) -> tuple[list[Row], list[Row]]:
    rows, links = [], []
    for position, c in enumerate(candidates):
        rows.append(
            {
                **c.model_dump(exclude={"research_finding_ids"}),
                "run_id": run_id,
                "position": position,
            }
        )
        links += [
            {"candidate_id": c.id, "finding_id": fid}
            for fid in dict.fromkeys(c.research_finding_ids)
        ]
    return rows, links


def critique_row(run_id: str, position: int, k: Critique) -> Row:
    return {
        "id": k.id,
        "run_id": run_id,
        "candidate_id": k.candidate_id,
        "generation_attempt": k.generation_attempt,
        "verdict": k.verdict.value,
        "recommended_verdict": k.recommended_verdict.value,
        "score": k.assessment.score,
        "factual_risk": k.assessment.factual_risk.value,
        "originality_risk": k.assessment.originality_risk.value,
        "tone_match": k.assessment.tone_match.value,
        "issues": [i.model_dump(mode="json") for i in k.issues],
        "policy_violations": [v.model_dump(mode="json") for v in k.policy_violations],
        "suggested_revision": k.suggested_revision,
        "position": position,
    }


def decision_row(run_id: str, d: ReviewDecision) -> Row:
    return {"run_id": run_id, **d.model_dump()}


def event_row(run_id: str, e: NodeEvent) -> Row:
    return {"run_id": run_id, **e.model_dump()}


def error_row(run_id: str, e: RunError) -> Row:
    return {"run_id": run_id, **e.model_dump()}
