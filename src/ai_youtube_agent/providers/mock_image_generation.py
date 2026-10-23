"""Deterministic Local Scene Image Generator (Script-to-Film Refactor, Phase 5).

``DeterministicSceneImageGenerator`` synthesizes valid 8-bit RGB PNG scene
illustrations in pure Python without paid external APIs:
- Preserves location color continuity by deriving background/stage bands from
  ``request.palette_rgb`` (or ``request.location_id``).
- Preserves character and shot distinction by deriving foreground composition
  bands from ``request.character_ids``, ``request.shot_id``, and
  ``request.prompt_hash``.
- Produces valid PNGs at ``1080x1920`` (Shorts 9:16) or ``1920x1080`` (LongForm 16:9)
  that pass ``probe_image`` and FFmpeg rendering.
"""

from __future__ import annotations

import hashlib
import struct
import threading
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from ai_youtube_agent.providers.image_generation import (
    PNG_MEDIA_TYPE,
    GeneratedImage,
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGeneratorCapabilities,
    ImageGeneratorErrorCode,
)
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    free_declaration,
)

Clock = Callable[[], datetime]
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def synthesize_scene_png(request: ImageGenerationRequest) -> bytes:
    """Synthesize a deterministic 8-bit RGB PNG for ``request``."""
    width = request.width
    height = request.height

    loc_seed = hashlib.sha256(
        f"loc|{request.location_id}|{request.style}".encode()
    ).digest()
    shot_seed = hashlib.sha256(request.prompt_hash.encode("utf-8")).digest()

    if len(request.palette_rgb) >= 3:
        bg_rgb = request.palette_rgb[0]
        mid_rgb = request.palette_rgb[1]
        accent_rgb = request.palette_rgb[2]
    else:
        bg_rgb = (loc_seed[0], loc_seed[1], loc_seed[2])
        mid_rgb = (loc_seed[3], loc_seed[4], loc_seed[5])
        accent_rgb = (shot_seed[0], shot_seed[1], shot_seed[2])

    subject_rgb = (
        (mid_rgb[0] ^ shot_seed[3]) & 0xFF,
        (mid_rgb[1] ^ shot_seed[4]) & 0xFF,
        (mid_rgb[2] ^ shot_seed[5]) & 0xFF,
    )

    border = max(8, min(width, height) // 48)
    horizon_top = height // 4
    horizon_bottom = (height * 3) // 4
    subject_left = width // 5
    subject_right = width - subject_left

    border_row = b"\x00" + (bytes(accent_rgb) * width)
    sky_row = (
        b"\x00"
        + (bytes(accent_rgb) * border)
        + (bytes(bg_rgb) * (width - 2 * border))
        + (bytes(accent_rgb) * border)
    )
    stage_row = (
        b"\x00"
        + (bytes(accent_rgb) * border)
        + (bytes(mid_rgb) * (subject_left - border))
        + (bytes(subject_rgb) * (subject_right - subject_left))
        + (bytes(mid_rgb) * (width - border - subject_right))
        + (bytes(accent_rgb) * border)
    )

    scanlines = bytearray()
    for y in range(height):
        if y < border or y >= height - border:
            scanlines.extend(border_row)
        elif horizon_top <= y < horizon_bottom:
            scanlines.extend(stage_row)
        else:
            scanlines.extend(sky_row)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    comment = (
        f"scene={request.scene_id};shot={request.shot_id};"
        f"loc={request.location_id};hash={request.prompt_hash[:16]}"
    ).encode("ascii", errors="ignore")
    text_chunk = b"Comment\x00" + comment
    compressed = zlib.compress(bytes(scanlines), level=1)

    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"tEXt", text_chunk)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


class DeterministicSceneImageGenerator:
    """Local deterministic PNG scene/shot illustrator for ``STORY_VIDEO`` mode."""

    def __init__(
        self,
        *,
        name: str = "local_deterministic_png",
        model_name: str = "story-canvas-png-v1",
        clock: Clock | None = None,
    ) -> None:
        self.name = name
        self.model_name = model_name
        self._clock = clock
        self._lock = threading.Lock()
        self._healthy = True
        self._failures: list[ImageGeneratorErrorCode] = []
        self.calls: list[ImageGenerationRequest] = []

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = healthy

    def fail_next(self, code: ImageGeneratorErrorCode, *, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be >= 1")
        with self._lock:
            self._failures.extend([code] * times)

    def capabilities(self) -> ImageGeneratorCapabilities:
        with self._lock:
            healthy = self._healthy
        return ImageGeneratorCapabilities(
            provider_name=self.name,
            model_name=self.model_name,
            available=healthy,
            is_mock_or_local=True,
            supports_vertical_9_16=True,
            supports_horizontal_16_9=True,
            reason=None if healthy else "Provider marked unhealthy",
        )

    def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        with self._lock:
            self.calls.append(request)
            if self._failures:
                code = self._failures.pop(0)
                raise ImageGenerationError(
                    code,
                    "scripted image generation failure",
                    provider=self.name,
                )
            if not self._healthy:
                raise ImageGenerationError(
                    ImageGeneratorErrorCode.UNAVAILABLE,
                    "scene image generator is unhealthy",
                    provider=self.name,
                )

        png_bytes = synthesize_scene_png(request)
        digest = hashlib.sha256(png_bytes).hexdigest()
        now = self._clock() if self._clock else datetime.now(UTC)
        return GeneratedImage(
            data=png_bytes,
            media_type=PNG_MEDIA_TYPE,
            width=request.width,
            height=request.height,
            sha256=digest,
            provider=self.name,
            model_name=self.model_name,
            prompt_hash=request.prompt_hash,
            generated_at=now,
        )

    def check(self) -> None:
        with self._lock:
            healthy = self._healthy
        if not healthy:
            raise ImageGenerationError(
                ImageGeneratorErrorCode.UNAVAILABLE,
                "scene image generator is unhealthy",
                provider=self.name,
            )

    def cost_declaration(self) -> ProviderCostDeclaration:
        return free_declaration(
            self.name,
            self.model_name,
            ProviderCostClass.LOCAL_FREE,
            "Ảnh PNG tạo cục bộ bằng Python, không gọi mạng.",
        )
