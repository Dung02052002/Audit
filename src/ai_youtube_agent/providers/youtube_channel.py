"""YouTube Channel read provider interface (Prompt Pack v8, prompt #146), context C12 Publishing.

Defines the channel read provider abstraction for inspecting remote YouTube
channel metadata, linkage status, and upload eligibility:

- ``RemoteChannelInfo``: frozen value object holding ``youtube_channel_id``
  (``UC`` + 22 chars), ``title``, optional ``handle``, ``country``,
  ``default_language``, ``uploads_playlist_id``, ``privacy_status``,
  ``is_linked``, ``long_uploads_status``, and ``fetched_at`` UTC timestamp.
- ``ChannelProviderErrorCode`` and ``YouTubeChannelProviderError(ProviderError)``:
  typed provider errors with retryability flags and safe user-facing messages.
- ``YouTubeChannelProvider``: synchronous ``@runtime_checkable`` Protocol for
  reading channel information and verifying channel access under an
  ``OAuthToken``.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.providers.youtube_oauth import OAuthToken

YOUTUBE_CHANNEL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^UC[A-Za-z0-9_-]{22}$"
)
YOUTUBE_HANDLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^@[A-Za-z0-9._-]{3,30}$"
)
VALID_LONG_UPLOADS_STATUSES: Final[frozenset[str]] = frozenset(
    {"allowed", "eligible", "disallowed"}
)
VALID_PRIVACY_STATUSES: Final[frozenset[str]] = frozenset(
    {"public", "unlisted", "private"}
)


class ChannelProviderErrorCode(StrEnum):
    UNAVAILABLE = "youtube_channel.unavailable"
    TIMEOUT = "youtube_channel.timeout"
    RATE_LIMITED = "youtube_channel.rate_limited"
    NOT_FOUND = "youtube_channel.not_found"
    UNAUTHORIZED = "youtube_channel.unauthorized"
    INVALID_REQUEST = "youtube_channel.invalid_request"


RETRYABLE_CHANNEL_CODES: Final[frozenset[ChannelProviderErrorCode]] = frozenset(
    {
        ChannelProviderErrorCode.UNAVAILABLE,
        ChannelProviderErrorCode.TIMEOUT,
        ChannelProviderErrorCode.RATE_LIMITED,
    }
)

USER_MESSAGES: Final[dict[ChannelProviderErrorCode, str]] = {
    ChannelProviderErrorCode.UNAVAILABLE: (
        "The YouTube channel service is temporarily unavailable."
    ),
    ChannelProviderErrorCode.TIMEOUT: "The YouTube channel read request timed out.",
    ChannelProviderErrorCode.RATE_LIMITED: (
        "YouTube channel requests are currently rate-limited."
    ),
    ChannelProviderErrorCode.NOT_FOUND: (
        "The requested YouTube channel was not found."
    ),
    ChannelProviderErrorCode.UNAUTHORIZED: (
        "The YouTube channel could not be accessed with the current authorization."
    ),
    ChannelProviderErrorCode.INVALID_REQUEST: (
        "The YouTube channel identifier is invalid."
    ),
}


class YouTubeChannelProviderError(ProviderError):
    default_code = ChannelProviderErrorCode.UNAVAILABLE.value
    default_user_message = USER_MESSAGES[ChannelProviderErrorCode.UNAVAILABLE]

    def __init__(
        self,
        code: ChannelProviderErrorCode,
        detail: str,
        *,
        provider: str = "youtube_channel",
        cause: Exception | None = None,
    ) -> None:
        if not isinstance(code, ChannelProviderErrorCode):
            raise TypeError("code must be a ChannelProviderErrorCode")
        self.channel_code = code
        super().__init__(
            detail,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code in RETRYABLE_CHANNEL_CODES,
        )
        self.__cause__ = cause


@dataclass(frozen=True)
class RemoteChannelInfo:
    youtube_channel_id: str
    title: str
    fetched_at: datetime
    handle: str | None = None
    country: str | None = None
    default_language: str | None = None
    uploads_playlist_id: str | None = None
    privacy_status: str = "public"
    is_linked: bool = True
    long_uploads_status: str = "allowed"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.youtube_channel_id, str)
            or not YOUTUBE_CHANNEL_ID_PATTERN.match(self.youtube_channel_id)
        ):
            raise ValueError(
                "youtube_channel_id must be 'UC' followed by 22 letters, digits, '_' or '-'"
            )
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title must not be empty")
        object.__setattr__(self, "title", self.title.strip())
        if self.handle is not None:
            if not isinstance(self.handle, str) or not YOUTUBE_HANDLE_PATTERN.match(
                self.handle
            ):
                raise ValueError("handle must match @handle pattern")
        if self.privacy_status not in VALID_PRIVACY_STATUSES:
            raise ValueError(
                f"privacy_status must be one of {sorted(VALID_PRIVACY_STATUSES)}"
            )
        if not isinstance(self.is_linked, bool):
            raise TypeError("is_linked must be a bool")
        if self.long_uploads_status not in VALID_LONG_UPLOADS_STATUSES:
            raise ValueError(
                f"long_uploads_status must be one of {sorted(VALID_LONG_UPLOADS_STATUSES)}"
            )
        if (
            not isinstance(self.fetched_at, datetime)
            or self.fetched_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("fetched_at must be timezone-aware UTC")

    @property
    def allows_long_uploads(self) -> bool:
        return self.long_uploads_status in ("allowed", "eligible")

    def as_dict(self) -> dict[str, Any]:
        return {
            "youtube_channel_id": self.youtube_channel_id,
            "title": self.title,
            "handle": self.handle,
            "country": self.country,
            "default_language": self.default_language,
            "uploads_playlist_id": self.uploads_playlist_id,
            "privacy_status": self.privacy_status,
            "is_linked": self.is_linked,
            "long_uploads_status": self.long_uploads_status,
            "allows_long_uploads": self.allows_long_uploads,
            "fetched_at": self.fetched_at.isoformat(),
        }


@runtime_checkable
class YouTubeChannelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def fetch_channel(
        self,
        youtube_channel_id: str,
        *,
        token: OAuthToken | None = None,
    ) -> RemoteChannelInfo: ...

    def verify_channel_access(
        self,
        channel_id: str,
        youtube_channel_id: str,
        *,
        token: OAuthToken | None = None,
    ) -> RemoteChannelInfo: ...

    def check(self) -> None: ...
