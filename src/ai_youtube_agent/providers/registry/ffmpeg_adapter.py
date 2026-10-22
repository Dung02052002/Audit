"""FFmpeg Video Render Provider Adapter.

Wraps the ffmpeg command line renderer and provides capability declaration,
binary version inspection, codec validation, and hardware resource checks.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.ffmpeg_video_render import FfmpegVideoRenderer
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
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
from ai_youtube_agent.providers.video_render import (
    RenderedVideo,
    RenderErrorCode,
    RenderRequest,
    VideoRenderer,
    VideoRenderError,
)

logger = get_logger(__name__)


class FFmpegAdapter(BaseProviderAdapter, VideoRenderer):
    """Unified adapter wrapping the FFmpeg rendering engine."""

    def __init__(
        self,
        *,
        binary: str = "ffmpeg",
        timeout_seconds: int = 600,
        font_file: Path | None = None,
        use_mock_fallback: bool = False,
    ) -> None:
        super().__init__(
            provider_id="ffmpeg",
            category=ProviderCategory.VIDEO_RENDER,
        )
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.font_file = font_file
        self.use_mock_fallback = use_mock_fallback
        self.name = "ffmpeg"
        self._real_renderer = FfmpegVideoRenderer(
            binary=binary,
            timeout_s=timeout_seconds,
            font_file=font_file,
        )
        self._mock_renderer = MockVideoRenderer()

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            provider_id=self.provider_id,
            name="FFmpeg Automated Video Rendering Engine",
            category=self.category,
            is_local=True,
            is_free=True,
            supported_modalities=(
                "video_rendering",
                "audio_mixing",
                "burned_captions",
                "filter_complex",
                "h264_aac_mp4",
            ),
            supported_models=("libx264", "aac", "yuv420p"),
            resource_requirements=ResourceRequirement(
                cpu_cores_min=2,
                ram_mb_min=2048,
                disk_space_mb_min=2048,
                executable_name="ffmpeg",
            ),
            metadata={
                "binary": self.binary,
                "timeout_seconds": self.timeout_seconds,
                "font_configured": self.font_file is not None,
            },
        )

    def check_connection(self) -> ConnectionCheckResult:
        start = time.monotonic()
        try:
            result = subprocess.run(
                [self.binary, "-hide_banner", "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            latency_ms = (time.monotonic() - start) * 1000.0
            if result.returncode == 0:
                return ConnectionCheckResult(
                    connected=True,
                    latency_ms=round(latency_ms, 2),
                    endpoint=self.binary,
                )
            return ConnectionCheckResult(
                connected=False,
                endpoint=self.binary,
                error_message=f"ffmpeg exited with return code {result.returncode}",
            )
        except Exception as exc:
            return ConnectionCheckResult(
                connected=False,
                endpoint=self.binary,
                error_message=f"ffmpeg execution failed: {exc}",
            )

    def check_models(self) -> ModelCheckResult:
        try:
            result = subprocess.run(
                [self.binary, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            has_h264 = "libx264" in result.stdout or "h264" in result.stdout
            has_aac = "aac" in result.stdout

            available = []
            if has_h264:
                available.append("libx264")
            if has_aac:
                available.append("aac")

            valid = has_h264 and has_aac
            missing = []
            if not has_h264:
                missing.append("libx264")
            if not has_aac:
                missing.append("aac")

            return ModelCheckResult(
                valid=valid,
                available_models=tuple(available),
                default_model="libx264+aac",
                missing_models=tuple(missing),
                error_message=None if valid else f"Missing encoders: {missing}",
            )
        except Exception as exc:
            return ModelCheckResult(
                valid=False,
                error_message=f"Could not inspect ffmpeg encoders: {exc}",
            )

    def check_credentials(self) -> CredentialCheckResult:
        return CredentialCheckResult(
            valid=True,
            has_credentials=True,
            auth_type="none",
        )

    def check_resources(self) -> ResourceCheckResult:
        which_path = shutil.which(self.binary)
        font_valid = True
        font_warning = None
        if self.font_file and not Path(self.font_file).exists():
            font_valid = False
            font_warning = f"Configured caption font file not found: {self.font_file}"

        return ResourceCheckResult(
            sufficient=which_path is not None and font_valid,
            executable_found=which_path is not None,
            details={
                "resolved_path": which_path or "Not found",
                "font_file": str(self.font_file) if self.font_file else None,
            },
            warning_message=font_warning
            or (None if which_path else f"Binary '{self.binary}' not found on PATH"),
        )

    def render(self, request: RenderRequest) -> RenderedVideo:
        conn = self.check_connection()
        if not conn.connected:
            if self.use_mock_fallback:
                return self._mock_renderer.render(request)
            raise VideoRenderError(
                RenderErrorCode.UNAVAILABLE,
                f"ffmpeg binary '{self.binary}' is not available.",
                provider=self.name,
            )
        return self._real_renderer.render(request)
