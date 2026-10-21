"""Scene & Shot Image Generation Provider Contract (Script-to-Film Refactor, Phase 5).

Defines the ``ImageGenerator`` protocol, ``ImageGenerationRequest``,
``GeneratedImage``, and ``ImageGeneratorCapabilities`` used by the
Script-to-Video orchestrator to synthesize or generate visual scene/shot
illustrations for ``ProductionMode.STORY_VIDEO``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.core.errors import DomainError

PNG_MEDIA_TYPE: Final[str] = "image/png"
MIN_IMAGE_DIMENSION: Final[int] = 640
MAX_IMAGE_DIMENSION: Final[int] = 4096


class ImageGeneratorErrorCode(StrEnum):
    INVALID_REQUEST = "image_gen.invalid_request"
    UNAVAILABLE = "image_gen.unavailable"
    NOT_CONFIGURED = "image_gen.not_configured"
    GENERATION_FAILED = "image_gen.failed"


USER_MESSAGES: Final[dict[ImageGeneratorErrorCode, str]] = {
    ImageGeneratorErrorCode.INVALID_REQUEST: (
        "The scene image generation request is invalid."
    ),
    ImageGeneratorErrorCode.UNAVAILABLE: (
        "The scene image generation provider is currently unavailable."
    ),
    ImageGeneratorErrorCode.NOT_CONFIGURED: (
        "No scene image generation provider is configured."
    ),
    ImageGeneratorErrorCode.GENERATION_FAILED: (
        "The scene image generation provider failed to produce an image."
    ),
}


class ImageGenerationError(DomainError):
    """Raised when an ``ImageGenerator`` cannot fulfill a request."""

    default_code = ImageGeneratorErrorCode.GENERATION_FAILED.value
    default_user_message = USER_MESSAGES[ImageGeneratorErrorCode.GENERATION_FAILED]
    default_http_status = HTTPStatus.BAD_GATEWAY

    def __init__(
        self,
        code: ImageGeneratorErrorCode,
        detail: str = "",
        *,
        provider: str = "unknown",
    ) -> None:
        super().__init__(
            detail or USER_MESSAGES[code],
            code=code.value,
            user_message=USER_MESSAGES[code],
        )
        self.error_code = code
        self.provider = provider
        if code is ImageGeneratorErrorCode.INVALID_REQUEST:
            self.default_http_status = HTTPStatus.BAD_REQUEST
        elif code in (
            ImageGeneratorErrorCode.UNAVAILABLE,
            ImageGeneratorErrorCode.NOT_CONFIGURED,
        ):
            self.default_http_status = HTTPStatus.SERVICE_UNAVAILABLE


@dataclass(frozen=True)
class ImageGenerationRequest:
    """Parameters for generating one scene/shot illustration."""

    scene_id: str
    shot_id: str
    prompt: str
    width: int
    height: int
    style: str = "cinematic_story"
    character_ids: tuple[str, ...] = ()
    location_id: str = "loc_default_1"
    palette_rgb: tuple[tuple[int, int, int], ...] = ()
    seed_key: str = ""

    def __post_init__(self) -> None:
        if not self.scene_id.strip():
            raise ValueError("scene_id must not be empty")
        if not self.shot_id.strip():
            raise ValueError("shot_id must not be empty")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if (
            isinstance(self.width, bool)
            or not isinstance(self.width, int)
            or not (MIN_IMAGE_DIMENSION <= self.width <= MAX_IMAGE_DIMENSION)
        ):
            raise ValueError(
                f"width must be an int between {MIN_IMAGE_DIMENSION} and {MAX_IMAGE_DIMENSION}"
            )
        if (
            isinstance(self.height, bool)
            or not isinstance(self.height, int)
            or not (MIN_IMAGE_DIMENSION <= self.height <= MAX_IMAGE_DIMENSION)
        ):
            raise ValueError(
                f"height must be an int between {MIN_IMAGE_DIMENSION} and {MAX_IMAGE_DIMENSION}"
            )

    @property
    def prompt_hash(self) -> str:
        payload = (
            f"{self.scene_id}|{self.shot_id}|{self.style}|{self.location_id}|"
            f"{','.join(self.character_ids)}|{self.width}x{self.height}|"
            f"{self.seed_key}|{self.prompt.strip()}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GeneratedImage:
    """A generated scene/shot image and its provenance metadata."""

    data: bytes
    media_type: str
    width: int
    height: int
    sha256: str
    provider: str
    model_name: str
    prompt_hash: str
    generated_at: datetime

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("data must not be empty")
        if not self.sha256.strip():
            raise ValueError("sha256 must not be empty")
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        if not self.model_name.strip():
            raise ValueError("model_name must not be empty")


@dataclass(frozen=True)
class ImageGeneratorCapabilities:
    """Reported capabilities and availability of an ``ImageGenerator``."""

    provider_name: str
    model_name: str
    available: bool
    is_mock_or_local: bool
    supports_vertical_9_16: bool = True
    supports_horizontal_16_9: bool = True
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "available": self.available,
            "is_mock_or_local": self.is_mock_or_local,
            "supports_vertical_9_16": self.supports_vertical_9_16,
            "supports_horizontal_16_9": self.supports_horizontal_16_9,
            "reason": self.reason,
        }


@runtime_checkable
class ImageGenerator(Protocol):
    """Protocol for scene/shot visual illustration providers."""

    name: str

    def capabilities(self) -> ImageGeneratorCapabilities: ...

    def generate(self, request: ImageGenerationRequest) -> GeneratedImage: ...

    def check(self) -> None: ...

