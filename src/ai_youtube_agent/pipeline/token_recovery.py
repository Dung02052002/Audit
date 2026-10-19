"""Token recovery (Prompt Pack v8, prompt #154), context C12 Publishing.

``TokenRecovery`` handles expired, revoked or insufficient YouTube authorisation for
the publish executor, safely:

- ``acquire`` gets a token with the scopes an upload needs. An *expired* token is
  renewed once through the provider's refresh grant. Anything that only a human can
  fix (revoked, invalid grant, not authorised, missing scope) raises
  ``PublishAuthorizationError`` (409): the channel must be authorised again, and the
  error says so without naming any token. A temporary provider problem (unavailable,
  timeout, rate limited) is raised unchanged, because a retry may work.
- ``renew`` refreshes a token the provider or YouTube refused (for example an upload
  answered "unauthorised"), at most once per call site, and checks the new token
  (not expired, required scopes) before it is used.

Secrets: this module never reads, logs or stores a token value. Errors and audit
metadata carry the channel id and the OAuth error code only. Nothing is renewed
silently: ``acquire`` reports whether it renewed, so the executor can audit it.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Final

from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthErrorCode,
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
    YouTubeOAuthProvider,
)

Clock = Callable[[], datetime]

REAUTHORIZATION_CODES: Final[frozenset[OAuthErrorCode]] = frozenset(
    {
        OAuthErrorCode.TOKEN_REVOKED,
        OAuthErrorCode.INVALID_GRANT,
        OAuthErrorCode.NOT_AUTHORIZED,
        OAuthErrorCode.INSUFFICIENT_SCOPE,
    }
)
"""OAuth errors that a refresh cannot fix: the channel owner must authorise again."""


class AuthorizationErrorCode(StrEnum):
    REAUTHORIZATION_REQUIRED = "domain.publish_authorization_required"


class PublishAuthorizationError(DomainError):
    default_code = AuthorizationErrorCode.REAUTHORIZATION_REQUIRED.value
    default_user_message = (
        "YouTube authorization for this channel must be renewed. "
        "Authorize the channel again, then publish again."
    )
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, channel_id: str, reason: str) -> None:
        self.channel_id = channel_id
        self.reason = reason
        super().__init__(
            f"channel {channel_id} needs a new YouTube authorization ({reason})"
        )


@dataclass(frozen=True)
class AcquiredToken:
    token: OAuthToken
    renewed: bool


class TokenRecovery:
    def __init__(
        self, oauth_provider: YouTubeOAuthProvider, *, clock: Clock | None = None
    ) -> None:
        self._oauth = oauth_provider
        self._clock = clock

    def acquire(
        self, channel_id: str, required_scopes: Iterable[OAuthScope]
    ) -> AcquiredToken:
        scopes = tuple(required_scopes)
        try:
            return AcquiredToken(
                self._oauth.get_token(channel_id, required_scopes=scopes), False
            )
        except YouTubeOAuthError as error:
            if error.oauth_code is OAuthErrorCode.TOKEN_EXPIRED:
                return AcquiredToken(self.renew(channel_id, scopes), True)
            if error.oauth_code in REAUTHORIZATION_CODES:
                raise PublishAuthorizationError(
                    channel_id, error.oauth_code.value
                ) from error
            raise

    def renew(
        self, channel_id: str, required_scopes: Iterable[OAuthScope]
    ) -> OAuthToken:
        scopes = tuple(required_scopes)
        try:
            token = self._oauth.refresh_token(channel_id)
            token.require_valid(self._now(), required_scopes=scopes)
        except YouTubeOAuthError as error:
            if (
                error.oauth_code in REAUTHORIZATION_CODES
                or error.oauth_code is OAuthErrorCode.TOKEN_EXPIRED
            ):
                raise PublishAuthorizationError(
                    channel_id, error.oauth_code.value
                ) from error
            raise
        return token

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
