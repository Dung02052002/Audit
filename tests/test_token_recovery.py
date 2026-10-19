"""Tests for the publish token recovery (M-154, prompt #154)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from ai_youtube_agent.pipeline.token_recovery import (
    AuthorizationErrorCode,
    PublishAuthorizationError,
    TokenRecovery,
)
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthErrorCode,
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CHANNEL = "channel-1"
UPLOAD_SCOPES = (OAuthScope.YOUTUBE_UPLOAD, OAuthScope.YOUTUBE_READONLY)


def _clock() -> datetime:
    return NOW


def _provider(**options) -> MockYouTubeOAuthProvider:
    return MockYouTubeOAuthProvider(clock=_clock, **options)


def test_a_valid_token_is_returned_without_renewal() -> None:
    oauth = _provider()

    acquired = TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    assert acquired.renewed is False
    assert acquired.token.channel_id == CHANNEL
    assert acquired.token.has_scopes(UPLOAD_SCOPES)


def test_an_expired_token_is_renewed_once_and_checked() -> None:
    oauth = _provider()
    expired = oauth.expire_channel_token(CHANNEL)

    acquired = TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    assert acquired.renewed is True
    assert (
        acquired.token.access_token.get_secret_value()
        != expired.access_token.get_secret_value()
    )
    assert not acquired.token.is_expired(NOW)


@pytest.mark.parametrize("revoked", [True])
def test_a_revoked_token_needs_a_new_authorization(revoked: bool) -> None:
    oauth = _provider()
    oauth.revoke_token(CHANNEL)

    with pytest.raises(PublishAuthorizationError) as caught:
        TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    error = caught.value
    assert error.code == AuthorizationErrorCode.REAUTHORIZATION_REQUIRED.value
    assert error.reason == OAuthErrorCode.TOKEN_REVOKED.value
    assert error.channel_id == CHANNEL
    assert error.to_public().http_status == 409


def test_an_unauthorised_channel_needs_a_new_authorization() -> None:
    oauth = _provider(auto_provision=False)

    with pytest.raises(PublishAuthorizationError) as caught:
        TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    assert caught.value.reason == OAuthErrorCode.NOT_AUTHORIZED.value


def test_a_missing_scope_needs_a_new_authorization() -> None:
    oauth = _provider(default_scopes=[OAuthScope.YOUTUBE_READONLY])

    with pytest.raises(PublishAuthorizationError) as caught:
        TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    assert caught.value.reason == OAuthErrorCode.INSUFFICIENT_SCOPE.value


def test_an_expired_token_without_a_refresh_grant_needs_a_new_authorization() -> None:
    oauth = _provider()
    oauth.seed_token(
        OAuthToken(
            channel_id=CHANNEL,
            access_token=SecretStr("expired-access"),
            refresh_token=None,
            scopes=frozenset(UPLOAD_SCOPES),
            issued_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(minutes=5),
        )
    )

    with pytest.raises(PublishAuthorizationError) as caught:
        TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    assert caught.value.reason == OAuthErrorCode.INVALID_GRANT.value


@pytest.mark.parametrize(
    "code",
    [OAuthErrorCode.UNAVAILABLE, OAuthErrorCode.TIMEOUT, OAuthErrorCode.RATE_LIMITED],
)
def test_a_temporary_provider_problem_is_raised_unchanged(code: OAuthErrorCode) -> None:
    oauth = _provider()
    oauth.fail_next(code)

    with pytest.raises(YouTubeOAuthError) as caught:
        TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    assert caught.value.oauth_code is code
    assert caught.value.retryable is True


def test_renew_refuses_a_new_token_that_lacks_a_scope() -> None:
    oauth = _provider(default_scopes=[OAuthScope.YOUTUBE_READONLY])

    with pytest.raises(PublishAuthorizationError) as caught:
        TokenRecovery(oauth, clock=_clock).renew(CHANNEL, UPLOAD_SCOPES)

    assert caught.value.reason == OAuthErrorCode.INSUFFICIENT_SCOPE.value


def test_renew_returns_a_fresh_valid_token() -> None:
    oauth = _provider()
    first = oauth.get_token(CHANNEL)

    renewed = TokenRecovery(oauth, clock=_clock).renew(CHANNEL, UPLOAD_SCOPES)

    assert (
        renewed.access_token.get_secret_value() != first.access_token.get_secret_value()
    )
    assert renewed.has_scopes(UPLOAD_SCOPES)


def test_the_error_never_carries_a_token_value() -> None:
    oauth = _provider()
    token = oauth.get_token(CHANNEL)
    secret = token.access_token.get_secret_value()
    oauth.revoke_token(CHANNEL)

    with pytest.raises(PublishAuthorizationError) as caught:
        TokenRecovery(oauth, clock=_clock).acquire(CHANNEL, UPLOAD_SCOPES)

    error = caught.value
    for text in (str(error), error.detail, error.user_message, repr(error)):
        assert secret not in text
        assert "yt_" not in text
