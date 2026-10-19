"""YouTube Upload provider interface (Prompt Pack v8, prompt #147), context C12 Publishing.

Defines the provider abstraction for uploading rendered MP4 video artifacts,
thumbnails, and caption tracks to YouTube:

- ``YouTubePrivacyStatus``: ``private``, ``unlisted``, ``public``.
- ``YouTubeVideoSnippet`` and ``YouTubeVideoStatus``: validated YouTube Data API
  v3 payload structures including explicit ``self_declared_made_for_kids`` and
  ``contains_synthetic_media`` flags.
- ``YouTubeUploadPayload``: complete upload envelope bound to the exact
  ``approval_request_id``, ``idempotency_key``, ``video_artifact_id``,
  ``video_sha256``, and optional thumbnail/subtitles artifacts.
- ``YouTubeUploadReceipt``: immutable confirmation carrying the 11-character
  ``youtube_video_id``, ``uploaded_at`` UTC timestamp, and upload flags.
- ``UploadErrorCode`` and ``YouTubeUploadError(ProviderError)``: typed provider
  errors with retryability flags and safe user-facing messages.
- ``YouTubeUploadProvider``: synchronous ``@runtime_checkable`` Protocol.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.core.artifact import SHA256_PATTERN
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.errors import ProviderError
from ai_youtube_agent.pipeline.publish import VIDEO_ID_PATTERN
from ai_youtube_agent.providers.youtube_channel import YOUTUBE_CHANNEL_ID_PATTERN
from ai_youtube_agent.providers.youtube_oauth import OAuthToken

CATEGORY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]{1,4}$")


class YouTubePrivacyStatus(StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class UploadErrorCode(StrEnum):
    UNAVAILABLE = "youtube_upload.unavailable"
    TIMEOUT = "youtube_upload.timeout"
    RATE_LIMITED = "youtube_upload.rate_limited"
    QUOTA_EXCEEDED = "youtube_upload.quota_exceeded"
    UNAUTHORIZED = "youtube_upload.unauthorized"
    INVALID_PAYLOAD = "youtube_upload.invalid_payload"
    INVALID_MEDIA = "youtube_upload.invalid_media"
    DUPLICATE_UPLOAD = "youtube_upload.duplicate_upload"


RETRYABLE_UPLOAD_CODES: Final[frozenset[UploadErrorCode]] = frozenset(
    {
        UploadErrorCode.UNAVAILABLE,
        UploadErrorCode.TIMEOUT,
        UploadErrorCode.RATE_LIMITED,
    }
)

USER_MESSAGES: Final[dict[UploadErrorCode, str]] = {
    UploadErrorCode.UNAVAILABLE: (
        "The YouTube upload service is temporarily unavailable."
    ),
    UploadErrorCode.TIMEOUT: "The YouTube video upload timed out.",
    UploadErrorCode.RATE_LIMITED: (
        "YouTube upload requests are currently rate-limited."
    ),
    UploadErrorCode.QUOTA_EXCEEDED: (
        "The YouTube API upload quota has been exceeded."
    ),
    UploadErrorCode.UNAUTHORIZED: (
        "The YouTube upload could not be authorized for this channel."
    ),
    UploadErrorCode.INVALID_PAYLOAD: (
        "The YouTube video metadata payload was rejected as invalid."
    ),
    UploadErrorCode.INVALID_MEDIA: (
        "The video, thumbnail or subtitle media bytes failed integrity validation."
    ),
    UploadErrorCode.DUPLICATE_UPLOAD: (
        "An upload with this idempotency key has already been completed."
    ),
}


class YouTubeUploadError(ProviderError):
    default_code = UploadErrorCode.UNAVAILABLE.value
    default_user_message = USER_MESSAGES[UploadErrorCode.UNAVAILABLE]

    def __init__(
        self,
        code: UploadErrorCode,
        detail: str,
        *,
        provider: str = "youtube_upload",
        cause: Exception | None = None,
    ) -> None:
        if not isinstance(code, UploadErrorCode):
            raise TypeError("code must be an UploadErrorCode")
        self.upload_code = code
        super().__init__(
            detail,
            provider=provider,
            code=code.value,
            user_message=USER_MESSAGES[code],
            retryable=code in RETRYABLE_UPLOAD_CODES,
        )
        self.__cause__ = cause


@dataclass(frozen=True)
class YouTubeVideoSnippet:
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str
    default_language: str
    default_audio_language: str

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not (1 <= len(self.title.strip()) <= 100):
            raise ValueError("title must be 1..100 characters")
        if "<" in self.title or ">" in self.title:
            raise ValueError("title must not contain '<' or '>'")
        if not isinstance(self.description, str) or len(self.description) > 5000:
            raise ValueError("description must be at most 5000 characters")
        if "<" in self.description or ">" in self.description:
            raise ValueError("description must not contain '<' or '>'")
        if not isinstance(self.tags, tuple) or len(self.tags) > 30:
            raise ValueError("tags must be a tuple of at most 30 strings")
        total_tag_chars = 0
        for tag in self.tags:
            if not isinstance(tag, str) or not (1 <= len(tag.strip()) <= 100):
                raise ValueError("each tag must be 1..100 characters")
            if "<" in tag or ">" in tag:
                raise ValueError("tags must not contain '<' or '>'")
            total_tag_chars += len(tag)
        if total_tag_chars > 500:
            raise ValueError("total tag characters must be at most 500")
        if (
            not isinstance(self.category_id, str)
            or not CATEGORY_ID_PATTERN.match(self.category_id)
        ):
            raise ValueError("category_id must be 1..4 digits")
        for lang_field in ("default_language", "default_audio_language"):
            val = getattr(self, lang_field)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{lang_field} must not be empty")

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "categoryId": self.category_id,
            "defaultLanguage": self.default_language,
            "defaultAudioLanguage": self.default_audio_language,
        }


@dataclass(frozen=True)
class YouTubeVideoStatus:
    privacy_status: YouTubePrivacyStatus
    self_declared_made_for_kids: bool
    contains_synthetic_media: bool
    embeddable: bool = True
    public_stats_viewable: bool = True
    publish_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.privacy_status, YouTubePrivacyStatus):
            raise TypeError("privacy_status must be a YouTubePrivacyStatus")
        for bool_name in (
            "self_declared_made_for_kids",
            "contains_synthetic_media",
            "embeddable",
            "public_stats_viewable",
        ):
            if not isinstance(getattr(self, bool_name), bool):
                raise TypeError(f"{bool_name} must be a bool")
        if self.publish_at is not None:
            if (
                not isinstance(self.publish_at, datetime)
                or self.publish_at.utcoffset() != timedelta(0)
            ):
                raise ValueError("publish_at must be timezone-aware UTC")
            if self.privacy_status is not YouTubePrivacyStatus.PRIVATE:
                raise ValueError(
                    "scheduled publish_at requires privacy_status to be PRIVATE"
                )

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "privacyStatus": self.privacy_status.value,
            "selfDeclaredMadeForKids": self.self_declared_made_for_kids,
            "containsSyntheticMedia": self.contains_synthetic_media,
            "embeddable": self.embeddable,
            "publicStatsViewable": self.public_stats_viewable,
        }
        if self.publish_at is not None:
            body["publishAt"] = self.publish_at.isoformat()
        return body


@dataclass(frozen=True)
class YouTubeUploadPayload:
    channel_id: str
    youtube_channel_id: str
    content_item_id: str
    content_type: ContentType
    approval_request_id: str
    idempotency_key: str
    video_artifact_id: str
    video_version: int
    video_sha256: str
    video_size_bytes: int
    video_media_type: str
    snippet: YouTubeVideoSnippet
    status: YouTubeVideoStatus
    metadata_artifact_id: str
    metadata_version: int
    metadata_sha256: str
    thumbnail_artifact_id: str | None = None
    thumbnail_sha256: str | None = None
    thumbnail_media_type: str | None = None
    subtitles_artifact_id: str | None = None
    subtitles_sha256: str | None = None
    subtitles_language: str | None = None
    disclosure_decision_id: str | None = None

    def __post_init__(self) -> None:
        for str_name in (
            "channel_id",
            "content_item_id",
            "approval_request_id",
            "idempotency_key",
            "video_artifact_id",
            "metadata_artifact_id",
        ):
            val = getattr(self, str_name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{str_name} must not be empty")
        if any(ch.isspace() for ch in self.idempotency_key):
            raise ValueError("idempotency_key must not contain whitespace")
        if not YOUTUBE_CHANNEL_ID_PATTERN.match(self.youtube_channel_id):
            raise ValueError("youtube_channel_id must match UC + 22 chars")
        if not isinstance(self.content_type, ContentType):
            raise TypeError("content_type must be a ContentType")
        for ver_name in ("video_version", "metadata_version"):
            ver = getattr(self, ver_name)
            if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
                raise ValueError(f"{ver_name} must be an integer >= 1")
        for sha_name in ("video_sha256", "metadata_sha256"):
            if not SHA256_PATTERN.match(getattr(self, sha_name)):
                raise ValueError(f"{sha_name} must be a 64-character lowercase hex sha256")
        if (
            isinstance(self.video_size_bytes, bool)
            or not isinstance(self.video_size_bytes, int)
            or self.video_size_bytes < 1
        ):
            raise ValueError("video_size_bytes must be a positive integer")
        if self.video_media_type != "video/mp4":
            raise ValueError("video_media_type must be 'video/mp4'")
        if not isinstance(self.snippet, YouTubeVideoSnippet):
            raise TypeError("snippet must be a YouTubeVideoSnippet")
        if not isinstance(self.status, YouTubeVideoStatus):
            raise TypeError("status must be a YouTubeVideoStatus")
        if (self.thumbnail_artifact_id is None) != (self.thumbnail_sha256 is None):
            raise ValueError(
                "thumbnail_artifact_id and thumbnail_sha256 must both be set or both None"
            )
        if self.thumbnail_sha256 is not None and not SHA256_PATTERN.match(
            self.thumbnail_sha256
        ):
            raise ValueError("thumbnail_sha256 must be a valid sha256")
        if (self.subtitles_artifact_id is None) != (self.subtitles_sha256 is None):
            raise ValueError(
                "subtitles_artifact_id and subtitles_sha256 must both be set or both None"
            )
        if self.subtitles_sha256 is not None and not SHA256_PATTERN.match(
            self.subtitles_sha256
        ):
            raise ValueError("subtitles_sha256 must be a valid sha256")

    def to_youtube_api_body(self) -> dict[str, Any]:
        return {
            "snippet": self.snippet.as_dict(),
            "status": self.status.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "youtube_channel_id": self.youtube_channel_id,
            "content_item_id": self.content_item_id,
            "content_type": self.content_type.value,
            "approval_request_id": self.approval_request_id,
            "idempotency_key": self.idempotency_key,
            "video_artifact_id": self.video_artifact_id,
            "video_version": self.video_version,
            "video_sha256": self.video_sha256,
            "video_size_bytes": self.video_size_bytes,
            "video_media_type": self.video_media_type,
            "metadata_artifact_id": self.metadata_artifact_id,
            "metadata_version": self.metadata_version,
            "metadata_sha256": self.metadata_sha256,
            "thumbnail_artifact_id": self.thumbnail_artifact_id,
            "thumbnail_sha256": self.thumbnail_sha256,
            "thumbnail_media_type": self.thumbnail_media_type,
            "subtitles_artifact_id": self.subtitles_artifact_id,
            "subtitles_sha256": self.subtitles_sha256,
            "subtitles_language": self.subtitles_language,
            "disclosure_decision_id": self.disclosure_decision_id,
            "youtube_api_body": self.to_youtube_api_body(),
        }


@dataclass(frozen=True)
class YouTubeUploadReceipt:
    youtube_video_id: str
    channel_id: str
    youtube_channel_id: str
    idempotency_key: str
    video_sha256: str
    privacy_status: YouTubePrivacyStatus
    contains_synthetic_media: bool
    thumbnail_uploaded: bool
    captions_uploaded: bool
    uploaded_at: datetime
    provider: str

    def __post_init__(self) -> None:
        if not VIDEO_ID_PATTERN.match(self.youtube_video_id):
            raise ValueError(
                "youtube_video_id must be 11 characters matching [A-Za-z0-9_-]{11}"
            )
        if not YOUTUBE_CHANNEL_ID_PATTERN.match(self.youtube_channel_id):
            raise ValueError("youtube_channel_id must match UC + 22 chars")
        if not SHA256_PATTERN.match(self.video_sha256):
            raise ValueError("video_sha256 must be a valid sha256")
        if not isinstance(self.privacy_status, YouTubePrivacyStatus):
            raise TypeError("privacy_status must be a YouTubePrivacyStatus")
        for bname in (
            "contains_synthetic_media",
            "thumbnail_uploaded",
            "captions_uploaded",
        ):
            if not isinstance(getattr(self, bname), bool):
                raise TypeError(f"{bname} must be a bool")
        if (
            not isinstance(self.uploaded_at, datetime)
            or self.uploaded_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("uploaded_at must be timezone-aware UTC")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must not be empty")

    def as_dict(self) -> dict[str, Any]:
        return {
            "youtube_video_id": self.youtube_video_id,
            "channel_id": self.channel_id,
            "youtube_channel_id": self.youtube_channel_id,
            "idempotency_key": self.idempotency_key,
            "video_sha256": self.video_sha256,
            "privacy_status": self.privacy_status.value,
            "contains_synthetic_media": self.contains_synthetic_media,
            "thumbnail_uploaded": self.thumbnail_uploaded,
            "captions_uploaded": self.captions_uploaded,
            "uploaded_at": self.uploaded_at.isoformat(),
            "provider": self.provider,
        }


@runtime_checkable
class YouTubeUploadProvider(Protocol):
    @property
    def name(self) -> str: ...

    def upload(
        self,
        payload: YouTubeUploadPayload,
        *,
        video_bytes: bytes,
        thumbnail_bytes: bytes | None = None,
        captions_bytes: bytes | None = None,
        token: OAuthToken | None = None,
    ) -> YouTubeUploadReceipt: ...

    def check(self) -> None: ...
