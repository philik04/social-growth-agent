"""HTTP transport. Thin: routes call services, never the graph directly."""

from social_growth_agent.api.app import create_app

__all__ = ["create_app"]
