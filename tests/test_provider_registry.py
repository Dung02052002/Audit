"""Tests for Section 1: Provider Registry & Tool Adapters.

Verifies ComfyUI, EdgeTTS, ElevenLabs, CapCut, FFmpeg, Playwright,
and Video Generation API adapters, diagnostics, capability declarations,
and FastAPI endpoint integrations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.main import create_app
from ai_youtube_agent.providers.image_generation import (
    ImageGenerationRequest,
    ImageGenerator,
)
from ai_youtube_agent.providers.registry import (
    CapCutAdapter,
    ComfyUIAdapter,
    EdgeTTSAdapter,
    ElevenLabsTTSAdapter,
    FFmpegAdapter,
    MockTTSAdapter,
    PlaywrightAdapter,
    ProviderCategory,
    RegistryDiagnosticReport,
    VideoGenApiAdapter,
    build_default_provider_registry,
)
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesizer,
)
from ai_youtube_agent.providers.video_generation import (
    VideoClipGenerationError,
    VideoClipGenerationRequest,
    VideoClipGenerator,
)
from ai_youtube_agent.providers.video_render import VideoRenderer


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "test.db",
        artifact_dir=tmp_path / "artifacts",
    )


# -----------------------------------------------------------------------------
# 1. ComfyUI Adapter Tests
# -----------------------------------------------------------------------------


def test_comfyui_adapter_capabilities() -> None:
    adapter = ComfyUIAdapter(
        base_url="http://127.0.0.1:8188", enable_video_workflow=True
    )
    caps = adapter.capabilities()

    assert caps.provider_id == "comfyui"
    assert caps.category == ProviderCategory.IMAGE_GENERATION
    assert caps.is_local is True
    assert caps.is_free is True
    assert "workflow_api" in caps.supported_modalities
    assert "video_clip" in caps.supported_modalities
    assert caps.resource_requirements.requires_gpu is True


def test_comfyui_adapter_connection_check_offline() -> None:
    # Pointing to an invalid local port should cleanly report offline, never crash
    adapter = ComfyUIAdapter(base_url="http://127.0.0.1:59999", timeout_seconds=0.2)
    conn = adapter.check_connection()
    assert conn.connected is False
    assert conn.endpoint == "http://127.0.0.1:59999"
    assert (
        "unreachable" in conn.error_message.lower()
        or "connection" in conn.error_message.lower()
    )


def test_comfyui_adapter_generates_image_with_fallback() -> None:
    adapter = ComfyUIAdapter(base_url="http://127.0.0.1:59999", timeout_seconds=0.2)
    assert isinstance(adapter, ImageGenerator)

    req = ImageGenerationRequest(
        scene_id="scene_01",
        shot_id="shot_01",
        prompt="A peaceful teahouse in the evening rain",
        width=1080,
        height=1920,
    )
    result = adapter.generate(req)
    assert len(result.data) > 0
    assert result.media_type == "image/png"
    assert result.width == 1080
    assert result.height == 1920


# -----------------------------------------------------------------------------
# 2. TTS Adapters Tests (EdgeTTS, ElevenLabs, Mock)
# -----------------------------------------------------------------------------


def test_edge_tts_adapter_vietnamese_priority() -> None:
    adapter = EdgeTTSAdapter(default_voice="vi-VN-HoaiMyNeural")
    assert isinstance(adapter, SpeechSynthesizer)

    caps = adapter.capabilities()
    assert caps.provider_id == "edge_tts"
    assert caps.category == ProviderCategory.SPEECH_SYNTHESIS
    assert caps.is_free is True
    assert "vi-VN-HoaiMyNeural" in caps.supported_models
    assert "vi-VN-NamMinhNeural" in caps.supported_models

    req = SpeechRequest(
        text="Xin chào, đây là hệ thống sản xuất video tự động.",
        voice_id="vi-VN-HoaiMyNeural",
        language="vi-VN",
    )
    speech = adapter.synthesize(req)
    assert len(speech.audio) > 0
    assert speech.characters == len(req.text)
    assert speech.voice_id == "vi-VN-HoaiMyNeural"


def test_elevenlabs_adapter_credentials_check() -> None:
    # Unconfigured adapter
    adapter = ElevenLabsTTSAdapter(api_key=None)
    creds = adapter.check_credentials()
    assert creds.valid is False
    assert creds.has_credentials is False

    # Configured adapter
    adapter_with_key = ElevenLabsTTSAdapter(api_key="test_api_key_123")
    creds_ok = adapter_with_key.check_credentials()
    assert creds_ok.valid is True
    assert creds_ok.has_credentials is True


def test_mock_tts_adapter() -> None:
    adapter = MockTTSAdapter()
    assert adapter.check_connection().connected is True
    req = SpeechRequest(
        text="Testing mock TTS",
        voice_id="mock-vietnamese-1",
        language="vi",
    )
    res = adapter.synthesize(req)
    assert res.provider == "mock"


# -----------------------------------------------------------------------------
# 3. CapCut Adapter Tests
# -----------------------------------------------------------------------------


def test_capcut_adapter_capabilities_and_checks(tmp_path: Path) -> None:
    adapter = CapCutAdapter(custom_draft_dir=tmp_path / "CapCutDrafts")
    caps = adapter.capabilities()

    assert caps.provider_id == "capcut"
    assert caps.category == ProviderCategory.DESKTOP_EDITOR
    assert caps.is_local is True
    assert "desktop_draft_export" in caps.supported_modalities

    models = adapter.check_models()
    assert models.valid is True
    assert "capcut_draft_v1" in models.available_models

    creds = adapter.check_credentials()
    assert creds.valid is True


def test_capcut_adapter_export_draft(tmp_path: Path) -> None:
    draft_dir = tmp_path / "CapCutProjects"
    adapter = CapCutAdapter(custom_draft_dir=draft_dir)

    # Fake audio file
    fake_audio = tmp_path / "voiceover.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ....data....")

    scenes = [
        {
            "scene_id": "scene_01",
            "duration_ms": 5000,
            "caption": "Mỗi tách trà nóng đều giữ lại hương vị của mùa cũ.",
        },
        {
            "scene_id": "scene_02",
            "duration_ms": 7000,
            "caption": "Cơn mưa đầu mùa ghé qua phố nhỏ.",
        },
    ]

    export_res = adapter.export_draft(
        title="Huong_Tra_Dem_Mua",
        scenes=scenes,
        audio_file_path=str(fake_audio),
        duration_ms=12000,
        is_vertical_9_16=True,
    )

    assert Path(export_res.draft_folder_path).exists()
    assert Path(export_res.draft_content_path).exists()
    assert Path(export_res.draft_meta_path).exists()
    assert Path(export_res.instructions_file).exists()

    # Validate draft_content.json structure
    content = json.loads(
        Path(export_res.draft_content_path).read_text(encoding="utf-8")
    )
    assert content["canvas_config"]["ratio"] == "9:16"
    assert len(content["tracks"]) == 3  # Video, audio, text
    assert len(content["tracks"][0]["segments"]) == 2  # 2 scenes
    assert len(content["tracks"][2]["segments"]) == 2  # 2 subtitle lines

    # Validate instructions file
    instructions = Path(export_res.instructions_file).read_text(encoding="utf-8")
    assert "HƯỚNG DẪN MỞ DỰ ÁN TRÊN CAPCUT" in instructions


# -----------------------------------------------------------------------------
# 4. FFmpeg Adapter Tests
# -----------------------------------------------------------------------------


def test_ffmpeg_adapter_capabilities_and_resources() -> None:
    adapter = FFmpegAdapter(binary="ffmpeg", use_mock_fallback=True)
    assert isinstance(adapter, VideoRenderer)

    caps = adapter.capabilities()
    assert caps.provider_id == "ffmpeg"
    assert caps.category == ProviderCategory.VIDEO_RENDER
    assert "video_rendering" in caps.supported_modalities

    creds = adapter.check_credentials()
    assert creds.valid is True


# -----------------------------------------------------------------------------
# 5. Playwright Adapter Tests
# -----------------------------------------------------------------------------


def test_playwright_adapter_capabilities_and_smoke_test() -> None:
    adapter = PlaywrightAdapter()
    caps = adapter.capabilities()

    assert caps.provider_id == "playwright"
    assert caps.category == ProviderCategory.BROWSER_AUTOMATION
    assert "web_ui_testing" in caps.supported_modalities
    assert "chromium" in caps.supported_models

    models = adapter.check_models()
    assert "chromium" in models.available_models or models.default_model == "chromium"

    creds = adapter.check_credentials()
    assert creds.valid is True


# -----------------------------------------------------------------------------
# 6. Video Gen API Adapter Tests
# -----------------------------------------------------------------------------


def test_video_api_adapter_unconfigured_honest_refusal() -> None:
    adapter = VideoGenApiAdapter(is_enabled=False)
    assert isinstance(adapter, VideoClipGenerator)

    caps = adapter.capabilities()
    assert caps.provider_id == "video_api"
    assert caps.category == ProviderCategory.VIDEO_GENERATION

    # Protocol capabilities
    clip_caps = adapter.capabilities_video_clip()
    assert clip_caps.available is False
    assert (
        "disabled" in clip_caps.reason.lower()
        or "not configured" in clip_caps.reason.lower()
    )

    # Generating clip while disabled must raise VideoClipGenerationError
    req = VideoClipGenerationRequest(
        scene_id="sc_01",
        shot_id="sh_01",
        prompt="Cinematic drone shot over mountains",
        duration_ms=4000,
        width=1080,
        height=1920,
    )
    with pytest.raises(VideoClipGenerationError):
        adapter.generate_clip(req)


# -----------------------------------------------------------------------------
# 7. Provider Registry Core Tests
# -----------------------------------------------------------------------------


def test_provider_registry_lifecycle_and_diagnostics(test_settings: Settings) -> None:
    registry = build_default_provider_registry(test_settings)

    # Verify registration of all 6 required tool sets
    assert registry.get("comfyui") is not None
    assert registry.get("edge_tts") is not None
    assert registry.get("elevenlabs") is not None
    assert registry.get("capcut") is not None
    assert registry.get("ffmpeg") is not None
    assert registry.get("playwright") is not None
    assert registry.get("video_api") is not None

    # Check active providers per category
    assert (
        registry.get_active(ProviderCategory.IMAGE_GENERATION).provider_id == "comfyui"
    )
    assert (
        registry.get_active(ProviderCategory.SPEECH_SYNTHESIS).provider_id == "edge_tts"
    )
    assert registry.get_active(ProviderCategory.DESKTOP_EDITOR).provider_id == "capcut"
    assert registry.get_active(ProviderCategory.VIDEO_RENDER).provider_id == "ffmpeg"
    assert (
        registry.get_active(ProviderCategory.BROWSER_AUTOMATION).provider_id
        == "playwright"
    )

    # Switch active provider
    registry.set_active("mock_tts")
    assert (
        registry.get_active(ProviderCategory.SPEECH_SYNTHESIS).provider_id == "mock_tts"
    )

    # Run diagnostics across all tools
    report = registry.diagnose_all()
    assert isinstance(report, RegistryDiagnosticReport)
    assert report.total_providers >= 6
    assert len(report.diagnostics) == report.total_providers

    # Inspect summary
    summary = registry.summary()
    assert summary["total_registered"] >= 6
    assert "categories" in summary
    assert "providers" in summary


# -----------------------------------------------------------------------------
# 8. HTTP API Endpoints Integration Tests
# -----------------------------------------------------------------------------


def test_providers_http_api(test_settings: Settings) -> None:
    container = build_container(test_settings)
    app = create_app(container)
    client = TestClient(app)

    # GET /providers
    res = client.get("/providers")
    assert res.status_code == 200
    data = res.json()
    assert data["total_registered"] >= 6

    # GET /providers/diagnostics
    res_diag = client.get("/providers/diagnostics")
    assert res_diag.status_code == 200
    diag_data = res_diag.json()
    assert diag_data["total_providers"] >= 6
    assert len(diag_data["diagnostics"]) >= 6

    # GET /providers/{id}
    res_comfy = client.get("/providers/comfyui")
    assert res_comfy.status_code == 200
    assert res_comfy.json()["provider_id"] == "comfyui"

    # POST /providers/{id}/test
    res_test = client.post("/providers/edge_tts/test")
    assert res_test.status_code == 200
    test_json = res_test.json()
    assert test_json["provider_id"] == "edge_tts"
    assert "connection" in test_json
    assert "models" in test_json

    # POST /providers/capcut/export
    res_export = client.post(
        "/providers/capcut/export",
        json={
            "title": "API_Test_Video",
            "duration_ms": 10000,
            "scenes": [
                {"scene_id": "sc_1", "duration_ms": 5000, "caption": "Scene 1 caption"},
                {"scene_id": "sc_2", "duration_ms": 5000, "caption": "Scene 2 caption"},
            ],
            "is_vertical_9_16": True,
        },
    )
    assert res_export.status_code == 200
    export_json = res_export.json()
    assert export_json["project_name"] == "API_Test_Video"
    assert "draft_content_path" in export_json
    assert Path(export_json["draft_content_path"]).exists()

    # POST /providers/playwright/smoke-test
    res_pw = client.post(
        "/providers/playwright/smoke-test", json={"base_url": "http://testserver/app"}
    )
    assert res_pw.status_code == 200
    assert "page_title" in res_pw.json()
