"""Strategy review authentication (Phase 7), pure."""

import pytest
from pydantic import SecretStr, ValidationError

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import (
    ReviewAuthInvalidError,
    ReviewAuthMissingError,
    ReviewAuthNotConfiguredError,
)
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.services.review_auth import ReviewAuth

TOKEN = "a-review-token-of-some-length"


def test_a_configured_token_is_required_and_compared_exactly():
    auth = ReviewAuth(token=SecretStr(TOKEN))
    assert auth.authenticate(f"Bearer {TOKEN}") is ReviewAuthMethod.REVIEW_TOKEN
    assert auth.authenticate(f"bearer {TOKEN}") is ReviewAuthMethod.REVIEW_TOKEN
    with pytest.raises(ReviewAuthMissingError):
        auth.authenticate(None)
    for header in (f"Bearer {TOKEN[:-1]}", f"Token {TOKEN}", TOKEN):
        with pytest.raises(ReviewAuthInvalidError) as info:
            auth.authenticate(header)
        assert TOKEN not in str(info.value)
    assert TOKEN not in repr(auth)


def test_without_a_token_review_is_refused_unless_local_development_opted_in():
    with pytest.raises(ReviewAuthNotConfiguredError):
        ReviewAuth().authenticate(None)
    local = ReviewAuth(allow_unauthenticated=True)
    assert local.authenticate(None) is ReviewAuthMethod.UNAUTHENTICATED_LOCAL
    # a configured token always wins over the local escape hatch
    both = ReviewAuth(token=SecretStr(TOKEN), allow_unauthenticated=True)
    with pytest.raises(ReviewAuthMissingError):
        both.authenticate(None)


def test_settings_guard_the_escape_hatch_and_thresholds():
    dev = AppSettings(app_environment="development", strategy_review_allow_unauthenticated=True)
    assert ReviewAuth.from_settings(dev).allow_unauthenticated
    assert ReviewAuth.from_settings(AppSettings()).token is None
    with pytest.raises(ValidationError):
        AppSettings(strategy_review_allow_unauthenticated=True)
    with pytest.raises(ValidationError):
        AppSettings(eval_min_group_n=8, eval_min_compare_n=6)
    with pytest.raises(ValidationError):
        AppSettings(eval_length_bucket_edges="200,100")
    assert AppSettings(eval_length_bucket_edges="80, 160,240").length_bucket_edges() == (
        80,
        160,
        240,
    )
    assert AppSettings().strategy_proposer == "disabled"
