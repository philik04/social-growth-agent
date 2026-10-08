"""Domain errors -> HTTP status codes. Messages are ours and never contain secrets."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from social_growth_agent.errors import (
    DatabaseUnavailableError,
    InvalidReviewError,
    InvalidRunStateError,
    InvalidScheduleError,
    ProviderError,
    PublicationExistsError,
    ResourceNotFoundError,
    RunNotFoundError,
    SocialGrowthError,
)

_STATUS: list[tuple[type[SocialGrowthError], int]] = [
    (RunNotFoundError, 404),
    (ResourceNotFoundError, 404),
    (InvalidScheduleError, 422),
    (InvalidRunStateError, 409),
    (InvalidReviewError, 409),
    (DatabaseUnavailableError, 503),
    (ProviderError, 503),
]


class PersistenceNotConfiguredError(SocialGrowthError):
    """The API was started without DATABASE_URL; run endpoints are unavailable."""


def install_error_handlers(app: FastAPI) -> None:
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, PersistenceNotConfiguredError):
            return JSONResponse({"detail": str(exc)}, status_code=503)
        if isinstance(exc, RunNotFoundError):
            return JSONResponse({"detail": f"run not found: {exc}"}, status_code=404)
        if isinstance(exc, PublicationExistsError):
            return JSONResponse(
                {
                    "detail": str(exc),
                    "publication_id": exc.publication_id,
                    "status": exc.status,
                },
                status_code=409,
            )
        for error_type, status in _STATUS:
            if isinstance(exc, error_type):
                return JSONResponse({"detail": str(exc)}, status_code=status)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    app.add_exception_handler(SocialGrowthError, handle)
