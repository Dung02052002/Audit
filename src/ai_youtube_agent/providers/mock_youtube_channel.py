"""Deterministic in-memory mock YouTube Channel read provider (Prompt Pack v8, prompt #146), context C12 Publishing."""

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from ai_youtube_agent.providers.youtube_channel import (
    YOUTUBE_CHANNEL_ID_PATTERN,
    ChannelProviderErrorCode,
    RemoteChannelInfo,
    YouTubeChannelProviderError,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
    YouTubeOAuthProvider,
)

Clock = Callable[[], datetime]


class MockYouTubeChannelProvider:
    """Thread-safe deterministic mock channel read provider."""

    def __init__(
        self,
        *,
        name: str = "mock_youtube_channel",
        oauth_provider: YouTubeOAuthProvider | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must not be empty")
        self._name = name.strip()
        self._oauth = oauth_provider
        self._clock = clock
        self._lock = threading.Lock()
        self._channels: dict[str, RemoteChannelInfo] = {}
        self._healthy = True
        self._next_error: ChannelProviderErrorCode | None = None

    @property
    def name(self) -> str:
        return self._name

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = bool(healthy)

    def fail_next(self, code: ChannelProviderErrorCode) -> None:
        if not isinstance(code, ChannelProviderErrorCode):
            raise TypeError("code must be a ChannelProviderErrorCode")
        with self._lock:
            self._next_error = code

    def register_channel(self, info: RemoteChannelInfo) -> None:
        if not isinstance(info, RemoteChannelInfo):
            raise TypeError("info must be a RemoteChannelInfo")
        with self._lock:
            self._channels[info.youtube_channel_id] = info

    def fetch_channel(
        self,
        youtube_channel_id: str,
        *,
        token: OAuthToken | None = None,
    ) -> RemoteChannelInfo:
        if (
            not isinstance(youtube_channel_id, str)
            or not YOUTUBE_CHANNEL_ID_PATTERN.match(youtube_channel_id)
        ):
            raise YouTubeChannelProviderError(
                ChannelProviderErrorCode.INVALID_REQUEST,
                "invalid YouTube channel ID format",
                provider=self._name,
            )
        now = self._now()
        if token is not None:
            try:
                token.require_valid(
                    now,
                    required_scopes=(OAuthScope.YOUTUBE_READONLY,),
                    provider=self._name,
                )
            except YouTubeOAuthError as exc:
                raise YouTubeChannelProviderError(
                    ChannelProviderErrorCode.UNAUTHORIZED,
                    f"token validation failed: {exc.code}",
                    provider=self._name,
                    cause=exc,
                ) from exc

        with self._lock:
            self._raise_if_injected()
            seeded = self._channels.get(youtube_channel_id)
            if seeded is not None:
                return seeded
            uploads_playlist = "UU" + youtube_channel_id[2:]
            return RemoteChannelInfo(
                youtube_channel_id=youtube_channel_id,
                title=f"Channel {youtube_channel_id[:8]}",
                handle=f"@ch_{youtube_channel_id[2:12].lower()}",
                country="US",
                default_language="en",
                uploads_playlist_id=uploads_playlist,
                privacy_status="public",
                is_linked=True,
                long_uploads_status="allowed",
                fetched_at=now,
            )

    def verify_channel_access(
        self,
        channel_id: str,
        youtube_channel_id: str,
        *,
        token: OAuthToken | None = None,
    ) -> RemoteChannelInfo:
        active_token = token
        if active_token is None and self._oauth is not None:
            try:
                active_token = self._oauth.get_token(
                    channel_id,
                    required_scopes=(OAuthScope.YOUTUBE_READONLY,),
                )
            except YouTubeOAuthError as exc:
                raise YouTubeChannelProviderError(
                    ChannelProviderErrorCode.UNAUTHORIZED,
                    f"could not obtain OAuth token for channel {channel_id}: {exc.code}",
                    provider=self._name,
                    cause=exc,
                ) from exc
        if active_token is not None and active_token.channel_id != channel_id:
            raise YouTubeChannelProviderError(
                ChannelProviderErrorCode.UNAUTHORIZED,
                f"token belongs to channel {active_token.channel_id}, not {channel_id}",
                provider=self._name,
            )
        return self.fetch_channel(youtube_channel_id, token=active_token)

    def check(self) -> None:
        with self._lock:
            if not self._healthy:
                raise YouTubeChannelProviderError(
                    ChannelProviderErrorCode.UNAVAILABLE,
                    "mock YouTube channel provider marked unhealthy",
                    provider=self._name,
                )

    def _raise_if_injected(self) -> None:
        if self._next_error is not None:
            code = self._next_error
            self._next_error = None
            raise YouTubeChannelProviderError(
                code,
                f"injected channel provider failure ({code.value})",
                provider=self._name,
            )
