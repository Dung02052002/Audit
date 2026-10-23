"""Tests for the BUDGET-01 provider cost classification and provider honesty."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.paid_api_budget import PaidTaskType
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.providers.image_generation import ImageGenerator
from ai_youtube_agent.providers.mock_research import MockResearchProvider
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderPricing,
    classify_provider,
    paid_declaration,
    task_type_of,
)
from ai_youtube_agent.providers.registry import (
    ComfyUIAdapter,
    EdgeTTSAdapter,
    ElevenLabsTTSAdapter,
    MockTTSAdapter,
    VideoGenApiAdapter,
    build_default_provider_registry,
)
from ai_youtube_agent.providers.research import ResearchProvider
from ai_youtube_agent.providers.research_cache import ResearchCache
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    SpeechSynthesizer,
    VoiceErrorCode,
)
from ai_youtube_agent.providers.text_generation import TextGenerator
from ai_youtube_agent.providers.video_generation import (
    VideoClipErrorCode,
    VideoClipGenerationError,
    VideoClipGenerationRequest,
    VideoClipGenerator,
)

FREE = {
    ProviderCostClass.MOCK,
    ProviderCostClass.LOCAL_FREE,
    ProviderCostClass.FREE_SERVICE,
}


class PaidVoice:
    name = "paid_voice"

    def cost_declaration(self):
        return paid_declaration(self.name, "v1", "test double")


class BrokenDeclaration:
    name = "broken"

    def cost_declaration(self):
        raise RuntimeError("no declaration")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "cost.db",
        artifact_dir=tmp_path / "artifacts",
    )


def test_registry_declarations_match_their_capabilities(settings: Settings) -> None:
    registry = build_default_provider_registry(settings)

    for adapter in registry.list_all():
        capability = adapter.capabilities()
        declaration = classify_provider(adapter)
        assert declaration.provider == adapter.name, adapter.provider_id
        if isinstance(adapter, MockTTSAdapter):
            assert declaration.cost_class is ProviderCostClass.MOCK
        elif capability.is_free and capability.is_local:
            assert declaration.cost_class is ProviderCostClass.LOCAL_FREE
        elif capability.is_free:
            assert declaration.cost_class is ProviderCostClass.FREE_SERVICE
        else:
            assert declaration.cost_class is ProviderCostClass.PAID
            assert declaration.pricing is None
            assert declaration.unknowns


def test_the_free_demo_providers_are_free() -> None:
    comfy = classify_provider(ComfyUIAdapter(base_url="http://127.0.0.1:59997"))
    edge = classify_provider(EdgeTTSAdapter())

    assert comfy.cost_class is ProviderCostClass.LOCAL_FREE
    assert comfy.model == ComfyUIAdapter().default_model
    assert edge.cost_class is ProviderCostClass.FREE_SERVICE
    assert edge.model == "vi-VN-HoaiMyNeural"


def test_the_paid_adapters_declare_no_price() -> None:
    for adapter in (
        ElevenLabsTTSAdapter(api_key="sk-test-not-real"),
        VideoGenApiAdapter(is_enabled=True, api_key="vg-test-not-real"),
    ):
        declaration = classify_provider(adapter)
        assert declaration.cost_class is ProviderCostClass.PAID
        assert declaration.price_known is False
        assert "giá chưa xác minh" in declaration.unknowns
        assert "hạn mức tài khoản chưa rõ" in declaration.unknowns
        assert "not-real" not in str(declaration.as_dict())


def test_the_container_providers_are_all_free(settings: Settings) -> None:
    container = build_container(settings)

    for interface in (
        SpeechSynthesizer,
        ImageGenerator,
        VideoClipGenerator,
        TextGenerator,
        ResearchProvider,
    ):
        declaration = classify_provider(container.resolve(interface))
        assert declaration.cost_class in FREE, interface.__name__
    assert (
        classify_provider(container.resolve(SpeechSynthesizer)).cost_class
        is ProviderCostClass.MOCK
    )


def test_classification_fails_closed() -> None:
    undeclared = classify_provider(object())
    broken = classify_provider(BrokenDeclaration())

    assert undeclared.cost_class is ProviderCostClass.UNKNOWN
    assert undeclared.is_free is False
    assert undeclared.pricing is None
    assert broken.cost_class is ProviderCostClass.UNKNOWN
    assert broken.provider == "broken"


def test_a_free_provider_with_a_paid_fallback_is_paid() -> None:
    edge = classify_provider(EdgeTTSAdapter(fallback=PaidVoice()))
    comfy = classify_provider(
        ComfyUIAdapter(base_url="http://127.0.0.1:59997", fallback_generator=object())
    )
    edge_free = classify_provider(EdgeTTSAdapter(fallback=MockSpeechSynthesizer()))

    assert edge.cost_class is ProviderCostClass.PAID
    assert edge.pricing is None
    assert comfy.cost_class is ProviderCostClass.UNKNOWN
    assert edge_free.cost_class is ProviderCostClass.FREE_SERVICE


def test_the_research_cache_costs_what_its_provider_costs(tmp_path: Path) -> None:
    cache = ResearchCache(MockResearchProvider(), Database(tmp_path / "unused.db"))

    assert classify_provider(cache).cost_class is ProviderCostClass.MOCK


@pytest.mark.parametrize(
    "overrides",
    [
        {"unit_price": Decimal("-1")},
        {"unit_price": 0.5},
        {"unit_price": Decimal("NaN")},
        {"currency": "usd"},
        {"source": " "},
        {"unit": ""},
    ],
)
def test_pricing_is_validated(overrides: dict) -> None:
    values = {
        "unit": "second",
        "unit_price": Decimal("0.05"),
        "currency": "USD",
        "source": "price sheet",
        "verified_at": date(2026, 10, 1),
        **overrides,
    }
    with pytest.raises(ValueError):
        ProviderPricing(**values)


# Provider honesty (no mock answer in place of a paid API)


def test_an_enabled_video_api_refuses_and_makes_no_clip() -> None:
    adapter = VideoGenApiAdapter(is_enabled=True, api_key="vg-test-not-real")

    capabilities = adapter.capabilities_video_clip()
    with pytest.raises(VideoClipGenerationError) as refused:
        adapter.generate_clip(
            VideoClipGenerationRequest(
                scene_id="sc_01",
                shot_id="sh_01",
                prompt="Cinematic street at night",
                duration_ms=4000,
                width=1080,
                height=1920,
            )
        )

    assert capabilities.available is False
    assert capabilities.is_mock is False
    assert "Chưa có tích hợp HTTP thật" in capabilities.reason
    assert refused.value.error_code is VideoClipErrorCode.NOT_CONFIGURED
    assert not hasattr(adapter, "_mock")


@pytest.mark.parametrize("api_key", [None, "sk-test-not-real"])
def test_elevenlabs_refuses_and_makes_no_mock_audio(api_key: str | None) -> None:
    adapter = ElevenLabsTTSAdapter(api_key=api_key)

    with pytest.raises(SpeechSynthesisError) as refused:
        adapter.synthesize(
            SpeechRequest(text="Xin chào", voice_id="rachel", language="vi")
        )

    assert refused.value.voice_code is VoiceErrorCode.UNAVAILABLE
    assert "not-real" not in refused.value.detail
    assert not hasattr(adapter, "_fallback")
    with pytest.raises(TypeError):
        ElevenLabsTTSAdapter(fallback=MockSpeechSynthesizer())


def test_task_type_of_names_the_kind_of_ai_call() -> None:
    assert task_type_of(MockSpeechSynthesizer()) is PaidTaskType.SPEECH_SYNTHESIS
    assert task_type_of(MockResearchProvider()) is PaidTaskType.RESEARCH
    assert task_type_of(ComfyUIAdapter()) is PaidTaskType.IMAGE_GENERATION
    assert task_type_of(VideoGenApiAdapter()) is PaidTaskType.VIDEO_CLIP_GENERATION
    assert task_type_of(object()) is PaidTaskType.TEXT_GENERATION
