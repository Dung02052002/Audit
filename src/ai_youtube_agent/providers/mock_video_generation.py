"""Unconfigured and Mock Video Clip Generators (Script-to-Film Refactor, Phase 5).

- ``UnconfiguredVideoClipGenerator`` is the default provider when no external
  AI video generation model is configured. It honestly reports
  ``available=False`` so ``ProductionMode.AI_FILM`` refuses to fake motion film
  from static images and directs callers to ``ProductionMode.STORY_VIDEO`` (or
  user-supplied video clips).
- ``MockVideoClipGenerator`` is an explicit deterministic clip generator for
  automated tests of ``AI_FILM`` clip orchestration.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from ai_youtube_agent.providers.mock_video_render import stub_mp4
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    free_declaration,
)
from ai_youtube_agent.providers.video_generation import (
    MP4_CLIP_MEDIA_TYPE,
    GeneratedVideoClip,
    VideoClipErrorCode,
    VideoClipGenerationError,
    VideoClipGenerationRequest,
    VideoClipGeneratorCapabilities,
)

Clock = Callable[[], datetime]


class UnconfiguredVideoClipGenerator:
    """Default provider when no AI video-clip generator is configured."""

    def __init__(self, *, name: str = "unconfigured") -> None:
        self.name = name

    def capabilities(self) -> VideoClipGeneratorCapabilities:
        return VideoClipGeneratorCapabilities(
            provider_name=self.name,
            model_name="none",
            available=False,
            is_mock=False,
            reason=(
                "No AI video-clip generation provider is configured in this workspace. "
                "Use STORY_VIDEO mode or attach pre-rendered video clip assets."
            ),
        )

    def generate_clip(self, request: VideoClipGenerationRequest) -> GeneratedVideoClip:
        raise VideoClipGenerationError(
            VideoClipErrorCode.NOT_CONFIGURED,
            f"Cannot generate AI_FILM clip for {request.shot_id}: "
            "no video clip provider is configured.",
            provider=self.name,
        )

    def check(self) -> None:
        raise VideoClipGenerationError(
            VideoClipErrorCode.NOT_CONFIGURED,
            "No AI video-clip generation provider is configured.",
            provider=self.name,
        )

    def cost_declaration(self) -> ProviderCostDeclaration:
        return free_declaration(
            self.name,
            "none",
            ProviderCostClass.MOCK,
            "Không có nhà cung cấp clip; không gọi API nào.",
        )


class MockVideoClipGenerator:
    """Explicit deterministic MP4 clip generator for testing ``AI_FILM`` workflows."""

    def __init__(
        self,
        *,
        name: str = "mock_video_clip",
        model_name: str = "mock-film-clip-v1",
        clock: Clock | None = None,
    ) -> None:
        self.name = name
        self.model_name = model_name
        self._clock = clock
        self._lock = threading.Lock()
        self._healthy = True
        self.calls: list[VideoClipGenerationRequest] = []

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = healthy

    def capabilities(self) -> VideoClipGeneratorCapabilities:
        with self._lock:
            healthy = self._healthy
        return VideoClipGeneratorCapabilities(
            provider_name=self.name,
            model_name=self.model_name,
            available=healthy,
            is_mock=True,
            reason=None if healthy else "Mock video clip generator marked unhealthy",
        )

    def generate_clip(self, request: VideoClipGenerationRequest) -> GeneratedVideoClip:
        with self._lock:
            self.calls.append(request)
            if not self._healthy:
                raise VideoClipGenerationError(
                    VideoClipErrorCode.UNAVAILABLE,
                    "mock video clip generator is unhealthy",
                    provider=self.name,
                )
        seed = hashlib.sha256(request.prompt_hash.encode("utf-8")).digest()
        data = stub_mp4(request.duration_ms, seed)
        digest = hashlib.sha256(data).hexdigest()
        now = self._clock() if self._clock else datetime.now(UTC)
        return GeneratedVideoClip(
            data=data,
            media_type=MP4_CLIP_MEDIA_TYPE,
            duration_ms=request.duration_ms,
            width=request.width,
            height=request.height,
            fps=request.fps,
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
            raise VideoClipGenerationError(
                VideoClipErrorCode.UNAVAILABLE,
                "mock video clip generator is unhealthy",
                provider=self.name,
            )

    def cost_declaration(self) -> ProviderCostDeclaration:
        return free_declaration(
            self.name,
            self.model_name,
            ProviderCostClass.MOCK,
            "Clip mock trong bộ nhớ.",
        )
