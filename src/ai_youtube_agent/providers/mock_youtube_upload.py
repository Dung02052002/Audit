"""Deterministic in-memory mock YouTube Upload provider (Prompt Pack v8, prompt #147),
context C12 Publishing.

Never connects to the network or uploads real videos to YouTube. Verifies OAuth
token scopes, checks media byte sizes and SHA-256 digests against the payload,
and produces a deterministic 11-character ``youtube_video_id``.
"""

import base64
import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    OAuthToken,
    YouTubeOAuthError,
)
from ai_youtube_agent.providers.youtube_upload import (
    UploadErrorCode,
    YouTubeUploadError,
    YouTubeUploadPayload,
    YouTubeUploadReceipt,
)

Clock = Callable[[], datetime]


def deterministic_youtube_video_id(idempotency_key: str, video_sha256: str) -> str:
    """Derive a deterministic 11-character YouTube video ID from key and sha256."""
    digest = hashlib.sha256(f"{idempotency_key}:{video_sha256}".encode()).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return encoded[:11]


class MockYouTubeUploadProvider:
    """Thread-safe deterministic mock upload provider for tests and local runs."""

    def __init__(
        self,
        *,
        name: str = "mock_youtube_upload",
        reject_duplicates: bool = False,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must not be empty")
        self._name = name.strip()
        self._reject_duplicates = reject_duplicates
        self._clock = clock
        self._lock = threading.Lock()
        self._uploads: list[tuple[YouTubeUploadPayload, YouTubeUploadReceipt]] = []
        self._by_key: dict[str, YouTubeUploadReceipt] = {}
        self._healthy = True
        self._failures: list[UploadErrorCode] = []
        self._calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def uploads(self) -> tuple[tuple[YouTubeUploadPayload, YouTubeUploadReceipt], ...]:
        with self._lock:
            return tuple(self._uploads)

    @property
    def calls(self) -> int:
        """How many times `upload` was called (successes, failures and replays)."""
        with self._lock:
            return self._calls

    def clear(self) -> None:
        with self._lock:
            self._uploads.clear()
            self._by_key.clear()
            self._failures.clear()
            self._calls = 0

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = bool(healthy)

    def fail_next(self, code: UploadErrorCode, times: int = 1) -> None:
        """Make the next `times` upload calls raise `YouTubeUploadError(code)`."""
        if not isinstance(code, UploadErrorCode):
            raise TypeError("code must be an UploadErrorCode")
        if isinstance(times, bool) or not isinstance(times, int) or times < 1:
            raise ValueError("times must be a whole number of 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def upload(
        self,
        payload: YouTubeUploadPayload,
        *,
        video_bytes: bytes,
        thumbnail_bytes: bytes | None = None,
        captions_bytes: bytes | None = None,
        token: OAuthToken | None = None,
    ) -> YouTubeUploadReceipt:
        if not isinstance(payload, YouTubeUploadPayload):
            raise TypeError("payload must be a YouTubeUploadPayload")
        if not isinstance(video_bytes, bytes):
            raise TypeError("video_bytes must be bytes")

        now = self._now()
        if token is not None:
            try:
                token.require_valid(
                    now,
                    required_scopes=(OAuthScope.YOUTUBE_UPLOAD,),
                    provider=self._name,
                )
            except YouTubeOAuthError as exc:
                raise YouTubeUploadError(
                    UploadErrorCode.UNAUTHORIZED,
                    f"upload token validation failed: {exc.code}",
                    provider=self._name,
                    cause=exc,
                ) from exc
            if token.channel_id != payload.channel_id:
                raise YouTubeUploadError(
                    UploadErrorCode.UNAUTHORIZED,
                    f"token belongs to channel {token.channel_id}, not "
                    f"{payload.channel_id}",
                    provider=self._name,
                )

        if len(video_bytes) != payload.video_size_bytes:
            raise YouTubeUploadError(
                UploadErrorCode.INVALID_MEDIA,
                f"video_bytes length {len(video_bytes)} does not match "
                f"payload.video_size_bytes {payload.video_size_bytes}",
                provider=self._name,
            )
        actual_video_sha = hashlib.sha256(video_bytes).hexdigest()
        if actual_video_sha != payload.video_sha256:
            raise YouTubeUploadError(
                UploadErrorCode.INVALID_MEDIA,
                "video_bytes sha256 does not match payload.video_sha256",
                provider=self._name,
            )

        if payload.thumbnail_sha256 is not None:
            if thumbnail_bytes is None:
                raise YouTubeUploadError(
                    UploadErrorCode.INVALID_MEDIA,
                    "thumbnail_bytes required when thumbnail_sha256 is set on payload",
                    provider=self._name,
                )
            if hashlib.sha256(thumbnail_bytes).hexdigest() != payload.thumbnail_sha256:
                raise YouTubeUploadError(
                    UploadErrorCode.INVALID_MEDIA,
                    "thumbnail_bytes sha256 does not match payload.thumbnail_sha256",
                    provider=self._name,
                )

        if payload.subtitles_sha256 is not None:
            if captions_bytes is None:
                raise YouTubeUploadError(
                    UploadErrorCode.INVALID_MEDIA,
                    "captions_bytes required when subtitles_sha256 is set on payload",
                    provider=self._name,
                )
            if hashlib.sha256(captions_bytes).hexdigest() != payload.subtitles_sha256:
                raise YouTubeUploadError(
                    UploadErrorCode.INVALID_MEDIA,
                    "captions_bytes sha256 does not match payload.subtitles_sha256",
                    provider=self._name,
                )

        with self._lock:
            self._calls += 1
            if self._failures:
                code = self._failures.pop(0)
                raise YouTubeUploadError(
                    code,
                    f"injected upload failure ({code.value})",
                    provider=self._name,
                )

            existing = self._by_key.get(payload.idempotency_key)
            if existing is not None:
                if self._reject_duplicates:
                    raise YouTubeUploadError(
                        UploadErrorCode.DUPLICATE_UPLOAD,
                        f"duplicate upload for key {payload.idempotency_key}",
                        provider=self._name,
                    )
                return existing

            yt_id = deterministic_youtube_video_id(
                payload.idempotency_key, payload.video_sha256
            )
            receipt = YouTubeUploadReceipt(
                youtube_video_id=yt_id,
                channel_id=payload.channel_id,
                youtube_channel_id=payload.youtube_channel_id,
                idempotency_key=payload.idempotency_key,
                video_sha256=payload.video_sha256,
                privacy_status=payload.status.privacy_status,
                contains_synthetic_media=payload.status.contains_synthetic_media,
                thumbnail_uploaded=thumbnail_bytes is not None,
                captions_uploaded=captions_bytes is not None,
                uploaded_at=now,
                provider=self._name,
            )
            self._uploads.append((payload, receipt))
            self._by_key[payload.idempotency_key] = receipt
            return receipt

    def check(self) -> None:
        with self._lock:
            if not self._healthy:
                raise YouTubeUploadError(
                    UploadErrorCode.UNAVAILABLE,
                    "mock YouTube upload provider marked unhealthy",
                    provider=self._name,
                )
