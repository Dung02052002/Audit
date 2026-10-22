"""Provider Registry and Adapters Package.

Exports unified adapter interfaces, capability declarations, and the central
ProviderRegistry coordinating ComfyUI, EdgeTTS, ElevenLabs, CapCut, FFmpeg,
Playwright, and external Video Generation APIs.
"""

from ai_youtube_agent.providers.registry.base import (
    BaseProviderAdapter,
    ProviderAdapter,
)
from ai_youtube_agent.providers.registry.capcut_adapter import (
    CapCutAdapter,
    CapCutExportResult,
)
from ai_youtube_agent.providers.registry.comfyui_adapter import (
    ComfyUIAdapter,
)
from ai_youtube_agent.providers.registry.ffmpeg_adapter import (
    FFmpegAdapter,
)
from ai_youtube_agent.providers.registry.models import (
    ConnectionCheckResult,
    CredentialCheckResult,
    ModelCheckResult,
    ProviderCapability,
    ProviderCategory,
    ProviderDiagnostic,
    ProviderStatus,
    RegistryDiagnosticReport,
    ResourceCheckResult,
    ResourceRequirement,
)
from ai_youtube_agent.providers.registry.playwright_adapter import (
    BrowserSmokeTestReport,
    PlaywrightAdapter,
)
from ai_youtube_agent.providers.registry.registry import (
    ProviderRegistry,
    ProviderRegistryError,
    build_default_provider_registry,
)
from ai_youtube_agent.providers.registry.tts_adapters import (
    DEFAULT_VIETNAMESE_VOICES,
    EdgeTTSAdapter,
    ElevenLabsTTSAdapter,
    MockTTSAdapter,
)
from ai_youtube_agent.providers.registry.video_api_adapter import (
    VideoGenApiAdapter,
)

__all__ = [
    "BaseProviderAdapter",
    "BrowserSmokeTestReport",
    "CapCutAdapter",
    "CapCutExportResult",
    "ComfyUIAdapter",
    "ConnectionCheckResult",
    "CredentialCheckResult",
    "DEFAULT_VIETNAMESE_VOICES",
    "build_default_provider_registry",
    "EdgeTTSAdapter",
    "ElevenLabsTTSAdapter",
    "FFmpegAdapter",
    "ModelCheckResult",
    "MockTTSAdapter",
    "PlaywrightAdapter",
    "ProviderAdapter",
    "ProviderCapability",
    "ProviderCategory",
    "ProviderDiagnostic",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProviderStatus",
    "RegistryDiagnosticReport",
    "ResourceCheckResult",
    "ResourceRequirement",
    "VideoGenApiAdapter",
]
