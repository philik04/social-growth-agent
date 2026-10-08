"""Phase 4 restart demo: a run survives its process being killed mid-run.

    DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.restart_demo

Offline and free: mock research fixtures and the deterministic fake agents. Three real
OS processes share one PostgreSQL database:

1. ``crash``: starts a run; retrieval completes and is checkpointed, then the process is
   killed (``os._exit``) in the middle of the research LLM call.
2. ``resume``: a fresh process starts up. Startup only flags the run ``stalled`` (no
   provider is called), then an explicit resume continues from the checkpoint. The
   retrieval ledger proves the completed retrieval was not repeated.
3. ``approve``: another fresh process approves a candidate; no provider is called.

Use a scratch database: the demo runs ``sga-db upgrade`` on it.
"""

import os
import subprocess
import sys

from pydantic import BaseModel
from sqlalchemy import func, select

from social_growth_agent.agents import ResearchReport
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import ReviewAction, ReviewDecision, SearchResult
from social_growth_agent.models.research import ResearchQuery
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.persistence.tables import ResearchFetchRow, RunEventRow
from social_growth_agent.providers import LLMProvider, LLMRequest, LLMResponse
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.services.demo import demo_account
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime


class CountingResearch(MockResearchProvider):
    """Mock retrieval that announces every call, so a repeated fetch would be visible."""

    def search(self, query: ResearchQuery) -> SearchResult:
        print(f"    [pid {os.getpid()}] RETRIEVAL CALLED: {query.text!r}")
        return super().search(query)


class DieDuringResearch:
    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        if schema is ResearchReport:
            print(f"    [pid {os.getpid()}] research LLM call started... killing the process")
            sys.stdout.flush()
            os._exit(137)
        return self._inner.generate_structured(request, schema)


class CountingLLM:
    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        self.calls.append(request.agent)
        return self._inner.generate_structured(request, schema)


def runtime(llm: LLMProvider) -> Runtime:
    settings = AppSettings(llm_provider="fake", research_provider="mock")
    deps = Dependencies(llm=llm, research_provider=CountingResearch())
    return build_runtime(settings, deps=deps, executor=InlineExecutor())


def ledger(rt: Runtime, run_id: str) -> tuple[int, list[str]]:
    with rt.db.transaction() as session:
        fetches = session.scalar(select(func.count()).where(ResearchFetchRow.run_id == run_id))
        nodes = list(
            session.scalars(
                select(RunEventRow.node)
                .where(RunEventRow.run_id == run_id)
                .order_by(RunEventRow.started_at)
            )
        )
    return int(fetches or 0), nodes


def crash() -> None:
    rt = runtime(DieDuringResearch(build_fake_llm()))
    account, strategy = demo_account()
    rt.service.create_run(account, strategy)


def resume(run_id: str) -> None:
    llm = CountingLLM(build_fake_llm())
    rt = runtime(llm)
    stalled = rt.service.recover_stalled()
    print(
        f"    [pid {os.getpid()}] startup flagged stalled: {stalled}; LLM calls so far: {llm.calls}"
    )
    before, nodes = ledger(rt, run_id)
    print(f"    before resume: retrievals in ledger={before} nodes={nodes}")
    status = rt.service.resume(run_id).status
    after, nodes = ledger(rt, run_id)
    print(f"    after resume:  status={status} retrievals in ledger={after}")
    print(f"    LLM calls in this process: {llm.calls}")
    print(f"    node path: {' -> '.join(nodes)}")
    repeated = after != before
    print("    RESULT: " + ("RETRIEVAL REPEATED" if repeated else "retrieval was NOT repeated"))
    rt.close()
    if repeated:
        sys.exit(1)


def approve(run_id: str) -> None:
    llm = CountingLLM(build_fake_llm())
    rt = runtime(llm)
    [pending] = [p for p in rt.service.pending_reviews() if p.run_id == run_id]
    candidate = pending.candidates[0]
    print(f"    [pid {os.getpid()}] approving {candidate.id}: {candidate.content}")
    decision = ReviewDecision(
        action=ReviewAction.APPROVE, candidate_id=candidate.id, reviewer="restart-demo"
    )
    status = rt.service.submit_review(pending.run_id, decision).status
    usage = rt.service.usage(pending.run_id)
    print(f"    status={status} LLM calls in this process={llm.calls}")
    research = usage.usage.research[0]
    print(
        f"    usage: retrievals={research.fetches} post_reads={research.post_reads} "
        f"llm_calls={sum(u.calls for u in usage.usage.llm)} "
        f"estimated_total={usage.at_run_pricing.total} {usage.at_run_pricing.pricing.currency} "
        f"(pricing {usage.at_run_pricing.pricing.id})"
    )
    rt.close()


def main(argv: list[str]) -> int:
    if argv[:1] == ["crash"]:
        crash()
        return 0
    if len(argv) == 2 and argv[0] in ("resume", "approve"):
        (resume if argv[0] == "resume" else approve)(argv[1])
        return 0

    url = AppSettings().database_url
    if url is None:
        print("Set DATABASE_URL to a scratch PostgreSQL database (see docs/DATABASE.md).")
        return 2
    upgrade(url.get_secret_value())
    rt = runtime(build_fake_llm())
    known = {r.id for r in rt.service.list_runs(limit=200)}

    print("== process 1: start a run, die during the research LLM call")
    code = _child("crash")
    [run_id] = [r.id for r in rt.service.list_runs(limit=200) if r.id not in known]
    print(f"   exit code {code}; run {run_id} left as {rt.service.get_summary(run_id).status}")
    rt.close()
    for phase in ("resume", "approve"):
        print(f"\n== process {2 if phase == 'resume' else 3}: {phase}")
        code = _child(phase, run_id)
        if code != 0:
            return code
    return 0


def _child(*args: str) -> int:
    return subprocess.call(
        [sys.executable, "-m", "social_growth_agent.services.restart_demo", *args]
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
