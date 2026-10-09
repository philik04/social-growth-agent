"""The price list: provider prices live only in a TOML file, never in code.

A price that is not in the file is *unknown*, not zero; estimates that need it say so.
Each loaded price list gets a stable id derived from its content (``<version>:<hash>``),
so the exact configuration a run was estimated with can be stored and reused later.
"""

import hashlib
import json
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from social_growth_agent.errors import ConfigurationError


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_million: Decimal | None = Field(default=None, ge=0)
    output_per_million: Decimal | None = Field(default=None, ge=0)


class XPrices(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    post_read: Decimal | None = Field(
        default=None,
        ge=0,
        description="Per post read: research search results and analytics metric reads.",
    )
    post_create: Decimal | None = Field(
        default=None,
        ge=0,
        description="Per post created (publishing). X bills post creation; while this is "
        "unset, publish lines are reported as missing prices, never as free.",
    )
    user_read: Decimal | None = Field(default=None, ge=0, description="Per user object read.")


class PriceList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Date the prices were checked.")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    source: str | None = None
    unbilled_providers: list[str] = Field(
        default_factory=list,
        description="Providers that never cost money (mock fixtures, fake agents).",
    )
    x: XPrices = Field(default_factory=XPrices)
    openai: dict[str, ModelPrice] = Field(
        default_factory=dict, description="Prices per OpenAI model name."
    )

    @property
    def id(self) -> str:
        """``<version>:<12 hex of sha256(content)>``: changes whenever any price changes."""
        digest = hashlib.sha256(json.dumps(self.content(), sort_keys=True).encode()).hexdigest()
        return f"{self.version}:{digest[:12]}"

    def content(self) -> dict[str, Any]:
        """Canonical JSON form; Decimals are stored as strings so nothing is rounded."""
        return self.model_dump(mode="json")

    def llm_price(self, provider: str, model: str) -> ModelPrice | None:
        return self.openai.get(model) if provider == "openai" else None


def load_price_list(path: Path) -> PriceList:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"pricing file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"pricing file {path} is not valid TOML: {exc}") from None
    try:
        return PriceList.model_validate(_normalize(raw))
    except ValidationError as exc:
        raise ConfigurationError(f"pricing file {path} is invalid: {exc}") from None


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """TOML layout -> model layout: ``[openai.models."<name>"]`` -> ``openai[<name>]``.
    Floats are converted via ``str`` so 0.1 stays 0.1."""
    data: dict[str, Any] = {k: _decimals(v) for k, v in raw.items()}
    data["openai"] = _decimals(dict(raw.get("openai", {}).get("models", {})))
    return data


def _decimals(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimals(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimals(v) for v in value]
    return value
