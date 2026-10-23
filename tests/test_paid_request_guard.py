"""Tests for the BUDGET-01 PaidRequestGuard and its provider wrappers.

The approval flow is proven with priced test doubles: no real paid adapter
exists, and none is ever called here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tests.factories import (
    make_ai_job,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

from ai_youtube_agent.content.paid_api_budget import (
    PaidApiMode,
    PaidBudgetErrorCode,
    PaidQuoteError,
    PaidQuoteStatus,
    PaidRequestAwaitingApprovalError,
    PaidRequestBlockedError,
    PaidRequestContext,
    PaidRequestStatus,
    PaidTaskType,
)
from ai_youtube_agent.content.paid_api_budget_service import PaidApiBudgetService
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.pipeline.paid_request_guard import (
    BUDGET_BLOCKED_REASON,
    PAID_APPROVAL_REQUIRED,
    PaidRequestGuard,
    image_spec,
    speech_occurrences,
    speech_spec,
)
from ai_youtube_agent.providers.image_generation import ImageGenerationRequest
from ai_youtube_agent.providers.mock_image_generation import (
    DeterministicSceneImageGenerator,
)
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.mock_video_generation import MockVideoClipGenerator
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    ProviderPricing,
    paid_declaration,
)
from ai_youtube_agent.providers.registry.comfyui_adapter import ComfyUIAdapter
from ai_youtube_agent.providers.registry.tts_adapters import (
    EdgeTTSAdapter,
    ElevenLabsTTSAdapter,
)
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    VoiceErrorCode,
)
from ai_youtube_agent.providers.video_generation import (
    GeneratedVideoClip,
    VideoClipErrorCode,
    VideoClipGenerationError,
    VideoClipGenerationRequest,
    VideoClipGeneratorCapabilities,
)

T0 = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "local-user")
PRICING = ProviderPricing(
    unit="second",
    unit_price=Decimal("0.10"),
    currency="USD",
    source="test price sheet",
    verified_at=date(2026, 10, 1),
    conditions=("trả theo giây clip",),
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class PricedVideoClipGenerator:
    """A paid clip provider with a known price (a test double, no network)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.name = "priced_video"
        self.model_name = "clip-v1"
        self.fail = fail
        self.calls: list[VideoClipGenerationRequest] = []
        self.checks = 0
        self._mock = MockVideoClipGenerator(name="priced_video")

    def cost_declaration(self) -> ProviderCostDeclaration:
        return ProviderCostDeclaration(
            provider=self.name,
            model=self.model_name,
            cost_class=ProviderCostClass.PAID,
            pricing=PRICING,
            basis="test double",
            unknowns=("hạn mức tài khoản chưa rõ",),
        )

    def capabilities(self) -> VideoClipGeneratorCapabilities:
        return VideoClipGeneratorCapabilities(
            provider_name=self.name,
            model_name=self.model_name,
            available=True,
            is_mock=False,
        )

    def generate_clip(self, request: VideoClipGenerationRequest) -> GeneratedVideoClip:
        self.calls.append(request)
        if self.fail:
            raise VideoClipGenerationError(
                VideoClipErrorCode.GENERATION_FAILED, "scripted", provider=self.name
            )
        return self._mock.generate_clip(request)

    def check(self) -> None:
        self.checks += 1


class UnpricedPaidSpeech:
    """A paid voice without a verified price."""

    name = "unpriced_voice"

    def __init__(self) -> None:
        self.calls = 0

    def cost_declaration(self) -> ProviderCostDeclaration:
        return paid_declaration(self.name, "voice-v1", "test double")

    def synthesize(self, request: SpeechRequest):
        self.calls += 1
        raise AssertionError("a paid provider must never be called")

    def check(self) -> None:
        self.calls += 1


class UndeclaredSpeech:
    """A provider that says nothing about its cost."""

    name = "mystery_voice"

    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, request: SpeechRequest):
        self.calls += 1
        raise AssertionError("an undeclared provider must never be called")

    def check(self) -> None:
        self.calls += 1


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def budget(database: Database, clock: Clock) -> PaidApiBudgetService:
    return PaidApiBudgetService(
        database,
        AuditLog(InMemoryAuditSink()),
        quote_ttl=timedelta(minutes=30),
        clock=clock,
    )


@pytest.fixture
def guard(budget: PaidApiBudgetService) -> PaidRequestGuard:
    return PaidRequestGuard(budget)


def paid_mode(budget: PaidApiBudgetService) -> None:
    budget.set_policy(PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL, actor=USER)


def clip_request(scene: str = "scene_1", duration_ms: int = 4000):
    return VideoClipGenerationRequest(
        scene_id=scene,
        shot_id=f"{scene}_shot_1",
        prompt=f"Một con phố nhỏ buổi tối ({scene})",
        duration_ms=duration_ms,
        width=1080,
        height=1920,
    )


def speech_request(text: str = "Xin chào các bạn") -> SpeechRequest:
    return SpeechRequest(text=text, voice_id="vi-VN-HoaiMyNeural", language="vi")


def approve(budget: PaidApiBudgetService, quote_id: str):
    quote = budget.get_quote(quote_id)
    return budget.approve(
        quote.id,
        actor=USER,
        request_hash=quote.request_hash,
        estimated_amount=quote.estimated_amount,
        currency=quote.currency,
    )


# Free providers


@pytest.mark.parametrize(
    "mode", [PaidApiMode.FREE_ONLY, PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL]
)
def test_a_free_provider_runs_in_both_modes(
    guard: PaidRequestGuard, budget: PaidApiBudgetService, mode: PaidApiMode
) -> None:
    budget.set_policy(mode, actor=USER)
    inner = MockSpeechSynthesizer()
    speech = guard.wrap_speech(inner)

    result = speech.synthesize(speech_request())

    assert result.provider == "mock"
    assert len(inner.calls) == 1
    assert speech.authorize_batch([speech_request()]) is PaidRequestStatus.FREE
    assert guard.preflight([speech]).status == PaidRequestStatus.FREE.value
    assert budget.list_quotes() == []


def test_a_free_quota_running_out_propagates_and_no_paid_provider_is_called(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    free = MockSpeechSynthesizer()
    free.fail_next(VoiceErrorCode.RATE_LIMITED)
    paid = UnpricedPaidSpeech()
    speech = guard.wrap_speech(free)

    with pytest.raises(SpeechSynthesisError) as failed:
        speech.synthesize(speech_request())

    assert failed.value.voice_code is VoiceErrorCode.RATE_LIMITED
    assert paid.calls == 0
    assert budget.list_quotes() == []


# Free only


def test_free_only_blocks_a_paid_provider_without_calling_it(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    inner = PricedVideoClipGenerator()
    clips = guard.wrap_video(inner)

    with pytest.raises(PaidRequestBlockedError) as blocked:
        clips.generate_clip(clip_request())

    error = blocked.value
    assert error.code == PaidBudgetErrorCode.BLOCKED_FREE_ONLY.value
    assert error.status is PaidRequestStatus.BLOCKED
    assert "Chỉ miễn phí" in error.user_message
    assert "ComfyUI" in error.user_message
    assert error.to_public().http_status == 409
    assert inner.calls == []
    (quote,) = budget.list_quotes()
    assert quote.status is PaidQuoteStatus.BLOCKED
    assert quote.blocked_reason == "budget.paid_provider_blocked_free_only"
    assert error.quote_ids == (quote.id,)


def test_free_only_reports_a_paid_wrapper_blocked_without_calling_it(
    guard: PaidRequestGuard,
) -> None:
    inner = PricedVideoClipGenerator()
    clips = guard.wrap_video(inner)

    capabilities = clips.capabilities_video_clip()
    with pytest.raises(PaidRequestBlockedError):
        clips.check()

    assert capabilities.available is False
    assert capabilities.reason == BUDGET_BLOCKED_REASON
    assert "bị chặn bởi chính sách ngân sách" in capabilities.reason
    assert inner.checks == 0
    assert clips.budget_blocked_reason() == BUDGET_BLOCKED_REASON


def test_preflight_in_free_only_blocks_and_records_every_paid_provider(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    with pytest.raises(PaidRequestBlockedError) as blocked:
        guard.preflight(
            [MockSpeechSynthesizer(), PricedVideoClipGenerator(), UnpricedPaidSpeech()]
        )

    quotes = budget.list_quotes()
    assert len(quotes) == 2
    assert {q.provider for q in quotes} == {"priced_video", "unpriced_voice"}
    assert {q.task_type for q in quotes} == {
        PaidTaskType.VIDEO_CLIP_GENERATION,
        PaidTaskType.SPEECH_SYNTHESIS,
    }
    assert set(blocked.value.quote_ids) == {q.id for q in quotes}


def test_preflight_in_paid_mode_needs_approvals_and_writes_nothing(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)

    preflight = guard.preflight([MockSpeechSynthesizer(), PricedVideoClipGenerator()])

    assert preflight.mode is PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL
    assert preflight.status == PAID_APPROVAL_REQUIRED
    assert preflight.paid_providers == ("priced_video",)
    assert budget.list_quotes() == []


def test_preflight_of_the_free_demo_providers_is_free(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    preflight = guard.preflight(
        [ComfyUIAdapter(base_url="http://127.0.0.1:59997"), EdgeTTSAdapter()]
    )

    assert preflight.status == "free"
    assert preflight.mode is PaidApiMode.FREE_ONLY
    assert [d.cost_class for d in preflight.declarations] == [
        ProviderCostClass.LOCAL_FREE,
        ProviderCostClass.FREE_SERVICE,
    ]
    assert budget.list_quotes() == []


# Unknown price and undeclared providers


def test_a_paid_provider_without_a_price_is_blocked_and_not_approvable(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = UnpricedPaidSpeech()
    speech = guard.wrap_speech(inner)

    with pytest.raises(PaidRequestBlockedError) as blocked:
        speech.synthesize(speech_request())

    assert blocked.value.code == PaidBudgetErrorCode.PRICE_UNKNOWN.value
    assert inner.calls == 0
    (quote,) = budget.list_quotes()
    assert quote.status is PaidQuoteStatus.BLOCKED
    assert quote.price_known is False
    with pytest.raises(PaidQuoteError) as refused:
        budget.approve(
            quote.id,
            actor=USER,
            request_hash=quote.request_hash,
            estimated_amount=Decimal("0"),
            currency="USD",
        )
    assert refused.value.code == PaidBudgetErrorCode.QUOTE_NOT_PENDING.value


@pytest.mark.parametrize(
    "mode", [PaidApiMode.FREE_ONLY, PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL]
)
def test_an_undeclared_provider_is_unknown_and_blocked(
    guard: PaidRequestGuard, budget: PaidApiBudgetService, mode: PaidApiMode
) -> None:
    budget.set_policy(mode, actor=USER)
    inner = UndeclaredSpeech()
    speech = guard.wrap_speech(inner)

    with pytest.raises(PaidRequestBlockedError) as blocked:
        speech.synthesize(speech_request())

    assert blocked.value.code == PaidBudgetErrorCode.PROVIDER_COST_UNKNOWN.value
    assert speech.cost_declaration().cost_class is ProviderCostClass.UNKNOWN
    assert inner.calls == 0


def test_the_real_paid_adapters_are_blocked_in_paid_mode_too(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    speech = guard.wrap_speech(ElevenLabsTTSAdapter(api_key="sk-test-not-real"))

    with pytest.raises(PaidRequestBlockedError) as blocked:
        speech.synthesize(speech_request())

    assert blocked.value.code == PaidBudgetErrorCode.PRICE_UNKNOWN.value
    assert "sk-test-not-real" not in blocked.value.user_message
    assert "sk-test-not-real" not in blocked.value.detail


# The approval flow (priced test double)


def test_a_priced_call_waits_for_approval_then_runs_once(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = PricedVideoClipGenerator()
    clips = guard.wrap_video(inner)

    with pytest.raises(PaidRequestAwaitingApprovalError) as waiting:
        clips.generate_clip(clip_request())

    assert waiting.value.status is PaidRequestStatus.AWAITING_APPROVAL
    assert waiting.value.code == "budget.paid_request_awaiting_approval"
    assert inner.calls == []
    (quote_id,) = waiting.value.quote_ids
    quote = budget.get_quote(quote_id)
    assert quote.status is PaidQuoteStatus.PENDING
    assert quote.provider == "priced_video"
    assert quote.model == "clip-v1"
    assert quote.task_type is PaidTaskType.VIDEO_CLIP_GENERATION
    assert quote.unit == "second"
    assert quote.unit_count == Decimal("4")
    assert quote.duration_ms == 4000
    assert quote.scene_id == "scene_1"
    assert quote.unit_price == Decimal("0.10")
    assert quote.estimated_amount == Decimal("0.400000")
    assert quote.currency == "USD"
    assert quote.conditions == ("trả theo giây clip",)
    assert quote.unknowns == ("hạn mức tài khoản chưa rõ",)
    assert quote.price_source.startswith("test price sheet")

    approve(budget, quote_id)
    with guard.bind(PaidRequestContext()) as session:
        clip = clips.generate_clip(clip_request())

    assert clip.provider == "priced_video"
    assert len(inner.calls) == 1
    assert session.status is PaidRequestStatus.PAID_APPROVED
    assert session.consumed_quote_ids == [quote_id]
    assert budget.get_quote(quote_id).status is PaidQuoteStatus.CONSUMED

    with pytest.raises(PaidRequestAwaitingApprovalError) as again:
        clips.generate_clip(clip_request())
    assert again.value.quote_ids != (quote_id,)
    assert len(inner.calls) == 1


def test_an_approval_of_another_request_does_not_cover_this_one(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = PricedVideoClipGenerator()
    clips = guard.wrap_video(inner)
    with pytest.raises(PaidRequestAwaitingApprovalError) as first:
        clips.generate_clip(clip_request("scene_1"))
    approve(budget, first.value.quote_ids[0])

    with pytest.raises(PaidRequestAwaitingApprovalError) as other:
        clips.generate_clip(clip_request("scene_2"))

    assert other.value.quote_ids != first.value.quote_ids
    assert inner.calls == []
    assert budget.get_quote(first.value.quote_ids[0]).status is (
        PaidQuoteStatus.APPROVED
    )


def test_an_expired_approval_asks_for_a_new_quote(
    guard: PaidRequestGuard, budget: PaidApiBudgetService, clock: Clock
) -> None:
    paid_mode(budget)
    inner = PricedVideoClipGenerator()
    clips = guard.wrap_video(inner)
    with pytest.raises(PaidRequestAwaitingApprovalError) as first:
        clips.generate_clip(clip_request())
    approve(budget, first.value.quote_ids[0])
    clock.now += timedelta(minutes=31)

    with pytest.raises(PaidRequestAwaitingApprovalError) as second:
        clips.generate_clip(clip_request())

    assert second.value.quote_ids != first.value.quote_ids
    assert budget.get_quote(first.value.quote_ids[0]).status is (
        PaidQuoteStatus.EXPIRED
    )
    assert inner.calls == []


def test_a_provider_failure_after_the_approval_propagates_without_a_retry(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = PricedVideoClipGenerator(fail=True)
    backup = MockVideoClipGenerator()
    clips = guard.wrap_video(inner)
    with pytest.raises(PaidRequestAwaitingApprovalError) as waiting:
        clips.generate_clip(clip_request())
    approve(budget, waiting.value.quote_ids[0])

    with pytest.raises(VideoClipGenerationError):
        clips.generate_clip(clip_request())
    with pytest.raises(PaidRequestAwaitingApprovalError):
        clips.generate_clip(clip_request())

    assert len(inner.calls) == 1
    assert backup.calls == []
    assert budget.get_quote(waiting.value.quote_ids[0]).status is (
        PaidQuoteStatus.CONSUMED
    )


def test_a_batch_is_quoted_in_one_pass_and_each_call_consumes_its_line(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = PricedVideoClipGenerator()
    clips = guard.wrap_video(inner)
    requests = [clip_request(f"scene_{n}", 2000 + n * 500) for n in range(1, 4)]

    with pytest.raises(PaidRequestAwaitingApprovalError) as waiting:
        clips.authorize_batch(requests)

    assert len(waiting.value.quote_ids) == 3
    assert inner.calls == []
    for quote_id in waiting.value.quote_ids:
        approve(budget, quote_id)
    assert clips.authorize_batch(requests) is PaidRequestStatus.PAID_APPROVED
    assert all(
        budget.get_quote(quote_id).status is PaidQuoteStatus.APPROVED
        for quote_id in waiting.value.quote_ids
    )
    for request in requests:
        clips.generate_clip(request)
    assert len(inner.calls) == 3
    assert all(
        budget.get_quote(quote_id).status is PaidQuoteStatus.CONSUMED
        for quote_id in waiting.value.quote_ids
    )


def test_a_batch_quotes_only_the_lines_not_yet_approved(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    clips = guard.wrap_video(PricedVideoClipGenerator())
    requests = [clip_request("scene_1"), clip_request("scene_2")]
    with pytest.raises(PaidRequestAwaitingApprovalError) as first:
        clips.authorize_batch(requests[:1])
    approve(budget, first.value.quote_ids[0])

    with pytest.raises(PaidRequestAwaitingApprovalError) as second:
        clips.authorize_batch(requests)

    assert len(second.value.quote_ids) == 1
    assert second.value.quote_ids != first.value.quote_ids


def test_bound_calls_record_their_item_and_job(
    database: Database, guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    job = make_ai_job(item)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
        AIJobRepository(connection).add(job)
    clips = guard.wrap_video(PricedVideoClipGenerator())

    with (
        guard.bind(PaidRequestContext(item.id, job.id)),
        pytest.raises(PaidRequestAwaitingApprovalError) as waiting,
    ):
        clips.generate_clip(clip_request())
    assert guard.context == PaidRequestContext()

    quote = budget.get_quote(waiting.value.quote_ids[0])
    assert quote.content_item_id == item.id
    assert quote.job_id == job.id
    assert [q.id for q in budget.list_quotes(job_id=job.id)] == [quote.id]


# Wrappers


def test_wrappers_keep_the_inner_name_and_expose_no_bypass(
    guard: PaidRequestGuard,
) -> None:
    edge = EdgeTTSAdapter()
    comfy = ComfyUIAdapter(base_url="http://127.0.0.1:59997")
    speech = guard.wrap_speech(edge)
    image = guard.wrap_image(comfy)
    clips = guard.wrap_video(MockVideoClipGenerator())

    assert speech.name == "edge_tts"
    assert speech.default_voice == "vi-VN-HoaiMyNeural"
    assert speech.output_format == "audio/mpeg"
    assert image.name == "comfyui"
    assert image.last_job is None
    assert clips.name == "mock_video_clip"
    assert clips.model_name == "mock-film-clip-v1"
    for wrapper, helper in (
        (speech, "synthesize_to_file"),
        (speech, "synthesize_wav"),
        (speech, "with_wav_output"),
        (image, "generate_and_store"),
        (image, "generate_with_job"),
        (image, "save_to_directory"),
    ):
        assert not hasattr(wrapper, helper), helper
        with pytest.raises(AttributeError):
            getattr(wrapper, helper)


def test_a_provider_is_wrapped_once_per_guard(guard: PaidRequestGuard) -> None:
    speech = guard.wrap_speech(MockSpeechSynthesizer())
    image = guard.wrap_image(DeterministicSceneImageGenerator())

    assert guard.wrap_speech(speech) is speech
    assert guard.wrap_image(image) is image


# Fingerprints and repeated speech


class PricedSpeech(MockSpeechSynthesizer):
    """A paid voice priced per character (a test double: mock WAV audio)."""

    def __init__(self) -> None:
        super().__init__(name="priced_voice")

    def cost_declaration(self) -> ProviderCostDeclaration:
        return ProviderCostDeclaration(
            provider=self.name,
            model="voice-v1",
            cost_class=ProviderCostClass.PAID,
            pricing=ProviderPricing(
                unit="character",
                unit_price=Decimal("0.0001"),
                currency="USD",
                source="test price sheet",
                verified_at=date(2026, 10, 1),
            ),
        )


def test_the_speech_fingerprint_holds_the_style_and_the_occurrence() -> None:
    declaration = PricedSpeech().cost_declaration()
    plain = speech_request()
    styled = SpeechRequest(
        text=plain.text,
        voice_id=plain.voice_id,
        language=plain.language,
        speaking_style="cheerful",
    )

    first = speech_spec(declaration, plain).request_hash

    assert speech_spec(declaration, speech_request()).request_hash == first
    assert speech_spec(declaration, plain, 0).request_hash == first
    assert speech_spec(declaration, styled).request_hash != first
    assert speech_spec(declaration, plain, 1).request_hash != first
    assert speech_occurrences(
        [plain, speech_request("Khác"), plain, styled, plain]
    ) == [0, 0, 1, 0, 2]


def test_the_image_fingerprint_holds_the_palette_and_the_shot() -> None:
    declaration = DeterministicSceneImageGenerator().cost_declaration()

    def request(**overrides: object) -> ImageGenerationRequest:
        values: dict[str, object] = {
            "scene_id": "scene_1",
            "shot_id": "scene_1_shot_1",
            "prompt": "Một con phố nhỏ buổi tối",
            "width": 1080,
            "height": 1920,
            "palette_rgb": ((10, 20, 30),),
        }
        values.update(overrides)
        return ImageGenerationRequest(**values)

    first = image_spec(declaration, request()).request_hash

    assert image_spec(declaration, request()).request_hash == first
    assert image_spec(declaration, request(shot_id="scene_1_shot_2")).request_hash != (
        first
    )
    assert (
        image_spec(declaration, request(palette_rgb=((10, 20, 31),))).request_hash
        != first
    )


def test_a_repeated_speech_text_needs_one_approval_per_call(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = PricedSpeech()
    speech = guard.wrap_speech(inner)
    plan = [speech_request(), speech_request("Hẹn gặp lại"), speech_request()]

    with (
        guard.bind(PaidRequestContext()),
        pytest.raises(PaidRequestAwaitingApprovalError) as waiting,
    ):
        speech.authorize_batch(plan)
    assert len(waiting.value.quote_ids) == 3
    for quote_id in waiting.value.quote_ids:
        approve(budget, quote_id)

    with guard.bind(PaidRequestContext()) as session:
        assert speech.authorize_batch(plan) is PaidRequestStatus.PAID_APPROVED
        for request in plan:
            speech.synthesize(request)
        with pytest.raises(PaidRequestAwaitingApprovalError):
            speech.synthesize(plan[0])

    assert len(inner.calls) == 3
    assert sorted(session.consumed_quote_ids) == sorted(waiting.value.quote_ids)


def test_reused_speech_positions_are_not_quoted_but_keep_their_occurrence(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = PricedSpeech()
    speech = guard.wrap_speech(inner)
    plan = [speech_request(), speech_request("Hẹn gặp lại"), speech_request()]

    with (
        guard.bind(PaidRequestContext()),
        pytest.raises(PaidRequestAwaitingApprovalError) as waiting,
    ):
        speech.authorize_batch(plan, reused={0})

    declaration = inner.cost_declaration()
    assert {budget.get_quote(q).request_hash for q in waiting.value.quote_ids} == {
        speech_spec(declaration, plan[1], 0).request_hash,
        speech_spec(declaration, plan[2], 1).request_hash,
    }
    for quote_id in waiting.value.quote_ids:
        approve(budget, quote_id)
    with guard.bind(PaidRequestContext()) as session:
        speech.authorize_batch(plan, reused={0})
        speech.synthesize(plan[1])
        speech.synthesize(plan[2])
    assert sorted(session.consumed_quote_ids) == sorted(waiting.value.quote_ids)


def test_a_failed_speech_call_keeps_its_occurrence_and_needs_a_new_approval(
    guard: PaidRequestGuard, budget: PaidApiBudgetService
) -> None:
    paid_mode(budget)
    inner = PricedSpeech()
    speech = guard.wrap_speech(inner)
    plan = [speech_request(), speech_request()]
    with (
        guard.bind(PaidRequestContext()),
        pytest.raises(PaidRequestAwaitingApprovalError) as waiting,
    ):
        speech.authorize_batch(plan)
    for quote_id in waiting.value.quote_ids:
        approve(budget, quote_id)
    inner.fail_next(VoiceErrorCode.RATE_LIMITED)

    with guard.bind(PaidRequestContext()):
        speech.authorize_batch(plan)
        with pytest.raises(SpeechSynthesisError):
            speech.synthesize(plan[0])
        with pytest.raises(PaidRequestAwaitingApprovalError) as again:
            speech.synthesize(plan[0])

    retry = budget.get_quote(again.value.quote_ids[0])
    assert retry.id not in waiting.value.quote_ids
    assert retry.request_hash == (
        speech_spec(inner.cost_declaration(), plan[0], 0).request_hash
    )
    assert len(inner.calls) == 1
