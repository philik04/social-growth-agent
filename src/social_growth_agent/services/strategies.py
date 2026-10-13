"""Strategy versions, proposals, decisions and activation (Phase 7).

Every state change here is an explicit request; nothing is triggered by a read, a timer
or another state change:

- **Register** a human-written strategy as a version.
- **Request a proposal** from a saved evaluation (opt-in: ``STRATEGY_PROPOSER``). The
  proposal row is committed as ``generating`` before the model is called, then finalized
  as ``pending_review`` (validated) or ``failed``. One open proposal per account.
- **Retry** a failed proposal, or one stuck in ``generating`` past the stale threshold
  (its process most likely died). The old row is closed and a new one is linked to it.
- **Approve** (all-or-nothing) creates a new immutable version. It does **not** activate
  it, publish anything or start a run.
- **Reject** records the decision and changes nothing else.
- **Activate** a version (compare-and-set against the currently active one).

Approval, rejection, activation and registration need review authentication, checked by
the API before these methods are called; the method is recorded with each decision.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from social_growth_agent.accounting import PriceList
from social_growth_agent.agents.evidence_digest import digest_summary
from social_growth_agent.agents.strategy import PROMPT_VERSION, ProposalContext, StrategyAgent
from social_growth_agent.analytics.evaluation import EvaluationConfig, sha256_json
from social_growth_agent.errors import (
    AgentOutputError,
    DigestMismatchError,
    NoActiveStrategyError,
    ProposalsDisabledError,
    ProposalStateError,
    ProposalValidationError,
    ProviderError,
    StrategyConflictError,
    StrategyVersionNotFoundError,
)
from social_growth_agent.models import ContentStrategy, LLMCall, new_id, utc_now
from social_growth_agent.models.strategy import (
    DecisionKind,
    EvaluationStatus,
    EvaluationSummary,
    ProposalOutcome,
    ProposalStatus,
    Proposer,
    ReviewAuthMethod,
    StrategyVersionOrigin,
    ValidatedProposal,
)
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import evaluations as edb
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence import strategies as sdb
from social_growth_agent.persistence.tables import (
    RunRow,
    StrategyActivationRow,
    StrategyProposalDecisionRow,
    StrategyProposalRow,
)
from social_growth_agent.persistence.views import (
    ActiveStrategyView,
    ProposalView,
    StrategyHistoryView,
    StrategyVersionView,
)
from social_growth_agent.policies.strategy_changes import (
    ALLOWED_FIELD_NAMES,
    changed_fields,
    validate_proposal,
)

logger = logging.getLogger(__name__)

INSUFFICIENT_SUMMARY = "Insufficient evidence: no change recommended."


@dataclass(frozen=True)
class RegisterResult:
    created: bool
    version: StrategyVersionView


@dataclass(frozen=True)
class ImportReport:
    registered: list[str]
    already_registered: list[str]
    conflicts: list[str]


class StrategyService:
    def __init__(
        self,
        db: Database,
        *,
        prices: PriceList,
        agent: StrategyAgent | None,
        proposer: Proposer | None,
        stale_seconds: float,
    ) -> None:
        self._db = db
        self._prices = prices
        self._agent = agent
        self._proposer = proposer
        self._stale_seconds = stale_seconds

    # --- versions and activation ------------------------------------------------------

    def register_version(
        self,
        strategy: ContentStrategy,
        *,
        reviewer: str,
        auth: ReviewAuthMethod,
        note: str | None,
        origin: StrategyVersionOrigin = StrategyVersionOrigin.REGISTERED,
    ) -> RegisterResult:
        """Idempotent: registering the same body again returns the existing version."""
        try:
            with self._db.transaction() as session:
                owner = sdb.account_of_strategy(session, strategy.id)
                if owner is not None and owner != strategy.account_id:
                    raise StrategyConflictError(
                        f"strategy {strategy.id} belongs to another account"
                    )
                existing = sdb.find_version(session, strategy.id, strategy.version)
                if existing is not None:
                    content = strategy.model_dump(mode="json")
                    if existing.content_sha256 != sdb.strategy_sha256(content):
                        raise StrategyConflictError(
                            f"strategy {strategy.id} v{strategy.version} is already registered "
                            "with a different body"
                        )
                    return RegisterResult(False, self._version_view(session, existing.id))
                row = sdb.insert_version(
                    session,
                    version_id=new_id("sv"),
                    strategy=strategy,
                    origin=origin,
                    parent_version_id=None,
                    created_by=reviewer,
                    auth_method=auth.value,
                    note=note,
                )
                return RegisterResult(True, self._version_view(session, row.id))
        except IntegrityError:
            raise StrategyConflictError(
                f"strategy {strategy.id} v{strategy.version} was registered concurrently"
            ) from None

    def import_from_runs(self, *, dry_run: bool) -> ImportReport:
        """Register the distinct strategy bodies stored in ``runs`` (origin
        ``imported_from_run``). An (id, version) used with several bodies is reported as a
        conflict and never guessed. Nothing is activated."""
        registered: list[str] = []
        already: list[str] = []
        conflicts: list[str] = []
        with self._db.transaction() as session:
            bodies: dict[tuple[str, int], dict[str, ContentStrategy]] = {}
            raws: list[dict[str, Any]] = list(
                session.scalars(select(RunRow.strategy).order_by(RunRow.created_at))
            )
            for raw in raws:
                strategy = ContentStrategy.model_validate(raw)
                key = (strategy.id, strategy.version)
                digest = sdb.strategy_sha256(strategy.model_dump(mode="json"))
                bodies.setdefault(key, {})[digest] = strategy
            for (strategy_id, version), by_digest in sorted(bodies.items()):
                label = f"{strategy_id}@v{version}"
                existing = sdb.find_version(session, strategy_id, version)
                if len(by_digest) > 1:
                    conflicts.append(f"{label}: {len(by_digest)} different bodies in runs")
                elif existing is not None:
                    if existing.content_sha256 in by_digest:
                        already.append(label)
                    else:
                        conflicts.append(f"{label}: registered with a different body")
                else:
                    registered.append(label)
                    if not dry_run:
                        [strategy] = by_digest.values()
                        sdb.insert_version(
                            session,
                            version_id=new_id("sv"),
                            strategy=strategy,
                            origin=StrategyVersionOrigin.IMPORTED_FROM_RUN,
                            parent_version_id=None,
                            created_by="import-runs",
                            auth_method=None,
                            note="imported from a run snapshot",
                        )
        return ImportReport(registered=registered, already_registered=already, conflicts=conflicts)

    def history(self, strategy_id: str) -> StrategyHistoryView:
        with self._db.transaction() as session:
            rows = sdb.versions_of(session, strategy_id)
            if not rows:
                raise StrategyVersionNotFoundError(f"no versions of strategy {strategy_id}")
            active = sdb.active_version_id(session, rows[0].account_id)
            activations = sdb.activations_of(session, [r.id for r in rows])
            return StrategyHistoryView(
                strategy_id=strategy_id,
                versions=[sdb.version_view(r, active) for r in rows],
                activations=[sdb.activation_view(a) for a in activations],
            )

    def active(self, account_id: str) -> ActiveStrategyView:
        with self._db.transaction() as session:
            activation = sdb.latest_activation(session, account_id)
            if activation is None:
                raise NoActiveStrategyError(f"account {account_id} has no active strategy")
            row = sdb.get_version(session, activation.strategy_version_id)
            return ActiveStrategyView(
                account_id=account_id,
                version=sdb.version_view(row, row.id),
                activation=sdb.activation_view(activation),
            )

    def active_strategy(self, account_id: str) -> ContentStrategy:
        return self.active(account_id).version.content

    def activate(
        self,
        strategy_id: str,
        version: int,
        *,
        expected_active_version_id: str | None,
        reviewer: str,
        auth: ReviewAuthMethod,
        note: str | None,
        now: datetime | None = None,
    ) -> ActiveStrategyView:
        """Explicit, compare-and-set: fails (409) unless the active version is still the
        one the caller saw. Two concurrent activations cannot both succeed."""
        try:
            with self._db.transaction() as session:
                row = sdb.find_version(session, strategy_id, version)
                if row is None:
                    raise StrategyVersionNotFoundError(f"{strategy_id} v{version} not found")
                current = sdb.latest_activation(session, row.account_id)
                current_id = current.strategy_version_id if current is not None else None
                if current_id == row.id:
                    raise ProposalStateError(f"{strategy_id} v{version} is already active")
                if current_id != expected_active_version_id:
                    raise ProposalStateError(
                        "the active version changed: expected "
                        f"{expected_active_version_id}, found {current_id}"
                    )
                session.add(
                    StrategyActivationRow(
                        id=new_id("act"),
                        account_id=row.account_id,
                        seq=(current.seq if current is not None else 0) + 1,
                        strategy_version_id=row.id,
                        previous_version_id=current_id,
                        activated_by=reviewer,
                        auth_method=auth.value,
                        note=note,
                        activated_at=now or utc_now(),
                    )
                )
                session.flush()
                account_id = row.account_id
        except IntegrityError:
            raise ProposalStateError("another activation happened concurrently") from None
        logger.info(
            "strategy activated",
            extra={"strategy_id": strategy_id, "strategy_version": version},
        )
        return self.active(account_id)

    # --- proposals --------------------------------------------------------------------

    def request_proposal(
        self, evaluation_id: str, *, requested_by: str | None, now: datetime | None = None
    ) -> ProposalView:
        agent = self._require_agent()
        now = now or utc_now()
        with self._db.transaction() as session:
            proposal_id = self._insert_proposal(
                session, evaluation_id, agent, requested_by=requested_by, retry_of=None, now=now
            )
        return self._generate(proposal_id, agent)

    def retry_proposal(
        self, proposal_id: str, *, requested_by: str | None, now: datetime | None = None
    ) -> ProposalView:
        """Explicit retry of a ``failed`` proposal, or of a ``generating`` one past the
        stale threshold (closed as ``abandoned``; a late result for it is discarded). Each
        proposal can be retried once (``UNIQUE retry_of``); the new one gets the next."""
        agent = self._require_agent()
        now = now or utc_now()
        try:
            with self._db.transaction() as session:
                old = sdb.get_proposal(session, proposal_id, lock=True)
                if old.status == ProposalStatus.GENERATING.value:
                    if (now - old.started_at).total_seconds() <= self._stale_seconds:
                        raise ProposalStateError(
                            f"proposal {proposal_id} is still generating; retry once it is stale"
                        )
                    sdb.transition(
                        session,
                        old.id,
                        ProposalStatus.GENERATING,
                        status=ProposalStatus.ABANDONED.value,
                        finished_at=now,
                        error_type="Abandoned",
                        error_message="no result before the stale threshold; closed by a retry",
                    )
                elif old.status != ProposalStatus.FAILED.value:
                    raise ProposalStateError(
                        f"proposal {proposal_id} is {old.status}; only failed or stale "
                        "proposals can be retried"
                    )
                new_id_ = self._insert_proposal(
                    session,
                    old.evaluation_id,
                    agent,
                    requested_by=requested_by,
                    retry_of=old.id,
                    now=now,
                )
        except IntegrityError:
            raise ProposalStateError(
                f"proposal {proposal_id} was already retried, or another proposal is open"
            ) from None
        return self._generate(new_id_, agent)

    def get_proposal(self, proposal_id: str, *, now: datetime | None = None) -> ProposalView:
        with self._db.transaction() as session:
            return self._view(session, sdb.get_proposal(session, proposal_id), now)

    def list_proposals(
        self,
        *,
        status: ProposalStatus | None,
        account_id: str | None,
        evaluation_id: str | None,
        limit: int,
    ) -> list[ProposalView]:
        with self._db.transaction() as session:
            rows = sdb.list_proposals(
                session,
                status=status,
                account_id=account_id,
                evaluation_id=evaluation_id,
                limit=limit,
            )
            return [self._view(session, r, None) for r in rows]

    # --- decisions --------------------------------------------------------------------

    def approve(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        auth: ReviewAuthMethod,
        summary: str,
        proposal_sha256: str,
        evaluation_digest: str,
        now: datetime | None = None,
    ) -> ProposalView:
        """All-or-nothing. Creates a new immutable version; does not activate it."""
        now = now or utc_now()
        superseded = False
        try:
            with self._db.transaction() as session:
                row = sdb.get_proposal(session, proposal_id, lock=True)
                self._require_pending(row)
                if row.outcome != ProposalOutcome.CHANGE.value or row.proposal is None:
                    raise ProposalStateError(
                        "a no-change proposal has nothing to approve; reject it to close it"
                    )
                if (
                    proposal_sha256 != row.proposal_sha256
                    or evaluation_digest != row.evaluation_digest
                ):
                    raise DigestMismatchError(
                        "the proposal or evaluation digest does not match the stored proposal"
                    )
                if sdb.active_version_id(session, row.account_id) != row.base_version_id:
                    sdb.transition(
                        session,
                        row.id,
                        ProposalStatus.PENDING_REVIEW,
                        status=ProposalStatus.SUPERSEDED.value,
                    )
                    superseded = True
                else:
                    self._create_version(session, row, reviewer, auth, summary, now)
        except IntegrityError:
            raise ProposalStateError(
                f"proposal {proposal_id} was decided concurrently, or the version exists"
            ) from None
        if superseded:
            raise ProposalStateError(
                "the active strategy changed since this proposal was made; it is superseded"
            )
        return self.get_proposal(proposal_id)

    def reject(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        auth: ReviewAuthMethod,
        summary: str,
        now: datetime | None = None,
    ) -> ProposalView:
        """Records the decision. The active strategy and every version stay as they are."""
        now = now or utc_now()
        try:
            with self._db.transaction() as session:
                row = sdb.get_proposal(session, proposal_id, lock=True)
                self._require_pending(row)
                session.add(
                    StrategyProposalDecisionRow(
                        id=new_id("sdec"),
                        proposal_id=row.id,
                        decision=DecisionKind.REJECT.value,
                        reviewer=reviewer,
                        auth_method=auth.value,
                        summary=summary,
                        reviewed_proposal_sha256=row.proposal_sha256,
                        reviewed_evaluation_digest=row.evaluation_digest,
                        resulting_version_id=None,
                        decided_at=now,
                    )
                )
                sdb.transition(
                    session,
                    row.id,
                    ProposalStatus.PENDING_REVIEW,
                    status=ProposalStatus.REJECTED.value,
                )
        except IntegrityError:
            raise ProposalStateError(f"proposal {proposal_id} was decided concurrently") from None
        return self.get_proposal(proposal_id)

    # --- internals --------------------------------------------------------------------

    def _require_agent(self) -> StrategyAgent:
        if self._agent is None or self._proposer is None:
            raise ProposalsDisabledError(
                "strategy proposals are disabled (STRATEGY_PROPOSER=disabled)"
            )
        return self._agent

    @staticmethod
    def _require_pending(row: StrategyProposalRow) -> None:
        if row.status != ProposalStatus.PENDING_REVIEW.value:
            raise ProposalStateError(f"proposal {row.id} is {row.status}, not pending_review")

    def _insert_proposal(
        self,
        session: Session,
        evaluation_id: str,
        agent: StrategyAgent,
        *,
        requested_by: str | None,
        retry_of: str | None,
        now: datetime,
    ) -> str:
        evaluation = edb.get(session, evaluation_id)
        activation = sdb.latest_activation(session, evaluation.account_id)
        if activation is None:
            raise ProposalStateError(
                f"account {evaluation.account_id} has no active strategy: register and "
                "activate a version first"
            )
        base = sdb.get_version(session, activation.strategy_version_id)
        if evaluation.strategy_id is not None and evaluation.strategy_id != base.strategy_id:
            raise ProposalStateError(
                "the evaluation is scoped to a different strategy than the active one"
            )
        open_ = sdb.open_proposal(session, evaluation.account_id)
        if open_ is not None:
            raise ProposalStateError(
                f"proposal {open_.id} is already open ({open_.status}) for this account; "
                "decide it, or retry it once it is failed or stale"
            )
        assert self._proposer is not None
        row = StrategyProposalRow(
            id=new_id("prop"),
            evaluation_id=evaluation.id,
            evaluation_digest=evaluation.digest,
            account_id=evaluation.account_id,
            base_version_id=base.id,
            status=ProposalStatus.GENERATING.value,
            proposer=self._proposer.value,
            provider=agent.settings.provider,
            model=agent.settings.model,
            prompt_version=PROMPT_VERSION,
            retry_of=retry_of,
            requested_by=requested_by,
            pricing_id=repo.save_price_list(session, self._prices),
            started_at=now,
            llm_calls=0,
        )
        session.add(row)
        session.flush()  # the partial unique index refuses a second open proposal here
        return row.id

    def _generate(self, proposal_id: str, agent: StrategyAgent) -> ProposalView:
        """Run outside any transaction: the ``generating`` row is already committed."""
        with self._db.transaction() as session:
            row = sdb.get_proposal(session, proposal_id)
            evaluation = edb.get(session, row.evaluation_id)
            base = sdb.get_version(session, row.base_version_id)
            summary = EvaluationSummary.model_validate(evaluation.summary)
            config = EvaluationConfig.model_validate(evaluation.config)
            strategy = ContentStrategy.model_validate(base.content)
            evaluation_id = evaluation.id

        if summary.status is EvaluationStatus.INSUFFICIENT_EVIDENCE:
            proposal = ValidatedProposal(
                outcome=ProposalOutcome.NO_CHANGE,
                summary=INSUFFICIENT_SUMMARY,
                changes=[],
                posting_time_note=None,
                posting_time_evidence_refs=[],
                proposed_strategy=None,
            )
            self._finish(
                proposal_id,
                status=ProposalStatus.PENDING_REVIEW,
                proposal=proposal,
                proposer=Proposer.DETERMINISTIC,
                call=None,
                latency_ms=0.0,
            )
            return self.get_proposal(proposal_id)

        # Phase 8: the model sees a compact digest, and is validated against the same.
        digest, omitted = digest_summary(summary)
        context = ProposalContext(
            evaluation_id=evaluation_id,
            summary=digest,
            omitted=omitted,
            thresholds={
                "min_group_n": config.min_group_n,
                "min_compare_n": config.min_compare_n,
                "min_total_n": config.min_total_n,
            },
            strategy=strategy,
        )
        started = time.perf_counter()
        try:
            result = agent.run(context)
        except (ProviderError, AgentOutputError) as exc:
            category = "provider error" if isinstance(exc, ProviderError) else "invalid output"
            self._finish(
                proposal_id,
                status=ProposalStatus.FAILED,
                call=exc.llm_call,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
                error_message=f"model call failed ({category})",
            )
            return self.get_proposal(proposal_id)
        except Exception as exc:
            self._finish(
                proposal_id,
                status=ProposalStatus.FAILED,
                call=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
                error_message="unexpected error",
            )
            raise
        latency = (time.perf_counter() - started) * 1000
        try:
            proposal = validate_proposal(
                result.output, summary=digest, config=config, base=strategy
            )
        except ProposalValidationError as exc:
            self._finish(
                proposal_id,
                status=ProposalStatus.FAILED,
                call=result.call,
                latency_ms=latency,
                error_type=type(exc).__name__,
                error_message="the proposal failed deterministic validation",
                validation_errors=exc.reasons,
            )
            return self.get_proposal(proposal_id)
        self._finish(
            proposal_id,
            status=ProposalStatus.PENDING_REVIEW,
            proposal=proposal,
            call=result.call,
            latency_ms=latency,
        )
        return self.get_proposal(proposal_id)

    def _finish(
        self,
        proposal_id: str,
        *,
        status: ProposalStatus,
        call: LLMCall | None,
        latency_ms: float,
        proposal: ValidatedProposal | None = None,
        proposer: Proposer | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        validation_errors: list[str] | None = None,
    ) -> None:
        values: dict[str, object] = {
            "status": status.value,
            "finished_at": utc_now(),
            "latency_ms": latency_ms,
            "llm_calls": 0 if call is None else 1,
            "input_tokens": call.usage.input_tokens if call and call.usage else None,
            "output_tokens": call.usage.output_tokens if call and call.usage else None,
            "error_type": error_type,
            "error_message": error_message,
            "validation_errors": validation_errors,
        }
        if call is not None:
            values["model"] = call.model
        if proposer is Proposer.DETERMINISTIC:
            values |= {"proposer": proposer.value, "provider": "none", "model": "none"}
        if proposal is not None:
            values |= {
                "outcome": proposal.outcome.value,
                "proposal": proposal.model_dump(mode="json"),
            }
        with self._db.transaction() as session:
            if proposal is not None:
                row = sdb.get_proposal(session, proposal_id)
                values["proposal_sha256"] = proposal_digest(
                    row.evaluation_id, row.evaluation_digest, row.base_version_id, proposal
                )
            applied = sdb.transition(session, proposal_id, ProposalStatus.GENERATING, **values)
        if not applied:
            # Closed (abandoned) by a retry while the model was answering: discard.
            logger.warning("late proposal result discarded", extra={"proposal_id": proposal_id})

    def _create_version(
        self,
        session: Session,
        row: StrategyProposalRow,
        reviewer: str,
        auth: ReviewAuthMethod,
        summary: str,
        now: datetime,
    ) -> None:
        assert row.proposal is not None
        proposal = ValidatedProposal.model_validate(row.proposal)
        base_row = sdb.get_version(session, row.base_version_id)
        base = ContentStrategy.model_validate(base_row.content)
        proposed = proposal.proposed_strategy
        if proposed is None or not changed_fields(base, proposed) <= ALLOWED_FIELD_NAMES:
            raise ProposalStateError("the stored proposal changes fields outside the allowlist")
        strategy = ContentStrategy.model_validate(
            {
                **proposed.model_dump(mode="json"),
                "id": base.id,
                "account_id": base.account_id,
                "version": sdb.next_version_number(session, base.id),
            }
        )
        version = sdb.insert_version(
            session,
            version_id=new_id("sv"),
            strategy=strategy,
            origin=StrategyVersionOrigin.PROPOSAL,
            parent_version_id=base_row.id,
            created_by=reviewer,
            auth_method=auth.value,
            note=summary,
        )
        session.add(
            StrategyProposalDecisionRow(
                id=new_id("sdec"),
                proposal_id=row.id,
                decision=DecisionKind.APPROVE.value,
                reviewer=reviewer,
                auth_method=auth.value,
                summary=summary,
                reviewed_proposal_sha256=row.proposal_sha256,
                reviewed_evaluation_digest=row.evaluation_digest,
                resulting_version_id=version.id,
                decided_at=now,
            )
        )
        sdb.transition(
            session, row.id, ProposalStatus.PENDING_REVIEW, status=ProposalStatus.APPROVED.value
        )
        session.flush()

    def _version_view(self, session: Session, version_id: str) -> StrategyVersionView:
        row = sdb.get_version(session, version_id)
        return sdb.version_view(row, sdb.active_version_id(session, row.account_id))

    def _view(
        self, session: Session, row: StrategyProposalRow, now: datetime | None
    ) -> ProposalView:
        return sdb.proposal_view(
            session,
            row,
            prices=self._prices,
            now=now or utc_now(),
            stale_seconds=self._stale_seconds,
        )


def proposal_digest(
    evaluation_id: str, evaluation_digest: str, base_version_id: str, proposal: ValidatedProposal
) -> str:
    """What a reviewer approves: echoed back on approval so a changed proposal is refused."""
    return sha256_json(
        {
            "evaluation_id": evaluation_id,
            "evaluation_digest": evaluation_digest,
            "base_version_id": base_version_id,
            "proposal": proposal.model_dump(mode="json"),
        }
    )
