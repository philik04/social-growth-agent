"""The source registry: which sources discovery may read, loaded from ``discovery.toml``.

Sources are configuration. Retrieved content can never add a source, a URL to fetch or an
account to read, and a feed may only be fetched from the host its registry entry names.
"""

import hashlib
import json
import tomllib
from pathlib import Path

from pydantic import ValidationError

from social_growth_agent.errors import ConfigurationError
from social_growth_agent.models.discovery import Registry


def load_registry(path: Path) -> Registry:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"discovery registry not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"discovery registry {path} is not valid TOML: {exc}") from None
    try:
        return Registry.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"discovery registry {path} is invalid: {exc}") from None


def registry_digest(registry: Registry) -> str:
    """Stable digest of the loaded registry, stored on every session."""
    canonical = json.dumps(registry.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
