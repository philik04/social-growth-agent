"""FastAPI application factory. Run and review endpoints arrive with persistence (Phase 4)."""

from fastapi import FastAPI

from social_growth_agent import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="social-growth-agent", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
