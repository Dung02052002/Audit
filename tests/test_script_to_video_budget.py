"""BUDGET-01 tests of the Script-to-Video pipeline, runner and retry endpoint.

The paid path is proven with ``PricedVideoClipGenerator``, a priced test
double: no real paid adapter exists and none is called. Every run uses the
mock voice and renderer.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.test_paid_request_guard import PricedVideoClipGenerator
from tests.test_script_to_video import SAMPLE_SHORTS_SCRIPT

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.paid_api_budget import (
    PaidApiMode,
    PaidQuoteStatus,
    PaidRequestStatus,
    PaidTaskType,
)
from ai_youtube_agent.content.paid_api_budget_service import PaidApiBudgetService
from ai_youtube_agent.content.story_bible import ProductionMode
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import AuditLog
from ai_youtube_agent.core.config import Environment, RenderProviderKind, Settings
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.http import LOCAL_USER
from ai_youtube_agent.main import create_app
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.script_to_video import (
    SCRIPT_TO_VIDEO_JOB_KIND,
    ScriptToVideoError,
    ScriptToVideoErrorCode,
    ScriptToVideoRequest,
    ScriptToVideoService,
    ScriptToVideoStage,
)
from ai_youtube_agent.pipeline.script_to_video_runner import (
    RunnerError,
    RunnerErrorCode,
    RunSnapshot,
    RunStatus,
    ScriptToVideoRunner,
    StageState,
)
from ai_youtube_agent.providers.image_generation import ImageGenerator
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.mock_video_generation import MockVideoClipGenerator
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    ProviderPricing,
)
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    SpeechSynthesizer,
    SynthesizedSpeech,
    VoiceErrorCode,
)
from ai_youtube_agent.providers.video_generation import VideoClipGenerator

WAIT_SECONDS = 60.0
FINAL = (
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.AWAITING_APPROVAL,
    RunStatus.BLOCKED,
)


@pytest.fixture
def settings(database: Database, tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_path=database.path,
        artifact_dir=tmp_path / "artifacts",
        render_provider=RenderProviderKind.MOCK,
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    built = build_container(settings)
    yield built
    built.resolve(ScriptToVideoRunner).shutdown()


@pytest.fixture
def priced() -> PricedVideoClipGenerator:
    return PricedVideoClipGenerator()


class PricedVoice(MockSpeechSynthesizer):
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


def _film_request(**overrides: Any) -> ScriptToVideoRequest:
    values: dict[str, Any] = {
        "script_text": SAMPLE_SHORTS_SCRIPT,
        "title": "Phim AI có trả phí",
        "content_type": ContentType.SHORTS,
        "production_mode": ProductionMode.AI_FILM,
        "content_item_id": "item_budget_film_01",
    }
    values.update(overrides)
    return ScriptToVideoRequest(**values)


def _paid_mode(container: Container) -> None:
    container.resolve(PaidApiBudgetService).set_policy(
        PaidApiMode.PAID_WITH_PER_REQUEST_APPROVAL, actor=LOCAL_USER
    )


def _approve_all(container: Container, quote_ids: tuple[str, ...]) -> None:
    budget = container.resolve(PaidApiBudgetService)
    for quote_id in quote_ids:
        quote = budget.get_quote(quote_id)
        budget.approve(
            quote.id,
            actor=LOCAL_USER,
            request_hash=quote.request_hash,
            estimated_amount=quote.estimated_amount,
            currency=quote.currency,
        )


def _job(database: Database, job_id: str) -> AIJob:
    with database.transaction() as connection:
        job = AIJobRepository(connection).get(job_id)
    assert job is not None
    return job


def _wait_final(runner: ScriptToVideoRunner, run_id: str) -> RunSnapshot:
    deadline = time.monotonic() + WAIT_SECONDS
    while runner.require(run_id).status not in FINAL:
        if time.monotonic() > deadline:
            raise AssertionError("the run did not finish in time")
        time.sleep(0.02)
    return runner.require(run_id)


def _states(view: dict[str, Any]) -> dict[str, str]:
    return {stage["id"]: stage["state"] for stage in view["progress"]["stages"]}


# The pipeline


def test_a_default_run_is_free_and_checks_the_budget(
    container: Container, database: Database
) -> None:
    manifest = container.resolve(ScriptToVideoService).run(
        _film_request(
            production_mode=ProductionMode.STORY_VIDEO,
            content_item_id="item_budget_free_01",
        )
    )

    assert manifest.budget_status is PaidRequestStatus.FREE
    assert manifest.paid_quote_ids == ()
    assert manifest.as_dict()["budget_status"] == "free"
    assert ScriptToVideoStage.BUDGET_CHECKED in manifest.completed_stages
    checkpoint = next(
        saved
        for saved in _job(database, manifest.job_id).checkpoints
        if saved.step == ScriptToVideoStage.BUDGET_CHECKED.value
    )
    assert dict(checkpoint.data) == {
        "mode": "free_only",
        "status": "free",
        "paid_providers": "",
    }
    assert manifest.capabilities.cost_classes == {
        "voice": "mock",
        "image": "local_free",
        "video_clip": "mock",
    }
    assert container.resolve(PaidApiBudgetService).list_quotes() == []


def test_free_only_blocks_a_paid_clip_provider_before_any_call(
    container: Container, database: Database, priced: PricedVideoClipGenerator
) -> None:
    with container.override(VideoClipGenerator, lambda _c: priced):
        service = container.resolve(ScriptToVideoService)
        capabilities = service.check_capabilities()

        with pytest.raises(ScriptToVideoError) as stopped:
            service.run(_film_request())

    error = stopped.value
    assert ProductionMode.AI_FILM not in capabilities.supported_modes
    assert capabilities.budget_blocked_modes == (ProductionMode.AI_FILM,)
    assert (
        "bị chặn bởi chính sách ngân sách"
        in (capabilities.unavailable_reasons["ai_film"])
    )
    assert error.code == ScriptToVideoErrorCode.BUDGET_BLOCKED.value
    assert error.budget_status is PaidRequestStatus.BLOCKED
    assert error.stage is ScriptToVideoStage.STORY_BIBLE_BUILT
    assert error.to_public().http_status == 409
    assert "Chỉ miễn phí" in error.user_message
    assert priced.calls == []
    assert priced.checks == 0
    (quote,) = container.resolve(PaidApiBudgetService).list_quotes()
    assert quote.status is PaidQuoteStatus.BLOCKED
    assert error.quote_ids == (quote.id,)
    assert quote.content_item_id == "item_budget_film_01"
    job = _job(database, quote.job_id)
    assert job.status is AIJobStatus.FAILED
    assert job.last_error == "budget.paid_provider_blocked_free_only"


def test_paid_mode_waits_for_approval_and_the_rerun_consumes_it(
    container: Container, database: Database, priced: PricedVideoClipGenerator
) -> None:
    _paid_mode(container)
    with container.override(VideoClipGenerator, lambda _c: priced):
        service = container.resolve(ScriptToVideoService)
        with pytest.raises(ScriptToVideoError) as stopped:
            service.run(_film_request())

        error = stopped.value
        budget = container.resolve(PaidApiBudgetService)
        quotes = [budget.get_quote(quote_id) for quote_id in error.quote_ids]
        assert error.code == ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL.value
        assert error.budget_status is PaidRequestStatus.AWAITING_APPROVAL
        assert error.stage is ScriptToVideoStage.SCENES_PLANNED
        assert priced.calls == []
        assert len(quotes) >= 1
        assert all(q.status is PaidQuoteStatus.PENDING for q in quotes)
        assert all(q.scene_id and q.duration_ms for q in quotes)
        assert all(q.task_type is PaidTaskType.VIDEO_CLIP_GENERATION for q in quotes)
        job = _job(database, quotes[0].job_id)
        assert job.status is AIJobStatus.FAILED
        assert job.last_error == "budget.paid_request_awaiting_approval"

        _approve_all(container, error.quote_ids)
        manifest = service.run(_film_request())

    assert manifest.budget_status is PaidRequestStatus.PAID_APPROVED
    assert set(manifest.paid_quote_ids) == set(error.quote_ids)
    assert len(priced.calls) == len(quotes)
    consumed = [budget.get_quote(quote_id) for quote_id in error.quote_ids]
    assert all(q.status is PaidQuoteStatus.CONSUMED for q in consumed)
    # The re-run asked for exactly the requests that were quoted.
    called = {
        budget.get_quote(quote_id).request_hash for quote_id in manifest.paid_quote_ids
    }
    assert called == {q.request_hash for q in quotes}
    assert all(sa.media_type == "video/mp4" for sa in manifest.scene_assets)


def test_a_paid_voice_has_every_chunk_quoted_before_the_first_call(
    container: Container,
) -> None:
    _paid_mode(container)
    voice = PricedVoice()
    request = _film_request(
        production_mode=ProductionMode.STORY_VIDEO,
        content_item_id="item_budget_voice_01",
    )
    with container.override(SpeechSynthesizer, lambda _c: voice):
        service = container.resolve(ScriptToVideoService)
        with pytest.raises(ScriptToVideoError) as stopped:
            service.run(request)

        error = stopped.value
        assert error.code == ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL.value
        assert error.stage is ScriptToVideoStage.BUDGET_CHECKED
        assert voice.calls == []
        budget = container.resolve(PaidApiBudgetService)
        quotes = [budget.get_quote(quote_id) for quote_id in error.quote_ids]
        assert all(q.task_type is PaidTaskType.SPEECH_SYNTHESIS for q in quotes)
        assert all(q.unit == "character" for q in quotes)

        _approve_all(container, error.quote_ids)
        manifest = service.run(request)

    assert manifest.budget_status is PaidRequestStatus.PAID_APPROVED
    assert set(manifest.paid_quote_ids) == set(error.quote_ids)
    assert len(voice.calls) == len(quotes)
    assert all(
        budget.get_quote(quote_id).status is PaidQuoteStatus.CONSUMED
        for quote_id in error.quote_ids
    )


class FlakyPricedVoice(PricedVoice):
    """A priced voice whose call number ``fail_at`` answers 429 (retryable)."""

    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self.fail_at = fail_at
        self.attempts = 0

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        self.attempts += 1
        if self.attempts == self.fail_at:
            raise SpeechSynthesisError(
                VoiceErrorCode.RATE_LIMITED, "429", provider=self.name
            )
        return super().synthesize(request)


def _audio_jobs(database: Database) -> list[tuple[str, str | None]]:
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT status, last_error FROM ai_jobs WHERE kind = 'audio.generate'"
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def test_a_budget_stop_inside_tts_fails_the_audio_job_and_requotes_only_the_rest(
    container: Container, database: Database
) -> None:
    _paid_mode(container)
    voice = FlakyPricedVoice(fail_at=2)
    request = _film_request(
        production_mode=ProductionMode.STORY_VIDEO,
        content_item_id="item_budget_voice_retry",
    )
    budget = container.resolve(PaidApiBudgetService)
    with container.override(SpeechSynthesizer, lambda _c: voice):
        service = container.resolve(ScriptToVideoService)
        with pytest.raises(ScriptToVideoError) as first:
            service.run(request)
        planned = first.value.quote_ids
        assert len(planned) >= 2
        _approve_all(container, planned)

        # Chunk 0 is made; chunk 1 consumes its approval, answers 429, and the
        # retry inside the TTS job needs a new approval instead of a call.
        with pytest.raises(ScriptToVideoError) as second:
            service.run(request)
        assert second.value.code == (
            ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL.value
        )
        assert len(second.value.quote_ids) == 1
        assert second.value.quote_ids[0] not in planned
        assert _audio_jobs(database) == [
            ("failed", "budget.paid_request_awaiting_approval")
        ]
        assert voice.attempts == 2

        # The re-run quotes nothing for the stored chunk 0: only the new line
        # is approved, and every other approval from the first pass is used.
        _approve_all(container, second.value.quote_ids)
        manifest = service.run(request)

    assert manifest.budget_status is PaidRequestStatus.PAID_APPROVED
    assert len(voice.calls) == len(planned)
    assert voice.attempts == len(planned) + 1
    assert _audio_jobs(database)[0][0] == "succeeded"
    quotes = budget.list_quotes(content_item_id=request.content_item_id, limit=200)
    assert len(quotes) == len(planned) + 1
    statuses = {quote.id: quote.status for quote in quotes}
    assert all(
        statuses[quote_id] is PaidQuoteStatus.CONSUMED
        for quote_id in (*planned, *second.value.quote_ids)
    )


def test_a_repeated_speech_text_is_quoted_once_per_call(
    container: Container,
) -> None:
    _paid_mode(container)
    voice = PricedVoice()
    line = "Narrator: Ánh đèn vàng phản chiếu trên mặt bàn gỗ tĩnh lặng giữa đêm mưa."
    script = "\n".join(
        (
            "SCENE 1: PHÒNG TRÀ CỔ - BAN ĐÊM",
            line,
            "Minh (trầm ngâm): Mỗi tách trà nóng đều giữ lại hương vị của một mùa cũ.",
            line,
        )
    )
    request = _film_request(
        script_text=script,
        production_mode=ProductionMode.STORY_VIDEO,
        content_item_id="item_budget_voice_repeat",
    )
    with container.override(SpeechSynthesizer, lambda _c: voice):
        service = container.resolve(ScriptToVideoService)
        with pytest.raises(ScriptToVideoError) as stopped:
            service.run(request)
        _approve_all(container, stopped.value.quote_ids)
        manifest = service.run(request)

    texts = [call.text for call in voice.calls]
    assert len(set(texts)) < len(texts)
    assert len(stopped.value.quote_ids) == len(texts)
    assert set(manifest.paid_quote_ids) == set(stopped.value.quote_ids)


def test_the_service_cannot_be_built_without_the_guard(
    container: Container,
) -> None:
    with pytest.raises(TypeError):
        ScriptToVideoService(
            container.resolve(Database),
            container.resolve(ArtifactStorage),
            container.resolve(AuditLog),
            speech_synthesizer=MockSpeechSynthesizer(),
            image_generator=container.resolve(ImageGenerator),
            video_clip_generator=MockVideoClipGenerator(),
            video_renderer=MockVideoRenderer(),
        )


# The runner


def test_the_runner_ends_a_free_only_paid_run_blocked(
    container: Container, priced: PricedVideoClipGenerator
) -> None:
    with container.override(VideoClipGenerator, lambda _c: priced):
        runner = container.resolve(ScriptToVideoRunner)
        snapshot, created = runner.submit(_film_request(), actor=LOCAL_USER)
        done = _wait_final(runner, snapshot.run_id)
        view = runner.describe(done)

    assert created is True
    assert done.status is RunStatus.BLOCKED
    assert done.error is not None
    assert done.error.code == ScriptToVideoErrorCode.BUDGET_BLOCKED.value
    assert done.error.budget_status == "blocked"
    assert done.error.cause_code == "budget.paid_provider_blocked_free_only"
    assert len(done.error.quote_ids) == 1
    assert view["status"] == "blocked"
    assert view["error"]["quote_ids"] == list(done.error.quote_ids)
    assert _states(view)["budget.checked"] == StageState.BLOCKED.value
    assert view["progress"]["current_stage"] == "budget.checked"
    assert priced.calls == []


def test_the_runner_ends_a_paid_run_awaiting_approval(
    container: Container, priced: PricedVideoClipGenerator
) -> None:
    _paid_mode(container)
    with container.override(VideoClipGenerator, lambda _c: priced):
        runner = container.resolve(ScriptToVideoRunner)
        snapshot, _ = runner.submit(_film_request(), actor=LOCAL_USER)
        done = _wait_final(runner, snapshot.run_id)
        view = runner.describe(done)

    assert done.status is RunStatus.AWAITING_APPROVAL
    assert done.error is not None
    assert done.error.code == ScriptToVideoErrorCode.AWAITING_PAID_APPROVAL.value
    assert done.error.budget_status == "awaiting_approval"
    assert done.error.quote_ids
    states = _states(view)
    assert states["budget.checked"] == StageState.DONE.value
    assert states["scenes.planned"] == StageState.DONE.value
    assert states["assets.ready"] == StageState.WAITING.value
    assert view["progress"]["current_stage"] == "assets.ready"
    assert priced.calls == []


@pytest.mark.parametrize(
    ("last_error", "status"),
    [
        ("budget.paid_request_awaiting_approval", RunStatus.AWAITING_APPROVAL),
        ("budget.paid_provider_blocked_free_only", RunStatus.BLOCKED),
        ("budget.price_unknown", RunStatus.BLOCKED),
        ("budget.provider_cost_unknown", RunStatus.BLOCKED),
        ("pipeline.script_to_video_stage_failed", RunStatus.FAILED),
    ],
)
def test_an_untracked_budget_failed_job_reads_as_its_budget_status(
    container: Container, database: Database, last_error: str, status: RunStatus
) -> None:
    runner = container.resolve(ScriptToVideoRunner)
    job = AIJob.create(SCRIPT_TO_VIDEO_JOB_KIND, f"s2v:{last_error}").start()
    job = job.fail(last_error)
    with database.transaction() as connection:
        AIJobRepository(connection).add(job)

    assert runner.job_status(job) is status
    assert runner.job_view(job.id)["status"] == status.value


# The retry endpoint


def _wait_http(client: TestClient, location: str) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        run = client.get(location).json()
        if run["status"] in {status.value for status in FINAL}:
            return run
        if time.monotonic() > deadline:
            raise AssertionError("the run did not finish in time")
        time.sleep(0.05)


def test_approve_and_retry_over_http(
    container: Container, priced: PricedVideoClipGenerator
) -> None:
    payload = {
        "script_text": SAMPLE_SHORTS_SCRIPT,
        "title": "Phim AI có trả phí",
        "content_type": "shorts",
        "production_mode": "ai_film",
    }
    with container.override(VideoClipGenerator, lambda _c: priced):
        client = TestClient(create_app(container))
        assert (
            client.put(
                "/budget/paid-api-policy",
                json={"mode": "paid_with_per_request_approval"},
            ).status_code
            == 200
        )
        submitted = client.post("/script-to-video/background-runs", json=payload)
        assert submitted.status_code == 202
        stopped = _wait_http(client, submitted.headers["Location"])
        assert stopped["status"] == "awaiting_approval"
        quotes = client.get(
            "/budget/paid-api-quotes", params={"job_id": stopped["job_id"]}
        ).json()["quotes"]
        assert {quote["id"] for quote in quotes} == set(stopped["error"]["quote_ids"])
        for quote in quotes:
            approved = client.post(
                f"/budget/paid-api-quotes/{quote['id']}/approve",
                json={
                    "request_hash": quote["request_hash"],
                    "estimated_amount": quote["estimated_amount"],
                    "currency": quote["currency"],
                },
            )
            assert approved.status_code == 200

        retried = client.post(
            f"/script-to-video/background-runs/{stopped['run_id']}/retry"
        )
        assert retried.status_code == 202
        assert retried.headers["Location"].startswith(
            "/script-to-video/background-runs/run_"
        )
        assert retried.json()["run_id"] != stopped["run_id"]
        done = _wait_http(client, retried.headers["Location"])

        again = client.post(f"/script-to-video/background-runs/{done['run_id']}/retry")
        missing = client.post("/script-to-video/background-runs/run_missing/retry")

    assert done["status"] == "succeeded"
    assert done["job_id"] == stopped["job_id"]
    assert len(priced.calls) == len(quotes)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "domain.run_not_retryable"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "domain.run_not_found"
    statuses = {
        quote["status"]
        for quote in client.get(
            "/budget/paid-api-quotes", params={"job_id": done["job_id"]}
        ).json()["quotes"]
    }
    assert statuses == {"consumed"}


def test_an_active_run_cannot_be_retried(container: Container) -> None:
    # A runner without a worker keeps its run queued.
    idle = ScriptToVideoRunner(
        lambda: container.resolve(ScriptToVideoService),
        container.resolve(Database),
        start_worker=False,
    )
    try:
        snapshot, _ = idle.submit(
            _film_request(production_mode=ProductionMode.STORY_VIDEO),
            actor=LOCAL_USER,
        )
        with pytest.raises(RunnerError) as refused:
            idle.retry(snapshot.run_id, actor=LOCAL_USER)
    finally:
        idle.shutdown()

    assert refused.value.code == RunnerErrorCode.RUN_NOT_RETRYABLE.value
    assert refused.value.to_public().http_status == 409
