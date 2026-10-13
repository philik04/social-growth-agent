"""Performance evaluations (Phase 7): deterministic, descriptive, reproducible.

Creating an evaluation reads stored snapshots only. It never calls the platform, never
calls a model and never changes a strategy. The same request (same account, age, window,
cutoff, scope and thresholds) returns the stored evaluation instead of a second one.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError

from social_growth_agent.accounting import PriceList
from social_growth_agent.analytics.evaluation import (
    EvaluationConfig,
    HourBlock,
    evaluate,
    evaluation_digest,
    sha256_json,
)
from social_growth_agent.errors import InvalidEvaluationRequestError
from social_growth_agent.models import SnapshotAge, new_id, utc_now
from social_growth_agent.models.strategy import Observation
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import evaluations as edb
from social_growth_agent.persistence.analytics import DEFAULT_ON_TARGET_SECONDS
from social_growth_agent.persistence.tables import PerformanceEvaluationRow
from social_growth_agent.persistence.views import EvaluationCreated, EvaluationView

logger = logging.getLogger(__name__)


class EvaluationRequest(BaseModel):
    """Datetimes must carry a timezone. ``data_cutoff`` defaults to now and may not be in
    the future; ``min_*`` may raise the configured thresholds, never go below the floors."""

    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    snapshot_age: str = Field(min_length=1, max_length=16)
    published_since: datetime | None = None
    published_until: datetime | None = None
    data_cutoff: datetime | None = None
    strategy_id: str | None = Field(default=None, max_length=64)
    synthetic: bool = False
    min_group_n: int | None = None
    min_compare_n: int | None = None
    min_total_n: int | None = None
    created_by: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _aware(self) -> Self:
        for name in ("published_since", "published_until", "data_cutoff"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must include a timezone")
        since, until = self.published_since, self.published_until
        if since is not None and until is not None and since >= until:
            raise ValueError("published_since must be before published_until")
        return self


class EvaluationDefaults(BaseModel):
    model_config = ConfigDict(frozen=True)

    ages: tuple[SnapshotAge, ...]
    min_group_n: int = 5
    min_compare_n: int = 10
    min_total_n: int = 20
    hour_block_hours: HourBlock = 4
    length_bucket_edges: tuple[int, ...] = (100, 200)
    on_target_min_seconds: int = DEFAULT_ON_TARGET_SECONDS


class EvaluationService:
    def __init__(self, db: Database, *, defaults: EvaluationDefaults, prices: PriceList) -> None:
        self._db = db
        self._defaults = defaults
        self._prices = prices

    def create(
        self, request: EvaluationRequest, *, now: datetime | None = None
    ) -> EvaluationCreated:
        now = now or utc_now()
        cutoff = request.data_cutoff or now
        if cutoff > now:
            raise InvalidEvaluationRequestError("data_cutoff may not be in the future")
        config = self._config(request)
        normalized = _normalized(request, cutoff)
        input_digest = sha256_json(
            {"request": normalized, "config": config.model_dump(mode="json")}
        )
        with self._db.transaction() as session:
            existing = edb.find_by_input_digest(session, input_digest)
            if existing is not None:
                return EvaluationCreated(
                    created=False, evaluation=edb.view(session, existing, prices=self._prices)
                )
        started = time.perf_counter()
        try:
            with self._db.transaction() as session:
                posts = edb.load_posts(
                    session,
                    account_id=request.account_id,
                    snapshot_age=config.snapshot_age,
                    cutoff=cutoff,
                    since=request.published_since,
                    until=request.published_until,
                    strategy_id=request.strategy_id,
                )
                result = evaluate(posts, config, cutoff)
                row = PerformanceEvaluationRow(
                    id=new_id("eval"),
                    account_id=request.account_id,
                    strategy_id=request.strategy_id,
                    snapshot_age=config.snapshot_age,
                    published_since=request.published_since,
                    published_until=request.published_until,
                    data_cutoff=cutoff,
                    synthetic=request.synthetic,
                    config=config.model_dump(mode="json"),
                    config_version=config.config_version,
                    request=normalized,
                    input_digest=input_digest,
                    digest=evaluation_digest(normalized, config, result),
                    status=result.summary.status.value,
                    eligible_count=result.summary.eligible_count,
                    excluded_count=result.summary.excluded_count,
                    posts_considered=len(posts),
                    snapshots_used=result.summary.eligible_count,
                    summary=result.summary.model_dump(mode="json"),
                    duration_ms=(time.perf_counter() - started) * 1000,
                    created_by=request.created_by,
                )
                edb.insert(session, row, result.observations)
                view = edb.view(session, row, prices=self._prices)
        except IntegrityError:
            # An identical request committed first: return that one.
            with self._db.transaction() as session:
                existing = edb.find_by_input_digest(session, input_digest)
                if existing is None:
                    raise
                return EvaluationCreated(
                    created=False, evaluation=edb.view(session, existing, prices=self._prices)
                )
        logger.info(
            "evaluation created",
            extra={
                "evaluation_id": view.id,
                "eligible": view.eligible_count,
                "excluded": view.excluded_count,
                "evaluation_status": view.status.value,
            },
        )
        return EvaluationCreated(created=True, evaluation=view)

    def get(self, evaluation_id: str) -> EvaluationView:
        with self._db.transaction() as session:
            return edb.view(session, edb.get(session, evaluation_id), prices=self._prices)

    def list_evaluations(self, *, account_id: str | None, limit: int) -> list[EvaluationView]:
        with self._db.transaction() as session:
            return [
                edb.view(session, r, prices=self._prices)
                for r in edb.list_rows(session, account_id=account_id, limit=limit)
            ]

    def observations(
        self, evaluation_id: str, *, eligible: bool | None, limit: int, offset: int
    ) -> list[Observation]:
        with self._db.transaction() as session:
            edb.get(session, evaluation_id)
            return edb.observations(
                session, evaluation_id, eligible=eligible, limit=limit, offset=offset
            )

    def _config(self, request: EvaluationRequest) -> EvaluationConfig:
        d = self._defaults
        age = next((a for a in d.ages if a.label == request.snapshot_age.strip().lower()), None)
        if age is None:
            known = ", ".join(a.label for a in d.ages)
            raise InvalidEvaluationRequestError(
                f"snapshot_age must be one of the configured ages ({known})"
            )
        try:
            return EvaluationConfig(
                snapshot_age=age.label,
                snapshot_age_seconds=age.seconds,
                on_target_min_seconds=d.on_target_min_seconds,
                min_group_n=request.min_group_n or d.min_group_n,
                min_compare_n=request.min_compare_n or d.min_compare_n,
                min_total_n=request.min_total_n or d.min_total_n,
                hour_block_hours=d.hour_block_hours,
                length_bucket_edges=d.length_bucket_edges,
                synthetic=request.synthetic,
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise InvalidEvaluationRequestError(f"{where}: {first['msg']}") from None


def _normalized(request: EvaluationRequest, cutoff: datetime) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat()

    return {
        "account_id": request.account_id,
        "snapshot_age": request.snapshot_age.strip().lower(),
        "published_since": iso(request.published_since),
        "published_until": iso(request.published_until),
        "data_cutoff": iso(cutoff),
        "strategy_id": request.strategy_id,
        "synthetic": request.synthetic,
    }
