"""Scripted, offline analytics provider for tests, demos and local development."""

import hashlib
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime

from social_growth_agent.models import (
    AnalyticsFailureCategory,
    AnalyticsFetch,
    MetricsScope,
    PostMetricMiss,
    PostMetricRecord,
)

AnalyticsScriptStep = BaseException | AnalyticsFetch | Callable[[Sequence[str]], object] | None
"""Per call: raise the exception, return the fetch as-is, run the callable first (it may
return an ``AnalyticsFetch`` to use instead), or ``None`` for the default answer."""


class MockAnalyticsProvider:
    """Answers with synthetic but stable public metrics derived from the post id.

    The numbers are made up (they say so: ``provider_name = "mock_analytics"``, an
    unbilled provider) and exist only so the pipeline can run offline. ``deleted`` ids
    are answered as not found; ``created_at`` supplies each post's creation time, which
    otherwise stays unknown (``None``), as for a platform that does not report it.
    """

    platform = "x"
    provider_name = "mock_analytics"
    metrics_scope: MetricsScope = "public"
    max_batch_size = 100

    def __init__(
        self,
        script: Iterable[AnalyticsScriptStep] = (),
        *,
        created_at: Mapping[str, datetime] | None = None,
        deleted: Iterable[str] = (),
        omit_impressions: bool = False,
    ) -> None:
        self._script = list(script)
        self._created_at = dict(created_at or {})
        self._deleted = set(deleted)
        self._omit_impressions = omit_impressions
        self._lock = threading.Lock()
        self.calls: list[list[str]] = []

    def fetch_post_metrics(self, post_ids: Sequence[str]) -> AnalyticsFetch:
        with self._lock:
            self.calls.append(list(post_ids))
            step = self._script.pop(0) if self._script else None
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, AnalyticsFetch):
            return step
        if step is not None:
            produced = step(post_ids)
            if isinstance(produced, AnalyticsFetch):
                return produced
        records = [self.metrics_for(i) for i in dict.fromkeys(post_ids) if i not in self._deleted]
        misses = [
            PostMetricMiss(
                provider_post_id=i,
                category=AnalyticsFailureCategory.NOT_FOUND,
                detail="Could not find the post (mock: deleted)",
            )
            for i in dict.fromkeys(post_ids)
            if i in self._deleted
        ]
        return AnalyticsFetch(
            records=records,
            misses=misses,
            http_status=200,
            latency_ms=1.0,
            posts_returned=len(records),
        )

    def metrics_for(self, post_id: str) -> PostMetricRecord:
        seed = int.from_bytes(hashlib.sha256(post_id.encode()).digest()[:8], "big")
        impressions = 1_000 + seed % 20_000
        calls = sum(post_id in c for c in self.calls)  # grows with each read, like a real post
        return PostMetricRecord(
            provider_post_id=post_id,
            provider_created_at=self._created_at.get(post_id),
            likes=impressions // 40 + calls,
            reposts=impressions // 250,
            replies=impressions // 400,
            quotes=impressions // 1000,
            bookmarks=impressions // 300,
            impressions=None if self._omit_impressions else impressions * calls,
        )
