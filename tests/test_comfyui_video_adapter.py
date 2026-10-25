"""Unit tests for ComfyUIVideoAdapter.

Ensures ComfyUIVideoAdapter behaves honestly:
1. Declares local_free cost class (0 VND API budget).
2. Supports text2video and image2video modalities.
3. Fails cleanly with honest diagnostic when ComfyUI is offline or weights are missing.
4. Never silently fakes video clips with mock data when real generation fails.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
import pytest

from ai_youtube_agent.providers.provider_cost import ProviderCostClass
from ai_youtube_agent.providers.registry.comfyui_adapter import HttpResponse
from ai_youtube_agent.providers.registry.comfyui_video_adapter import ComfyUIVideoAdapter
from ai_youtube_agent.providers.registry.models import ProviderCategory, ProviderStatus
from ai_youtube_agent.providers.video_generation import (
    VideoClipGenerationError,
    VideoClipGenerationRequest,
)


class FakeTransport:
    """Configurable in-memory transport for testing ComfyUIVideoAdapter without network."""

    def __init__(
        self,
        handlers: dict[str, Any] | None = None,
    ) -> None:
        self.handlers = handlers or {}
        self.recorded_requests: list[tuple[str, str, bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        self.recorded_requests.append((method, url, body))
        for pattern, handler in self.handlers.items():
            if pattern in url:
                if isinstance(handler, Exception):
                    raise handler
                status, resp_body = handler
                return HttpResponse(status=status, body=resp_body)
        raise ConnectionRefusedError(f"Connection refused to {url}")


def test_comfyui_video_adapter_capabilities() -> None:
    adapter = ComfyUIVideoAdapter()
    cap = adapter.capabilities()
    assert cap.provider_id == "comfyui_video"
    assert cap.category == ProviderCategory.VIDEO_GENERATION
    assert cap.is_local is True
    assert cap.is_free is True
    assert "text2video" in cap.supported_modalities
    assert "image2video" in cap.supported_modalities

    cost = adapter.cost_declaration()
    assert cost.cost_class == ProviderCostClass.LOCAL_FREE
    assert cost.is_free is True
    assert cost.pricing is None


def test_comfyui_video_adapter_offline_diagnostics() -> None:
    # Default adapter pointing to an unreachable port
    adapter = ComfyUIVideoAdapter(base_url="http://127.0.0.1:9999", timeout_seconds=0.1)
    diag = adapter.diagnose()
    assert diag.status == ProviderStatus.UNAVAILABLE
    assert diag.connection.connected is False
    assert diag.models.valid is False


def test_comfyui_video_adapter_detects_motion_models() -> None:
    object_info = {
        "CheckpointLoaderSimple": {
            "input": {
                "required": {
                    "ckpt_name": [
                        [
                            "v1-5-pruned-emaonly.safetensors",
                            "ltxv-2b-0.9.8-distilled-fp8.safetensors",
                            "mm_sd_v15_v2.ckpt",
                        ]
                    ]
                }
            }
        }
    }
    transport = FakeTransport(
        handlers={"/object_info": (200, json.dumps(object_info).encode("utf-8"))}
    )
    adapter = ComfyUIVideoAdapter(transport=transport)
    model_check = adapter.check_models()
    assert model_check.valid is True
    assert "ltxv-2b-0.9.8-distilled-fp8.safetensors" in model_check.available_models
    assert "mm_sd_v15_v2.ckpt" in model_check.available_models


def test_comfyui_video_adapter_offline_raises_honest_error(tmp_path: Path) -> None:
    adapter = ComfyUIVideoAdapter(base_url="http://127.0.0.1:9999", timeout_seconds=0.1)
    req = VideoClipGenerationRequest(
        scene_id="sc_01",
        shot_id="sh_01",
        prompt="A man walking in rainy Hanoi street",
        duration_ms=3000,
        width=512,
        height=512,
        fps=24,
    )

    with pytest.raises(VideoClipGenerationError) as exc_info:
        adapter.generate_clip(req)

    err_msg = str(exc_info.value)
    assert "ComfyUI video engine không phản hồi" in err_msg or "9999" in err_msg or "unavailable" in err_msg.lower() or "connection refused" in err_msg.lower()
