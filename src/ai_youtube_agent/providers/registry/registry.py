"""Unified Provider Registry.

Orchestrates all AI engines, desktop applications, and web services:
- ComfyUI (Local Image & Workflow AI)
- EdgeTTS & ElevenLabs (Local Free Priority & Premium Cloud Voices)
- CapCut (Desktop Integration & Draft Exporter)
- FFmpeg (Automated Video Assembly & Rendering)
- Playwright (Web App Browser Automation & Testing)
- Video Generation APIs (Opt-in Motion Video Engines)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.registry.base import ProviderAdapter
from ai_youtube_agent.providers.registry.models import (
    ProviderCategory,
    ProviderDiagnostic,
    ProviderStatus,
    RegistryDiagnosticReport,
)

logger = get_logger(__name__)


class ProviderRegistryError(DomainError):
    """Raised when registry operations fail (e.g. unknown provider)."""

    default_code = "provider_registry.error"


class ProviderRegistry:
    """Central registry and capability manager for video production tools."""

    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        self._active: dict[ProviderCategory, str] = {}

    def register(
        self,
        adapter: ProviderAdapter,
        *,
        is_active: bool = False,
    ) -> None:
        """Register a provider adapter into the registry."""
        provider_id = adapter.provider_id
        category = adapter.category
        self._providers[provider_id] = adapter

        # If it's the first in its category or explicitly flagged, set as active
        if is_active or category not in self._active:
            self._active[category] = provider_id

        logger.info(
            "Registered provider adapter",
            extra={
                "fields": {
                    "provider_id": provider_id,
                    "category": category.value,
                    "is_active": self._active.get(category) == provider_id,
                }
            },
        )

    def unregister(self, provider_id: str) -> bool:
        """Unregister a provider by id."""
        adapter = self._providers.pop(provider_id, None)
        if not adapter:
            return False

        # If it was active, reset active choice
        if self._active.get(adapter.category) == provider_id:
            remaining = self.get_by_category(adapter.category)
            if remaining:
                self._active[adapter.category] = remaining[0].provider_id
            else:
                self._active.pop(adapter.category, None)
        return True

    def get(self, provider_id: str) -> ProviderAdapter | None:
        """Retrieve an adapter by id."""
        return self._providers.get(provider_id)

    def get_by_category(
        self, category: ProviderCategory
    ) -> tuple[ProviderAdapter, ...]:
        """List all registered adapters in a category."""
        return tuple(p for p in self._providers.values() if p.category == category)

    def get_active(self, category: ProviderCategory) -> ProviderAdapter | None:
        """Retrieve the currently active provider adapter for a category."""
        active_id = self._active.get(category)
        if active_id:
            return self._providers.get(active_id)
        candidates = self.get_by_category(category)
        return candidates[0] if candidates else None

    def set_active(self, provider_id: str) -> None:
        """Set a registered provider as active for its category."""
        adapter = self.get(provider_id)
        if not adapter:
            raise ProviderRegistryError(f"Provider '{provider_id}' is not registered.")
        self._active[adapter.category] = provider_id

    def list_all(self) -> tuple[ProviderAdapter, ...]:
        """List all registered adapters."""
        return tuple(self._providers.values())

    def diagnose_all(self) -> RegistryDiagnosticReport:
        """Execute full diagnostic inspection across all registered providers."""
        diagnostics: list[ProviderDiagnostic] = []
        ready_count = 0

        for adapter in self._providers.values():
            diag = adapter.diagnose()
            diagnostics.append(diag)
            if diag.status == ProviderStatus.READY:
                ready_count += 1

        overall_status = ProviderStatus.READY
        if ready_count < len(diagnostics):
            if any(
                d.status in (ProviderStatus.UNAVAILABLE, ProviderStatus.MISCONFIGURED)
                for d in diagnostics
            ):
                overall_status = ProviderStatus.DEGRADED
            elif ready_count == 0 and len(diagnostics) > 0:
                overall_status = ProviderStatus.UNAVAILABLE

        return RegistryDiagnosticReport(
            status=overall_status,
            total_providers=len(diagnostics),
            ready_count=ready_count,
            diagnostics=tuple(diagnostics),
            checked_at=datetime.now(UTC),
        )

    def diagnose(self, provider_id: str) -> ProviderDiagnostic:
        """Diagnose a specific provider."""
        adapter = self.get(provider_id)
        if not adapter:
            raise ProviderRegistryError(f"Provider '{provider_id}' not found.")
        return adapter.diagnose()

    def check_all(self) -> dict[str, bool]:
        """Perform quick health checks across all providers."""
        results: dict[str, bool] = {}
        for pid, adapter in self._providers.items():
            try:
                adapter.check()
                results[pid] = True
            except Exception:
                results[pid] = False
        return results

    def summary(self) -> dict[str, Any]:
        """Return a compact JSON-serializable overview of all providers."""
        categories_dict: dict[str, Any] = {}
        for cat in ProviderCategory:
            active_adapter = self.get_active(cat)
            all_in_cat = self.get_by_category(cat)
            categories_dict[cat.value] = {
                "active": active_adapter.provider_id if active_adapter else None,
                "available_providers": [p.provider_id for p in all_in_cat],
            }

        return {
            "total_registered": len(self._providers),
            "categories": categories_dict,
            "providers": [
                {
                    "provider_id": p.provider_id,
                    "category": p.category.value,
                    "name": p.capabilities().name,
                    "is_local": p.capabilities().is_local,
                    "is_free": p.capabilities().is_free,
                    "is_active": self._active.get(p.category) == p.provider_id,
                }
                for p in self._providers.values()
            ],
        }


def build_default_provider_registry(
    settings: Any = None,
    *,
    comfyui_base_url: str | None = None,
    elevenlabs_api_key: str | None = None,
    edge_tts_voice: str | None = None,
    capcut_draft_dir: Any = None,
    video_gen_enabled: bool = False,
) -> ProviderRegistry:
    """Build and populate the standard ProviderRegistry with all tools."""
    import os
    from pathlib import Path

    from ai_youtube_agent.core.config import RenderProviderKind
    from ai_youtube_agent.providers.registry.capcut_adapter import CapCutAdapter
    from ai_youtube_agent.providers.registry.comfyui_adapter import ComfyUIAdapter
    from ai_youtube_agent.providers.registry.ffmpeg_adapter import FFmpegAdapter
    from ai_youtube_agent.providers.registry.playwright_adapter import PlaywrightAdapter
    from ai_youtube_agent.providers.registry.tts_adapters import (
        EdgeTTSAdapter,
        ElevenLabsTTSAdapter,
        MockTTSAdapter,
    )
    from ai_youtube_agent.providers.registry.video_api_adapter import VideoGenApiAdapter

    registry = ProviderRegistry()

    # 1. ComfyUI (Local Image & Workflow AI)
    c_url = comfyui_base_url or os.environ.get(
        "COMFYUI_BASE_URL", "http://127.0.0.1:8188"
    )
    comfy = ComfyUIAdapter(base_url=c_url)
    registry.register(comfy, is_active=True)

    # 2. TTS Providers (Local/Free Vietnamese Priority: EdgeTTS, ElevenLabs, Mock)
    vt_voice = edge_tts_voice or os.environ.get("EDGE_TTS_VOICE", "vi-VN-HoaiMyNeural")
    edge_tts = EdgeTTSAdapter(default_voice=vt_voice)
    registry.register(edge_tts, is_active=True)

    el_key = elevenlabs_api_key or os.environ.get("ELEVENLABS_API_KEY")
    eleven = ElevenLabsTTSAdapter(api_key=el_key)
    registry.register(eleven, is_active=False)

    mock_tts = MockTTSAdapter()
    registry.register(mock_tts, is_active=False)

    # 3. CapCut Desktop Integration & Draft Exporter
    cc_dir = capcut_draft_dir or (
        Path(os.environ["CAPCUT_DRAFT_DIR"])
        if "CAPCUT_DRAFT_DIR" in os.environ
        else None
    )
    capcut = CapCutAdapter(custom_draft_dir=cc_dir)
    registry.register(capcut, is_active=True)

    # 4. FFmpeg Video Renderer
    ffmpeg_bin = getattr(settings, "ffmpeg_binary", "ffmpeg") if settings else "ffmpeg"
    timeout_s = getattr(settings, "render_timeout_seconds", 600) if settings else 600
    font_file = getattr(settings, "render_font_file", None) if settings else None
    use_mock = (
        getattr(settings, "render_provider", RenderProviderKind.MOCK)
        == RenderProviderKind.MOCK
        if settings
        else False
    )

    ffmpeg = FFmpegAdapter(
        binary=ffmpeg_bin,
        timeout_seconds=timeout_s,
        font_file=font_file,
        use_mock_fallback=use_mock,
    )
    registry.register(ffmpeg, is_active=True)

    # 5. Playwright Browser Automation & UI Smoke Testing
    playwright = PlaywrightAdapter()
    registry.register(playwright, is_active=True)

    # 6. External Video Generation API (Opt-in)
    vgen_flag = video_gen_enabled or (
        os.environ.get("AI_YOUTUBE_AGENT_VIDEO_GEN_ENABLED", "").lower()
        in ("true", "1")
    )
    video_api = VideoGenApiAdapter(is_enabled=vgen_flag)
    registry.register(video_api, is_active=True)

    return registry
