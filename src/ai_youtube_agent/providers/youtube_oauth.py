"""YouTube OAuth / token provider contract.

(Prompt Pack v8, prompt #145), context C12 Publishing.

Defines the secure OAuth/token interface for YouTube operations:

- ``OAuthScope``: closed enum of Google/YouTube OAuth 2.0 scopes.
- ``OAuthToken``: frozen value object holding ``access_token`` and optional
  ``refresh_token`` as ``SecretStr`` values so credentials are never exposed in
  ``repr()``, ``str()``, or logs; supports ``is_expired``, ``has_scopes``,
  ``require_valid``, and ``redacted_dict``.
- ``OAuthErrorCode`` and ``YouTubeOAuthError(ProviderError)``: typed provider
  errors with retryability flags and safe user-facing messages that never echo
  tokens or secrets.
- ``YouTubeOAuthProvider``: synchronous ``@runtime_checkable`` Protocol for
  retrieving, refreshing, revoking, and health-checking channel OAuth tokens.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import SecretStr

from ai_youtube_agent.core.errors import ProviderError


class OAuthScope(StrEnum):
    YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
    YOUTUBE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
    YOUTUBE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
    YT_ANALYTICS_READONLY = "https://www.googleapis.com/auth/yt-analytics.readonly"
    YT_ANALYTICS_MONETARY_READONLY = (
        "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"
    )


class OAuthErrorCode(StrEnum):
    UNAVAILABLE = "oauth.unavailable"
    TIMEOUT = "oauth.timeout"
    RATE_LIMITED = "oauth.rate_limited"
    TOKEN_EXPIRED = "oauth.token_expired"
    TOKEN_REVOKED = "oauth.token_revoked"
    INVALID_GRANT = "oauth.invalid_grant"
    INSUFFICIENT_SCOPE = "oauth.insufficient_scope"
    NOT_AUTHORIZED = "oauth.not_authorized"


RETRYABLE_OAUTH_CODES: Final[frozenset[OAuthErrorCode]] = frozenset(
    {
        OAuthErrorCode.UNAVAILABLE,
        OAuthErrorCode.TIMEOUT,
        OAuthErrorCode.RATE_LIMITED,
    }
)

USER_MESSAGES: Final[dict[OAuthErrorCode, str]] = {
    OAuthErrorCode.UNAVAILABLE: (
        "The YouTube authorization service is temporarily unavailable."
    ),
    OAuthErrorCode.TIMEOUT: "The YouTube authorization request timed out.",
    OAuthErrorCode.RATE_LIMITED: (
        "YouTube authorization requests are currently rate-limited."
    ),
    OAuthErrorCode.TOKEN_EXPIRED: (
        "The YouTube access token has expired and needs to be refreshed."
    ),
    OAuthErrorCode.TOKEN_REVOKED: (
        "YouTube authorization for this channel was revoked; re-authorize the channel."
    ),
    OAuthErrorCode.INVALID_GRANT: (
        "The YouTube refresh grant is invalid or expired; re-authorize the channel."
    ),
    OAuthErrorCode.INSUFFICIENT_SCOPE: (
        "The YouTube authorization lacks the required permissions for this action."
    ),
    OAuthErrorCode.NOT_AUTHORIZED: (
        "This channel has not been authorized with YouTube OAuth."
    ),
}


class YouTubeOAuthError(ProviderError):
    default_code = OAuthErrorCode.UNAVAILABLE.value
    default_user_message = USER_MESSAGES[OAuthErrorCode.UNAVAILABLE]

    def __init__(
        self,
        code: OAuthErrorCode,
        detail: str,
        *,
        provider: str = "youtube_oauth",
        cause: Exception | None = None,
    ) -> None:
        if not isinstance(code, OAuthErrorCode):
            raise TypeError("code must be an OAuthErrorCode")
        self.oauth_code = code
        super().__init__(
            detail,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code in RETRYABLE_OAUTH_CODES,
        )
        self.__cause__ = cause


@dataclass(frozen=True)
class OAuthToken:
    channel_id: str
    access_token: SecretStr
    refresh_token: SecretStr | None
    scopes: frozenset[OAuthScope]
    issued_at: datetime
    expires_at: datetime
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        if not isinstance(self.channel_id, str) or not self.channel_id.strip():
            raise ValueError("channel_id must not be empty")
        if not isinstance(self.access_token, SecretStr):
            raise TypeError("access_token must be a SecretStr")
        raw_access = self.access_token.get_secret_value()
        if not raw_access or any(ch.isspace() for ch in raw_access):
            raise ValueError(
                "access_token must be a non-empty token without whitespace"
            )
        if self.refresh_token is not None:
            if not isinstance(self.refresh_token, SecretStr):
                raise TypeError("refresh_token must be a SecretStr or None")
            raw_refresh = self.refresh_token.get_secret_value()
            if not raw_refresh or any(ch.isspace() for ch in raw_refresh):
                raise ValueError(
                    "refresh_token must be a non-empty token without whitespace"
                )
        if not isinstance(self.scopes, frozenset) or not self.scopes:
            raise ValueError("scopes must be a non-empty frozenset of OAuthScope")
        if any(not isinstance(scope, OAuthScope) for scope in self.scopes):
            raise TypeError("scopes must contain OAuthScope values")
        if not isinstance(
            self.issued_at, datetime
        ) or self.issued_at.utcoffset() != timedelta(0):
            raise ValueError("issued_at must be timezone-aware UTC")
        if not isinstance(
            self.expires_at, datetime
        ) or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("expires_at must be timezone-aware UTC")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be strictly after issued_at")
        if not isinstance(self.token_type, str) or self.token_type != "Bearer":
            raise ValueError("token_type must be 'Bearer'")

    def is_expired(self, now: datetime, *, skew_seconds: int = 60) -> bool:
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("now must be timezone-aware UTC")
        if (
            isinstance(skew_seconds, bool)
            or not isinstance(skew_seconds, int)
            or skew_seconds < 0
        ):
            raise ValueError("skew_seconds must be a non-negative integer")
        return now + timedelta(seconds=skew_seconds) >= self.expires_at

    def has_scopes(self, required: Iterable[OAuthScope]) -> bool:
        req_set = set(required)
        if any(not isinstance(s, OAuthScope) for s in req_set):
            raise TypeError("required scopes must be OAuthScope values")
        return req_set.issubset(self.scopes)

    def require_valid(
        self,
        now: datetime,
        *,
        required_scopes: Iterable[OAuthScope] = (),
        skew_seconds: int = 60,
        provider: str = "youtube_oauth",
    ) -> None:
        if self.is_expired(now, skew_seconds=skew_seconds):
            raise YouTubeOAuthError(
                OAuthErrorCode.TOKEN_EXPIRED,
                f"OAuth token for channel {self.channel_id} expired at "
                f"{self.expires_at.isoformat()}",
                provider=provider,
            )
        req_tuple = tuple(required_scopes)
        if not self.has_scopes(req_tuple):
            missing = sorted(s.value for s in set(req_tuple) - self.scopes)
            raise YouTubeOAuthError(
                OAuthErrorCode.INSUFFICIENT_SCOPE,
                f"OAuth token for channel {self.channel_id} is missing scopes: "
                f"{', '.join(missing)}",
                provider=provider,
            )

    def redacted_dict(self) -> dict[str, Any]:
        """Return safe metadata.

        Never exposes access or refresh token secrets.
        """
        return {
            "channel_id": self.channel_id,
            "token_type": self.token_type,
            "scopes": sorted(s.value for s in self.scopes),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "has_refresh_token": self.refresh_token is not None,
        }


@runtime_checkable
class YouTubeOAuthProvider(Protocol):
    @property
    def name(self) -> str: ...

    def get_token(
        self,
        channel_id: str,
        *,
        required_scopes: Iterable[OAuthScope] = (),
    ) -> OAuthToken: ...

    def refresh_token(self, channel_id: str) -> OAuthToken: ...

    def revoke_token(self, channel_id: str) -> None: ...

    def check(self) -> None: ...


def get_or_renew_token(
    oauth: YouTubeOAuthProvider,
    channel_id: str,
    *,
    required_scopes: Iterable[OAuthScope] = (),
) -> OAuthToken:
    """Return a valid token, renewing an expired one once through the refresh grant.

    Only ``TOKEN_EXPIRED`` triggers a refresh; the renewed token is checked for the
    required scopes. Every other OAuth error propagates unchanged.
    """
    scopes = tuple(required_scopes)
    try:
        return oauth.get_token(channel_id, required_scopes=scopes)
    except YouTubeOAuthError as error:
        if error.oauth_code is not OAuthErrorCode.TOKEN_EXPIRED:
            raise
    token = oauth.refresh_token(channel_id)
    if not token.has_scopes(scopes):
        missing = sorted(s.value for s in set(scopes) - token.scopes)
        raise YouTubeOAuthError(
            OAuthErrorCode.INSUFFICIENT_SCOPE,
            f"OAuth token for channel {channel_id} is missing scopes: "
            f"{', '.join(missing)}",
            provider=oauth.name,
        )
    return token
