"""Derived metrics, computed in application code from what the platform reported.

Conservative by construction:

- a total is ``None`` when any of its parts is ``None`` (a partial sum would look like a
  smaller total);
- a rate exists only when its denominator was reported and is greater than 0;
- these are *observed* values of one post. They say nothing about why a post did better
  or worse than another: posts differ in audience, time, topic and more.
"""

from pydantic import BaseModel, ConfigDict, Field


def total_engagement(
    likes: int | None, reposts: int | None, replies: int | None, quotes: int | None
) -> int | None:
    """``likes + reposts + replies + quotes``; bookmarks are reported separately."""
    parts = (likes, reposts, replies, quotes)
    if any(p is None for p in parts):
        return None
    return sum(p for p in parts if p is not None)


def rate_per_impression(count: int | None, impressions: int | None) -> float | None:
    if count is None or impressions is None or impressions <= 0:
        return None
    return count / impressions


def engagement_rate_by_impressions(
    likes: int | None,
    reposts: int | None,
    replies: int | None,
    quotes: int | None,
    impressions: int | None,
) -> float | None:
    return rate_per_impression(total_engagement(likes, reposts, replies, quotes), impressions)


class ObservedRates(BaseModel):
    """Derived from one snapshot. ``None`` means not computable from what was reported."""

    model_config = ConfigDict(frozen=True)

    observed_engagement_count: int | None = Field(
        description="likes + reposts + replies + quotes, as reported; None if any is missing."
    )
    observed_engagement_rate_by_impressions: float | None = Field(
        description="observed_engagement_count / impressions; None without impressions > 0."
    )
    observed_reply_rate: float | None = Field(description="replies / impressions")
    observed_repost_rate: float | None = Field(description="reposts / impressions")


def observed_rates(
    *,
    likes: int | None,
    reposts: int | None,
    replies: int | None,
    quotes: int | None,
    impressions: int | None,
) -> ObservedRates:
    engagement = total_engagement(likes, reposts, replies, quotes)
    return ObservedRates(
        observed_engagement_count=engagement,
        observed_engagement_rate_by_impressions=rate_per_impression(engagement, impressions),
        observed_reply_rate=rate_per_impression(replies, impressions),
        observed_repost_rate=rate_per_impression(reposts, impressions),
    )
