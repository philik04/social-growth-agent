"""Pure cost estimation: usage counts x a price list. No I/O, no provider knowledge."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from social_growth_agent.accounting.pricing import PriceList

_MILLION = Decimal(1_000_000)


class LLMUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    calls_without_usage: int = Field(
        default=0, ge=0, description="Calls that reported no token counts (e.g. failures)."
    )


class ResearchUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    fetches: int = Field(ge=0)
    requests: int = Field(ge=0)
    post_reads: int = Field(ge=0)
    user_reads: int = Field(ge=0)


class PublishUsage(BaseModel):
    """Publish operations for one run. Priced with ``x.post_create`` when configured.
    X's pay-per-use pricing does bill post creation (per request, more for posts with a
    URL); the price is left blank in ``pricing.toml`` until you set it, and the line is
    then reported under ``missing_prices`` instead of being assumed free."""

    model_config = ConfigDict(frozen=True)

    provider: str
    attempts: int = Field(ge=0, description="Platform post-create calls made.")
    posts_created: int = Field(ge=0, description="Calls that definitely created a post.")


class AnalyticsUsage(BaseModel):
    """Metrics reads for one run's publications (Phase 6)."""

    model_config = ConfigDict(frozen=True)

    provider: str
    requests: int = Field(
        ge=0,
        description="Provider requests that touched this run's posts. A batched request "
        "counts for every run it touched, so this is not additive across runs.",
    )
    post_reads: int = Field(
        ge=0,
        description="Times a post of this run came back in a response: the billed unit. "
        "An upper bound: the platform deduplicates repeated reads within a UTC day.",
    )
    snapshots: int = Field(ge=0, description="Metric snapshots stored.")
    failed_requests: int = Field(ge=0)
    unfinished_requests: int = Field(
        ge=0,
        description="Requests started but never finished (the process died); their reads, "
        "if any, are not counted in post_reads.",
    )


class UsageCounts(BaseModel):
    """Canonical, provider-reported resource counts for one run."""

    model_config = ConfigDict(frozen=True)

    research: list[ResearchUsage] = Field(default_factory=list)
    llm: list[LLMUsage] = Field(default_factory=list)
    publishing: list[PublishUsage] = Field(default_factory=list)
    analytics: list[AnalyticsUsage] = Field(default_factory=list)


class CostLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: str
    provider: str
    quantity: int
    unit_price: Decimal | None
    unit: str
    cost: Decimal | None = Field(description="None when the price is not configured.")


class PricingMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version: str
    as_of: str
    currency: str
    source: str | None


class CostEstimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    estimated: bool = True
    basis: str = Field(description="'run_pricing' or 'current_pricing'.")
    pricing: PricingMetadata
    lines: list[CostLine]
    missing_prices: list[str]
    total: Decimal | None = Field(description="None unless every billable price is known.")


def estimate_cost(usage: UsageCounts, prices: PriceList, *, basis: str) -> CostEstimate:
    lines: list[CostLine] = []
    unbilled = set(prices.unbilled_providers)
    for r in usage.research:
        free = r.provider in unbilled
        post_price = prices.x.post_read if r.provider == "x" else None
        user_price = prices.x.user_read if r.provider == "x" else None
        lines.append(_line("post_reads", r.provider, r.post_reads, "post", post_price, free))
        lines.append(_line("user_reads", r.provider, r.user_reads, "user", user_price, free))
    for pub in usage.publishing:
        free = pub.provider in unbilled
        create_price = prices.x.post_create if pub.provider == "x" else None
        lines.append(
            _line("posts_created", pub.provider, pub.posts_created, "post", create_price, free)
        )
    for a in usage.analytics:
        # Same unit as research reads: a post returned by the X API. No separate
        # analytics price is invented.
        free = a.provider in unbilled
        read_price = prices.x.post_read if a.provider == "x" else None
        lines.append(
            _line("analytics_post_reads", a.provider, a.post_reads, "post", read_price, free)
        )
    for u in usage.llm:
        free = u.provider in unbilled
        price = prices.llm_price(u.provider, u.model)
        per_input = _per_token(price.input_per_million if price else None)
        per_output = _per_token(price.output_per_million if price else None)
        tokens_in, tokens_out = f"{u.model} input_tokens", f"{u.model} output_tokens"
        lines.append(_line(tokens_in, u.provider, u.input_tokens, "token", per_input, free))
        lines.append(_line(tokens_out, u.provider, u.output_tokens, "token", per_output, free))

    missing = [f"{line.provider}:{line.item}" for line in lines if line.cost is None]
    costs = [line.cost for line in lines if line.cost is not None]
    pricing = PricingMetadata(
        id=prices.id,
        version=prices.version,
        as_of=prices.as_of,
        currency=prices.currency,
        source=prices.source,
    )
    return CostEstimate(
        basis=basis,
        pricing=pricing,
        lines=lines,
        missing_prices=missing,
        total=None if missing else sum(costs, Decimal(0)),
    )


def _per_token(per_million: Decimal | None) -> Decimal | None:
    return None if per_million is None else per_million / _MILLION


def _line(
    item: str, provider: str, quantity: int, unit: str, unit_price: Decimal | None, free: bool
) -> CostLine:
    if free:
        unit_price = Decimal(0)
    cost = None
    if quantity == 0:
        cost = Decimal(0)
    elif unit_price is not None:
        cost = unit_price * quantity
    return CostLine(
        item=item, provider=provider, quantity=quantity, unit=unit, unit_price=unit_price, cost=cost
    )
