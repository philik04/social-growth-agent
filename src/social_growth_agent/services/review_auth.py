"""Who may approve, reject, activate or register strategy versions (Phase 7).

The API has no user accounts, so a submitted reviewer name proves nothing. Strategy
review therefore requires ``Authorization: Bearer <STRATEGY_REVIEW_TOKEN>``, compared in
constant time. That proves possession of the shared token, not identity; the reviewer
name is recorded as a claim next to the ``auth_method``.

Without a configured token the review endpoints refuse (503), unless unauthenticated
review was explicitly enabled for local development (``APP_ENVIRONMENT=development`` and
``STRATEGY_REVIEW_ALLOW_UNAUTHENTICATED=true``); such decisions are recorded as
``unauthenticated_local``. The token never appears in errors, logs or stored rows.
"""

import hmac
from dataclasses import dataclass

from pydantic import SecretStr

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import (
    ReviewAuthInvalidError,
    ReviewAuthMissingError,
    ReviewAuthNotConfiguredError,
)
from social_growth_agent.models.strategy import ReviewAuthMethod


@dataclass(frozen=True)
class ReviewAuth:
    token: SecretStr | None = None
    allow_unauthenticated: bool = False

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "ReviewAuth":
        token = settings.strategy_review_token
        if token is not None and not token.get_secret_value().strip():
            token = None
        return cls(
            token=token, allow_unauthenticated=settings.strategy_review_allow_unauthenticated
        )

    def authenticate(self, authorization: str | None) -> ReviewAuthMethod:
        if self.token is None:
            if self.allow_unauthenticated:
                return ReviewAuthMethod.UNAUTHENTICATED_LOCAL
            raise ReviewAuthNotConfiguredError(
                "strategy review is not configured: set STRATEGY_REVIEW_TOKEN"
            )
        if not authorization:
            raise ReviewAuthMissingError("strategy review requires a bearer token")
        scheme, _, presented = authorization.partition(" ")
        expected = self.token.get_secret_value().encode("utf-8")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.strip().encode("utf-8"), expected
        ):
            raise ReviewAuthInvalidError("invalid strategy review token")
        return ReviewAuthMethod.REVIEW_TOKEN
