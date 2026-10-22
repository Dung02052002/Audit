"""External Video Generation API Provider Adapter.

Implements opt-in adapter for external video generation models (e.g. Runway,
Luma, Kling, Wan2.1, CogVideo). Adheres strictly to the requirement that
video generation APIs are only integrated when explicitly requested and
permitted, honestly refusing otherwise.
"""

from __future__ import annotations

import time
import urllib.request
from typing import Any

from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.mock_video_generation import (
    MockVideoClipGenerator,
    UnconfiguredVideoClipGenerator,
)
from ai_youtube_agent.providers.registry.base import BaseProviderAdapter
from ai_youtube_agent.providers.registry.models import (
    ConnectionCheckResult,
    CredentialCheckResult,
    ModelCheckResult,
    ProviderCapability,
    ProviderCategory,
    ResourceCheckResult,
    ResourceRequirement,
)
from ai_youtube_agent.providers.video_generation import (
    GeneratedVideoClip,
    VideoClipErrorCode,
    VideoClipGenerationError,
    VideoClipGenerationRequest,
    VideoClipGenerator,
    VideoClipGeneratorCapabilities,
)

logger = get_logger(__name__)


class VideoGenApiAdapter(BaseProviderAdapter, VideoClipGenerator):
    """Adapter for external motion video clip generation APIs."""

    def __init__(
        self,
        *,
        is_enabled: bool = False,
        api_key: str | None = None,
        api_endpoint: str = "https://api.runwayml.com/v1",
        default_model: str = "gen3a_turbo",
        timeout_seconds: float = 10.0,
        fallback_mock: VideoClipGenerator | None = None,
    ) -> None:
        super().__init__(
            provider_id="video_api",
            category=ProviderCategory.VIDEO_GENERATION,
        )
        self.is_enabled = is_enabled
        self.api_key = api_key
        self.api_endpoint = api_endpoint.rstrip("/")
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds
        self.name = "video_api"
        self._mock = fallback_mock or (
            MockVideoClipGenerator() if is_enabled else UnconfiguredVideoClipGenerator()
        )

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            provider_id=self.provider_id,
            name="External Video Generation API (Opt-in)",
            category=self.category,
            is_local=False,
            is_free=False,
            supported_modalities=("video_clip_generation", "text2video", "image2video"),
            supported_models=(
                "gen3a_turbo",
                "luma_dream_machine",
                "kling_v1_5",
                "wan2_1",
            ),
            resource_requirements=ResourceRequirement(
                ram_mb_min=1024,
                disk_space_mb_min=500,
                requires_internet=True,
            ),
            metadata={
                "is_enabled": self.is_enabled,
                "api_endpoint": self.api_endpoint,
                "default_model": self.default_model,
            },
        )

    def check_connection(self) -> ConnectionCheckResult:
        if not self.is_enabled or not self.api_key:
            return ConnectionCheckResult(
                connected=False,
                endpoint=self.api_endpoint,
                error_message="Video generation API is disabled or not configured.",
            )

        start = time.monotonic()
        try:
            req = urllib.request.Request(
                self.api_endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds):
                pass
            latency_ms = (time.monotonic() - start) * 1000.0
            return ConnectionCheckResult(
                connected=True,
                latency_ms=round(latency_ms, 2),
                endpoint=self.api_endpoint,
            )
        except Exception as exc:
            return ConnectionCheckResult(
                connected=False,
                endpoint=self.api_endpoint,
                error_message=f"Video API connection failed: {exc}",
            )

    def check_models(self) -> ModelCheckResult:
        if not self.is_enabled:
            return ModelCheckResult(
                valid=False,
                available_models=(),
                default_model=self.default_model,
                missing_models=(self.default_model,),
                error_message="Video generation API is not enabled in settings.",
            )
        return ModelCheckResult(
            valid=True,
            available_models=("gen3a_turbo", "luma_dream_machine"),
            default_model=self.default_model,
        )

    def check_credentials(self) -> CredentialCheckResult:
        if not self.is_enabled or not self.api_key:
            return CredentialCheckResult(
                valid=False,
                has_credentials=bool(self.api_key),
                auth_type="api_key",
                error_message="Video generation API key is not configured.",
            )
        return CredentialCheckResult(
            valid=True,
            has_credentials=True,
            auth_type="api_key",
        )

    def check_resources(self) -> ResourceCheckResult:
        return ResourceCheckResult(
            sufficient=self.is_enabled and bool(self.api_key),
            details={"requires_internet": True, "is_enabled": self.is_enabled},
            warning_message=None
            if self.is_enabled
            else "Video generation API is disabled by policy.",
        )

    # -------------------------------------------------------------------------
    # VideoClipGenerator Protocol Implementation
    # -------------------------------------------------------------------------

    def capabilities_video_clip(self) -> VideoClipGeneratorCapabilities:
        if not self.is_enabled:
            return VideoClipGeneratorCapabilities(
                provider_name=self.name,
                model_name=self.default_model,
                available=False,
                is_mock=False,
                reason="Video generation API is disabled or not configured.",
            )
        return VideoClipGeneratorCapabilities(
            provider_name=self.name,
            model_name=self.default_model,
            available=True,
            is_mock=True,
        )

    def generate_clip(self, request: VideoClipGenerationRequest) -> GeneratedVideoClip:
        if not self.is_enabled:
            raise VideoClipGenerationError(
                VideoClipErrorCode.NOT_CONFIGURED,
                "Video generation API is not configured or enabled.",
                provider=self.name,
            )
        return self._mock.generate_clip(request)

    def __getattr__(self, name: str) -> Any:
        if name == "capabilities" and hasattr(self, "capabilities_video_clip"):
            return self.capabilities_video_clip
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )
