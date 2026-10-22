"""Deterministic in-memory mock YouTube OAuth provider (Prompt Pack v8, prompt #145),
context C12 Publishing.
"""

import hashlib
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from ai_youtube_agent.providers.youtube_oauth import (
    OAuthErrorCode,
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
)

Clock = Callable[[], datetime]

DEFAULT_MOCK_SCOPES = frozenset(
    {
        OAuthScope.YOUTUBE_READONLY,
        OAuthScope.YOUTUBE_UPLOAD,
        OAuthScope.YOUTUBE_FORCE_SSL,
        OAuthScope.YT_ANALYTICS_READONLY,
        OAuthScope.YT_ANALYTICS_MONETARY_READONLY,
    }
)


class MockYouTubeOAuthProvider:
    """Thread-safe in-memory OAuth token provider for development and tests."""

    def __init__(
        self,
        *,
        name: str = "mock_youtube_oauth",
        default_scopes: Iterable[OAuthScope] = DEFAULT_MOCK_SCOPES,
        token_ttl: timedelta = timedelta(hours=1),
        auto_provision: bool = True,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must not be empty")
        self._name = name.strip()
        self._default_scopes = frozenset(default_scopes)
        self._token_ttl = token_ttl
        self._auto_provision = auto_provision
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, OAuthToken] = {}
        self._revoked: set[str] = set()
        self._healthy = True
        self._next_error: OAuthErrorCode | None = None
        self._version_counter: dict[str, int] = {}

    @property
    def name(self) -> str:
        return self._name

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = bool(healthy)

    def fail_next(self, code: OAuthErrorCode) -> None:
        if not isinstance(code, OAuthErrorCode):
            raise TypeError("code must be an OAuthErrorCode")
        with self._lock:
            self._next_error = code

    def seed_token(
        self,
        token_or_channel_id: OAuthToken | str,
        *,
        scopes: Iterable[OAuthScope] | None = None,
        access_token: SecretStr | str | None = None,
        refresh_token: SecretStr | str | None = None,
        expires_at: datetime | None = None,
    ) -> OAuthToken:
        if isinstance(token_or_channel_id, OAuthToken):
            token = token_or_channel_id
        elif isinstance(token_or_channel_id, str):
            channel_id = token_or_channel_id
            now = self._now()
            resolved_scopes = (
                frozenset(scopes) if scopes is not None else self._default_scopes
            )
            raw_acc = (
                access_token.get_secret_value()
                if isinstance(access_token, SecretStr)
                else (access_token or self._digest("access", channel_id, 0))
            )
            raw_ref = (
                refresh_token.get_secret_value()
                if isinstance(refresh_token, SecretStr)
                else (refresh_token or self._digest("refresh", channel_id, 0))
            )
            token = OAuthToken(
                channel_id=channel_id,
                access_token=SecretStr(raw_acc),
                refresh_token=SecretStr(raw_ref),
                scopes=resolved_scopes,
                issued_at=now,
                expires_at=expires_at or (now + timedelta(hours=1)),
            )
        else:
            raise TypeError("token must be an OAuthToken or str")

        with self._lock:
            self._revoked.discard(token.channel_id)
            self._tokens[token.channel_id] = token
        return token

    def expire_channel_token(self, channel_id: str) -> OAuthToken:
        with self._lock:
            now = self._now()
            existing = self._tokens.get(channel_id)
            scopes = existing.scopes if existing else self._default_scopes
            expired = OAuthToken(
                channel_id=channel_id,
                access_token=SecretStr(self._digest("expired_access", channel_id, 0)),
                refresh_token=SecretStr(self._digest("refresh", channel_id, 0)),
                scopes=scopes,
                issued_at=now - timedelta(hours=2),
                expires_at=now - timedelta(minutes=5),
            )
            self._tokens[channel_id] = expired
            return expired

    def get_token(
        self,
        channel_id: str,
        *,
        required_scopes: Iterable[OAuthScope] = (),
    ) -> OAuthToken:
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("channel_id must not be empty")
        req_scopes = tuple(required_scopes)
        with self._lock:
            self._raise_if_injected()
            if channel_id in self._revoked:
                raise YouTubeOAuthError(
                    OAuthErrorCode.TOKEN_REVOKED,
                    f"OAuth token for channel {channel_id} was revoked",
                    provider=self._name,
                )
            now = self._now()
            token = self._tokens.get(channel_id)
            if token is None:
                if not self._auto_provision:
                    raise YouTubeOAuthError(
                        OAuthErrorCode.NOT_AUTHORIZED,
                        f"channel {channel_id} has no provisioned OAuth token",
                        provider=self._name,
                    )
                token = self._mint_token_locked(channel_id, now, self._default_scopes)
            token.require_valid(now, required_scopes=req_scopes, provider=self._name)
            return token

    def refresh_token(self, channel_id: str) -> OAuthToken:
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("channel_id must not be empty")
        with self._lock:
            self._raise_if_injected()
            if channel_id in self._revoked:
                raise YouTubeOAuthError(
                    OAuthErrorCode.TOKEN_REVOKED,
                    f"cannot refresh revoked token for channel {channel_id}",
                    provider=self._name,
                )
            existing = self._tokens.get(channel_id)
            if existing is not None and existing.refresh_token is None:
                raise YouTubeOAuthError(
                    OAuthErrorCode.INVALID_GRANT,
                    f"channel {channel_id} token has no refresh_token",
                    provider=self._name,
                )
            scopes = existing.scopes if existing is not None else self._default_scopes
            now = self._now()
            return self._mint_token_locked(channel_id, now, scopes)

    def revoke_token(self, channel_id: str) -> None:
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("channel_id must not be empty")
        with self._lock:
            self._raise_if_injected()
            self._tokens.pop(channel_id, None)
            self._revoked.add(channel_id)

    def check(self) -> None:
        with self._lock:
            if not self._healthy:
                raise YouTubeOAuthError(
                    OAuthErrorCode.UNAVAILABLE,
                    "mock YouTube OAuth provider marked unhealthy",
                    provider=self._name,
                )

    def _raise_if_injected(self) -> None:
        if self._next_error is not None:
            code = self._next_error
            self._next_error = None
            raise YouTubeOAuthError(
                code,
                f"injected OAuth failure ({code.value})",
                provider=self._name,
            )

    def _mint_token_locked(
        self,
        channel_id: str,
        now: datetime,
        scopes: frozenset[OAuthScope],
    ) -> OAuthToken:
        seq = self._version_counter.get(channel_id, 0) + 1
        self._version_counter[channel_id] = seq
        token = OAuthToken(
            channel_id=channel_id,
            access_token=SecretStr(self._digest("access", channel_id, seq)),
            refresh_token=SecretStr(self._digest("refresh", channel_id, seq)),
            scopes=scopes,
            issued_at=now,
            expires_at=now + self._token_ttl,
        )
        self._tokens[channel_id] = token
        return token

    @staticmethod
    def _digest(prefix: str, channel_id: str, seq: int) -> str:
        raw = f"{prefix}:{channel_id}:{seq}".encode()
        return f"yt_{prefix}_{hashlib.sha256(raw).hexdigest()[:32]}"
