"""Scene & Shot Video Clip Generation Provider Contract (Script-to-Film Refactor, Phase 5).

Defines the ``VideoClipGenerator`` protocol, ``VideoClipGenerationRequest``,
``GeneratedVideoClip``, and ``VideoClipGeneratorCapabilities`` for
``ProductionMode.AI_FILM``.

Crucial honesty rule:
- ``ProductionMode.AI_FILM`` requires real motion video clips per shot/scene.
- When no real video-clip generator is configured and no user video clips are
  attached, ``VideoClipGenerator.capabilities().available`` is ``False`` and
  the orchestrator refuses ``AI_FILM`` with a clear error message recommending
  ``STORY_VIDEO`` mode instead of faking ``AI_FILM`` with still images.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.core.errors import DomainError

MP4_CLIP_MEDIA_TYPE: Final[str] = "video/mp4"


class VideoClipErrorCode(StrEnum):
    INVALID_REQUEST = "video_clip_gen.invalid_request"
    UNAVAILABLE = "video_clip_gen.unavailable"
    NOT_CONFIGURED = "video_clip_gen.not_configured"
    GENERATION_FAILED = "video_clip_gen.failed"


USER_MESSAGES: Final[dict[VideoClipErrorCode, str]] = {
    VideoClipErrorCode.INVALID_REQUEST: (
        "The video clip generation request is invalid."
    ),
    VideoClipErrorCode.UNAVAILABLE: (
        "The AI video clip generation provider is currently unavailable."
    ),
    VideoClipErrorCode.NOT_CONFIGURED: (
        "No AI video clip generation provider is configured for AI_FILM mode. "
        "Switch to STORY_VIDEO mode or attach video clip assets."
    ),
    VideoClipErrorCode.GENERATION_FAILED: (
        "The AI video clip generation provider failed to produce a video clip."
    ),
}


class VideoClipGenerationError(DomainError):
    """Raised when a ``VideoClipGenerator`` cannot fulfill a clip request."""

    default_code = VideoClipErrorCode.GENERATION_FAILED.value
    default_user_message = USER_MESSAGES[VideoClipErrorCode.GENERATION_FAILED]
    default_http_status = HTTPStatus.BAD_GATEWAY

    def __init__(
        self,
        code: VideoClipErrorCode,
        detail: str = "",
        *,
        provider: str = "unconfigured",
    ) -> None:
        super().__init__(
            detail or USER_MESSAGES[code],
            code=code.value,
            user_message=USER_MESSAGES[code],
        )
        self.error_code = code
        self.provider = provider
        if code is VideoClipErrorCode.INVALID_REQUEST:
            self.default_http_status = HTTPStatus.BAD_REQUEST
        elif code in (
            VideoClipErrorCode.UNAVAILABLE,
            VideoClipErrorCode.NOT_CONFIGURED,
        ):
            self.default_http_status = HTTPStatus.SERVICE_UNAVAILABLE


@dataclass(frozen=True)
class VideoClipGenerationRequest:
    """Parameters for generating one AI motion video clip for a scene/shot."""

    scene_id: str
    shot_id: str
    prompt: str
    duration_ms: int
    width: int
    height: int
    fps: int = 30
    camera_motion: str = "static"
    character_ids: tuple[str, ...] = ()
    location_id: str = "loc_default_1"

    def __post_init__(self) -> None:
        if not self.scene_id.strip():
            raise ValueError("scene_id must not be empty")
        if not self.shot_id.strip():
            raise ValueError("shot_id must not be empty")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if self.duration_ms <= 0:
            raise ValueError("duration_ms must be > 0")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be > 0")
        if self.fps <= 0:
            raise ValueError("fps must be > 0")

    @property
    def prompt_hash(self) -> str:
        payload = (
            f"{self.scene_id}|{self.shot_id}|{self.camera_motion}|"
            f"{self.location_id}|{','.join(self.character_ids)}|"
            f"{self.width}x{self.height}@{self.fps}|{self.duration_ms}|"
            f"{self.prompt.strip()}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GeneratedVideoClip:
    """A generated motion video clip and its provenance metadata."""

    data: bytes
    media_type: str
    duration_ms: int
    width: int
    height: int
    fps: int
    sha256: str
    provider: str
    model_name: str
    prompt_hash: str
    generated_at: datetime


@dataclass(frozen=True)
class VideoClipGeneratorCapabilities:
    """Reported capabilities and availability of a ``VideoClipGenerator``."""

    provider_name: str
    model_name: str
    available: bool
    is_mock: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "available": self.available,
            "is_mock": self.is_mock,
            "reason": self.reason,
        }


@runtime_checkable
class VideoClipGenerator(Protocol):
    """Protocol for AI motion video clip generation providers."""

    name: str

    def capabilities(self) -> VideoClipGeneratorCapabilities: ...

    def generate_clip(
        self, request: VideoClipGenerationRequest
    ) -> GeneratedVideoClip: ...

    def check(self) -> None: ...

