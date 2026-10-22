"""Tests for the APP-01 background Script-to-Video runner.

Every run uses the mock providers (never ffmpeg) and every wait has a timeout.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.factories import make_channel, make_content_item, make_strategy_profile
from tests.test_script_to_video import SAMPLE_SHORTS_SCRIPT

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.story_bible import ProductionMode
from ai_youtube_agent.core.config import Environment, RenderProviderKind, Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.di import Container
from ai_youtube_agent.core.http import LOCAL_USER
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus, JobCheckpoint
from ai_youtube_agent.pipeline.script_to_video import (
    SCRIPT_TO_VIDEO_JOB_KIND,
    USER_MESSAGES,
    ScriptToVideoError,
    ScriptToVideoErrorCode,
    ScriptToVideoRequest,
    ScriptToVideoService,
    ScriptToVideoStage,
)
from ai_youtube_agent.pipeline.script_to_video_runner import (
    PIPELINE_STAGE_ORDER,
    RunnerError,
    RunnerErrorCode,
    RunSnapshot,
    RunStatus,
    ScriptToVideoRunner,
    StageState,
    job_progress,
)
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer
from ai_youtube_agent.providers.video_render import (
    RenderedVideo,
    RenderErrorCode,
    RenderRequest,
    VideoRenderer,
)

WAIT_SECONDS = 60.0
FINAL = (RunStatus.SUCCEEDED, RunStatus.FAILED)


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


def _request(**overrides: Any) -> ScriptToVideoRequest:
    values: dict[str, Any] = {
        "script_text": SAMPLE_SHORTS_SCRIPT,
        "title": "Hương Trà Đêm Mưa",
        "content_type": ContentType.SHORTS,
        "production_mode": ProductionMode.STORY_VIDEO,
        "subtitles_enabled": True,
    }
    values.update(overrides)
    return ScriptToVideoRequest(**values)


def _wait_for(condition: Callable[[], bool], *, timeout: float = WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        time.sleep(0.02)


def _wait_final(runner: ScriptToVideoRunner, run_id: str) -> RunSnapshot:
    _wait_for(lambda: runner.require(run_id).status in FINAL)
    return runner.require(run_id)


def _states(view: dict[str, Any]) -> dict[str, str]:
    return {stage["id"]: stage["state"] for stage in view["progress"]["stages"]}


class BlockingRenderer:
    """Wraps the mock renderer and holds every render until released."""

    def __init__(self) -> None:
        self.name = "mock"
        self._inner = MockVideoRenderer()
        self.entered = threading.Event()
        self.release = threading.Event()

    def render(self, request: RenderRequest) -> RenderedVideo:
        self.entered.set()
        if not self.release.wait(timeout=WAIT_SECONDS):
            raise AssertionError("the blocking renderer was never released")
        return self._inner.render(request)

    def check(self) -> None:
        self._inner.check()


def test_a_submitted_run_succeeds_in_the_background(
    container: Container, database: Database
) -> None:
    runner = container.resolve(ScriptToVideoRunner)

    snapshot, created = runner.submit(_request(), actor=LOCAL_USER)
    assert created is True
    assert snapshot.run_id.startswith("run_")
    assert snapshot.status in (RunStatus.QUEUED, RunStatus.RUNNING)

    done = _wait_final(runner, snapshot.run_id)

    assert done.status is RunStatus.SUCCEEDED
    assert done.error is None
    assert done.job_id is not None
    assert done.content_item_id is not None
    assert done.started_at is not None and done.finished_at is not None
    assert done.result is not None
    assert done.result.video_size_bytes > 0
    assert done.result.scene_count == 3
    assert done.result.approval_id is None
    assert done.request.script_chars == len(SAMPLE_SHORTS_SCRIPT)

    view = runner.describe(done)
    states = _states(view)
    assert states[ScriptToVideoStage.VIDEO_RENDERED.value] == "done"
    assert states[ScriptToVideoStage.SUBTITLES_GENERATED.value] == "done"
    assert states[ScriptToVideoStage.APPROVAL_REQUESTED.value] == "skipped"
    assert view["progress"]["completed"] == len(PIPELINE_STAGE_ORDER) - 1
    assert view["progress"]["reused"] is False
    assert view["progress"]["current_stage"] is None
    assert "script_text" not in view["request"]

    with database.transaction() as connection:
        job = AIJobRepository(connection).get(done.job_id)
    assert job is not None
    assert job.status is AIJobStatus.SUCCEEDED
    assert job.content_item_id == done.content_item_id
    assert runner.latest_manifest(done.content_item_id) is not None
    assert runner.active_run_id(done.content_item_id) is None


def test_a_running_run_shows_its_job_and_active_stage(
    container: Container,
) -> None:
    renderer = BlockingRenderer()
    with container.override(VideoRenderer, lambda _c: renderer):
        runner = container.resolve(ScriptToVideoRunner)
        snapshot, _ = runner.submit(_request(), actor=LOCAL_USER)
        try:
            assert renderer.entered.wait(timeout=WAIT_SECONDS)

            running = runner.require(snapshot.run_id)
            assert running.status is RunStatus.RUNNING
            assert running.job_id is not None
            assert runner.active_run_id(running.content_item_id) == snapshot.run_id
            states = _states(runner.describe(running))
            assert states[ScriptToVideoStage.THUMBNAIL_GENERATED.value] == "done"
            assert states[ScriptToVideoStage.AUDIO_MIXED.value] == "active"
            assert states[ScriptToVideoStage.VIDEO_RENDERED.value] == "pending"

            job = runner.job_view(running.job_id)
            assert job["tracked"] is True
            assert job["status"] == "running"
            assert job["progress"]["current_stage"] == "audio.mixed"
        finally:
            renderer.release.set()
        done = _wait_final(runner, snapshot.run_id)

    assert done.status is RunStatus.SUCCEEDED


def test_a_failing_render_fails_the_run_with_safe_fields(
    container: Container, database: Database
) -> None:
    renderer = MockVideoRenderer()
    renderer.fail_next(RenderErrorCode.FAILED)
    with container.override(VideoRenderer, lambda _c: renderer):
        runner = container.resolve(ScriptToVideoRunner)
        snapshot, _ = runner.submit(_request(), actor=LOCAL_USER)
        done = _wait_final(runner, snapshot.run_id)

    assert done.status is RunStatus.FAILED
    assert done.result is None
    assert done.error is not None
    assert done.error.code == ScriptToVideoErrorCode.STAGE_FAILED.value
    assert done.error.message == USER_MESSAGES[ScriptToVideoErrorCode.STAGE_FAILED]
    assert done.error.stage == ScriptToVideoStage.THUMBNAIL_GENERATED.value
    assert done.error.cause_code is not None
    assert done.error.retryable is False
    assert set(done.error.as_dict()) == {
        "code",
        "message",
        "stage",
        "cause_code",
        "retryable",
    }

    view = runner.describe(done)
    assert _states(view)[ScriptToVideoStage.AUDIO_MIXED.value] == "failed"
    assert view["progress"]["current_stage"] == ScriptToVideoStage.AUDIO_MIXED.value
    with database.transaction() as connection:
        job = AIJobRepository(connection).get(done.job_id)
    assert job is not None
    assert job.status is AIJobStatus.FAILED


def test_an_unavailable_mode_is_refused_before_it_is_queued(
    container: Container,
) -> None:
    runner = container.resolve(ScriptToVideoRunner)

    with pytest.raises(ScriptToVideoError) as refused:
        runner.submit(
            _request(production_mode=ProductionMode.AI_FILM), actor=LOCAL_USER
        )

    assert refused.value.code == ScriptToVideoErrorCode.MODE_UNAVAILABLE.value
    assert runner.list_runs() == []


def test_a_blank_script_is_refused_before_it_is_queued(
    container: Container,
) -> None:
    runner = container.resolve(ScriptToVideoRunner)

    for blank in (None, "", "   \n "):
        with pytest.raises(ScriptToVideoError) as refused:
            runner.submit(_request(script_text=blank), actor=LOCAL_USER)
        assert refused.value.code == ScriptToVideoErrorCode.EMPTY_SCRIPT.value

    assert runner.list_runs() == []


class _VanishingModeService:
    """Supports every mode at submit time, then fails like a mode that vanished."""

    def check_capabilities(self) -> Any:
        class _Capabilities:
            unavailable_reasons: dict[str, str] = {}

            def supports_mode(self, _mode: ProductionMode) -> bool:
                return True

        return _Capabilities()

    def run(self, request: ScriptToVideoRequest, **_kwargs: Any) -> Any:
        raise ScriptToVideoError(
            "secret internal detail",
            code=ScriptToVideoErrorCode.MODE_UNAVAILABLE,
            stage=ScriptToVideoStage.STORY_BIBLE_BUILT,
        )


class _CrashingService(_VanishingModeService):
    def run(self, request: ScriptToVideoRequest, **_kwargs: Any) -> Any:
        raise RuntimeError("secret internal detail")


def test_a_run_failing_before_its_job_has_no_job_id(database: Database) -> None:
    runner = ScriptToVideoRunner(lambda: _VanishingModeService(), database)
    try:
        snapshot, _ = runner.submit(
            _request(production_mode=ProductionMode.AI_FILM), actor=LOCAL_USER
        )
        done = _wait_final(runner, snapshot.run_id)
    finally:
        runner.shutdown()

    assert done.status is RunStatus.FAILED
    assert done.job_id is None
    assert done.error is not None
    assert done.error.code == ScriptToVideoErrorCode.MODE_UNAVAILABLE.value
    assert done.error.stage == ScriptToVideoStage.STORY_BIBLE_BUILT.value
    assert "secret" not in str(done.as_dict())
    progress = runner.describe(done)["progress"]
    # No stage ran; only the approval the request turned off is skipped.
    assert {
        stage["id"]: stage["state"]
        for stage in progress["stages"]
        if stage["state"] != "pending"
    } == {ScriptToVideoStage.APPROVAL_REQUESTED.value: "skipped"}


def test_an_unexpected_error_is_a_generic_internal_error(database: Database) -> None:
    runner = ScriptToVideoRunner(lambda: _CrashingService(), database)
    try:
        snapshot, _ = runner.submit(_request(), actor=LOCAL_USER)
        done = _wait_final(runner, snapshot.run_id)
    finally:
        runner.shutdown()

    assert done.status is RunStatus.FAILED
    assert done.error is not None
    assert done.error.code == "application.internal"
    assert done.error.cause_code is None
    assert "secret" not in str(done.as_dict())


def test_the_same_payload_while_active_returns_the_same_run(
    container: Container, database: Database
) -> None:
    runner = ScriptToVideoRunner(
        lambda: container.resolve(ScriptToVideoService),
        database,
        start_worker=False,
    )

    first, first_created = runner.submit(_request(), actor=LOCAL_USER)
    again, again_created = runner.submit(_request(), actor=LOCAL_USER)
    other, other_created = runner.submit(
        _request(visual_style="cinematic_watercolor"), actor=LOCAL_USER
    )

    assert first_created is True
    assert again_created is False
    assert again.run_id == first.run_id
    assert other_created is True
    assert other.run_id != first.run_id
    assert [run.run_id for run in runner.list_runs()] == [first.run_id, other.run_id]
    runner.shutdown()


def test_shutdown_cancels_queued_runs_and_refuses_new_ones(
    container: Container, database: Database
) -> None:
    runner = ScriptToVideoRunner(
        lambda: container.resolve(ScriptToVideoService),
        database,
        start_worker=False,
    )
    snapshot, _ = runner.submit(_request(), actor=LOCAL_USER)
    assert snapshot.status is RunStatus.QUEUED

    runner.shutdown()
    runner.shutdown()

    cancelled = runner.require(snapshot.run_id)
    assert cancelled.status is RunStatus.FAILED
    assert cancelled.finished_at is not None
    assert cancelled.error is not None
    assert cancelled.error.code == RunnerErrorCode.RUN_CANCELLED.value
    assert cancelled.error.retryable is True
    with pytest.raises(RunnerError) as refused:
        runner.submit(_request(title="Another"), actor=LOCAL_USER)
    assert refused.value.code == RunnerErrorCode.RUNNER_STOPPED.value
    assert refused.value.to_public().http_status == 503


def test_an_untracked_running_job_reads_as_interrupted(
    container: Container, database: Database
) -> None:
    runner = container.resolve(ScriptToVideoRunner)
    job = AIJob.create(SCRIPT_TO_VIDEO_JOB_KIND, "s2v:untracked").start()
    job = job.checkpoint(ScriptToVideoStage.SCRIPT_PARSED.value, {"beat_count": 3})
    with database.transaction() as connection:
        AIJobRepository(connection).add(job)

    view = runner.job_view(job.id)

    assert view["status"] == "interrupted"
    assert view["tracked"] is False
    assert view["job"]["status"] == "running"
    assert view["job"]["checkpoints"][0]["step"] == "script.parsed"
    assert "idempotency_key" not in view["job"]
    assert runner.job_status(job) is RunStatus.INTERRUPTED


def test_unknown_runs_and_jobs_are_not_found(container: Container) -> None:
    runner = container.resolve(ScriptToVideoRunner)

    assert runner.get("run_missing") is None
    with pytest.raises(RunnerError) as missing_run:
        runner.require("run_missing")
    assert missing_run.value.code == RunnerErrorCode.RUN_NOT_FOUND.value
    with pytest.raises(RunnerError) as missing_job:
        runner.job_view("job_missing")
    assert missing_job.value.code == RunnerErrorCode.JOB_NOT_FOUND.value


def test_the_registry_evicts_the_oldest_finished_runs(database: Database) -> None:
    runner = ScriptToVideoRunner(lambda: _VanishingModeService(), database, history=2)
    try:
        run_ids = []
        for style in ("a", "b", "c"):
            snapshot, _ = runner.submit(_request(visual_style=style), actor=LOCAL_USER)
            _wait_final(runner, snapshot.run_id)
            run_ids.append(snapshot.run_id)
    finally:
        runner.shutdown()

    assert runner.get(run_ids[0]) is None
    assert [run.run_id for run in runner.list_runs()] == [run_ids[2], run_ids[1]]


def test_the_sync_run_is_unchanged_and_the_hook_is_optional(
    container: Container,
) -> None:
    service = container.resolve(ScriptToVideoService)
    calls: list[tuple[str, str]] = []

    plain = service.run(_request(content_item_id="item_runner_sync_01"))

    def hook(job_id: str, item_id: str) -> None:
        calls.append((job_id, item_id))
        raise RuntimeError("a broken hook must not stop the run")

    hooked = service.run(
        _request(content_item_id="item_runner_sync_01"), on_job_started=hook
    )

    assert plain.status is hooked.status
    assert hooked.video_sha256 == plain.video_sha256
    assert calls == [(hooked.job_id, hooked.content_item_id)]


# job_progress


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def _job_with(steps: list[tuple[str, datetime]], status: AIJobStatus) -> AIJob:
    job = AIJob.create(SCRIPT_TO_VIDEO_JOB_KIND, "s2v:progress", clock=lambda: NOW)
    checkpoints = tuple(
        JobCheckpoint(index + 1, step, moment)
        for index, (step, moment) in enumerate(steps)
    )
    updated_at = max((moment for _, moment in steps), default=NOW)
    return replace(
        job,
        status=status,
        attempts=1,
        last_error="stage failed" if status is AIJobStatus.FAILED else None,
        checkpoints=checkpoints,
        updated_at=updated_at,
    )


def test_progress_of_a_succeeded_job_entered_again_is_reused() -> None:
    steps = [(stage.value, NOW) for stage in PIPELINE_STAGE_ORDER[:-1]]
    job = _job_with(steps, AIJobStatus.SUCCEEDED)

    progress = job_progress(job, RunStatus.SUCCEEDED, since=NOW + timedelta(minutes=5))

    assert progress.reused is True
    assert progress.completed == len(PIPELINE_STAGE_ORDER) - 1
    assert progress.stages[-1].state is StageState.SKIPPED


def test_progress_follows_only_the_latest_attempt() -> None:
    later = NOW + timedelta(minutes=1)
    steps = [
        (ScriptToVideoStage.SCRIPT_PARSED.value, NOW),
        (ScriptToVideoStage.STORY_BIBLE_BUILT.value, NOW),
        (ScriptToVideoStage.VOICE_GENERATED.value, NOW),
        (ScriptToVideoStage.SCRIPT_PARSED.value, later),
    ]
    job = _job_with(steps, AIJobStatus.RUNNING)

    progress = job_progress(job, RunStatus.RUNNING, since=later)

    states = {stage.id: stage.state for stage in progress.stages}
    assert states["script.parsed"] is StageState.DONE
    assert states["story_bible.built"] is StageState.ACTIVE
    assert states["voice.generated"] is StageState.PENDING
    assert progress.current_stage == "story_bible.built"
    assert progress.completed == 1


def test_progress_skips_an_optional_stage_that_was_passed() -> None:
    stages = [
        stage
        for stage in PIPELINE_STAGE_ORDER[:8]
        if stage is not ScriptToVideoStage.SUBTITLES_GENERATED
    ]
    job = _job_with([(stage.value, NOW) for stage in stages], AIJobStatus.FAILED)

    progress = job_progress(job, RunStatus.FAILED)

    states = {stage.id: stage.state for stage in progress.stages}
    assert states["subtitles.generated"] is StageState.SKIPPED
    assert states["audio.mixed"] is StageState.FAILED
    assert states["video.rendered"] is StageState.PENDING


def test_progress_skips_a_disabled_optional_stage_up_front() -> None:
    reached = PIPELINE_STAGE_ORDER[
        : PIPELINE_STAGE_ORDER.index(ScriptToVideoStage.TIMELINE_BUILT) + 1
    ]
    job = _job_with([(stage.value, NOW) for stage in reached], AIJobStatus.RUNNING)

    progress = job_progress(
        job,
        RunStatus.RUNNING,
        disabled_stages=frozenset({ScriptToVideoStage.SUBTITLES_GENERATED}),
    )

    states = {stage.id: stage.state for stage in progress.stages}
    assert states["subtitles.generated"] is StageState.SKIPPED
    assert states["thumbnail.generated"] is StageState.ACTIVE
    assert states["approval.requested"] is StageState.PENDING
    assert progress.current_stage == "thumbnail.generated"


class _FailsAtThumbnailService(_VanishingModeService):
    """Records the stages up to the timeline, then fails at the thumbnail."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def run(
        self,
        request: ScriptToVideoRequest,
        *,
        on_job_started: Callable[[str, str], None] | None = None,
        **_kwargs: Any,
    ) -> Any:
        job = AIJob.create(SCRIPT_TO_VIDEO_JOB_KIND, "s2v:thumbnail-fails").start()
        last = PIPELINE_STAGE_ORDER.index(ScriptToVideoStage.TIMELINE_BUILT)
        for stage in PIPELINE_STAGE_ORDER[: last + 1]:
            job = job.checkpoint(stage.value)
        job = job.fail("thumbnail failed")
        with self._database.transaction() as connection:
            AIJobRepository(connection).add(job)
        assert on_job_started is not None
        on_job_started(job.id, "item_thumbnail_fails")
        raise ScriptToVideoError(
            "thumbnail failed",
            code=ScriptToVideoErrorCode.STAGE_FAILED,
            stage=ScriptToVideoStage.TIMELINE_BUILT,
        )


def test_a_failure_after_disabled_subtitles_fails_the_thumbnail_step(
    database: Database,
) -> None:
    runner = ScriptToVideoRunner(lambda: _FailsAtThumbnailService(database), database)
    try:
        snapshot, _ = runner.submit(_request(subtitles_enabled=False), actor=LOCAL_USER)
        done = _wait_final(runner, snapshot.run_id)
    finally:
        runner.shutdown()

    assert done.status is RunStatus.FAILED
    assert done.job_id is not None
    view = runner.describe(done)
    states = _states(view)
    assert states[ScriptToVideoStage.SUBTITLES_GENERATED.value] == "skipped"
    assert states[ScriptToVideoStage.THUMBNAIL_GENERATED.value] == "failed"
    assert states[ScriptToVideoStage.APPROVAL_REQUESTED.value] == "skipped"
    assert view["progress"]["current_stage"] == (
        ScriptToVideoStage.THUMBNAIL_GENERATED.value
    )
    # Without the request the optional stage is never chosen as the next one.
    job_states = {
        stage["id"]: stage["state"]
        for stage in runner.job_view(done.job_id)["progress"]["stages"]
    }
    assert job_states[ScriptToVideoStage.THUMBNAIL_GENERATED.value] == "failed"
    assert job_states[ScriptToVideoStage.SUBTITLES_GENERATED.value] != "failed"


def test_a_queued_run_rechecks_that_its_item_is_editable(
    container: Container, database: Database
) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = make_content_item(channel, strategy)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
    renderer = BlockingRenderer()
    with container.override(VideoRenderer, lambda _c: renderer):
        runner = container.resolve(ScriptToVideoRunner)
        first, _ = runner.submit(_request(), actor=LOCAL_USER)
        try:
            assert renderer.entered.wait(timeout=WAIT_SECONDS)
            queued, created = runner.submit(
                _request(content_item_id=item.id), actor=LOCAL_USER
            )
            assert created is True
            assert runner.require(queued.run_id).status is RunStatus.QUEUED
            approved = replace(
                item,
                status=ContentStatus.APPROVED,
                updated_at=item.updated_at + timedelta(seconds=1),
            )
            with database.transaction() as connection:
                ContentItemRepository(connection).update(
                    approved, expected_updated_at=item.updated_at
                )
        finally:
            renderer.release.set()
        assert _wait_final(runner, first.run_id).status is RunStatus.SUCCEEDED
        done = _wait_final(runner, queued.run_id)

    assert done.status is RunStatus.FAILED
    assert done.job_id is None
    assert done.error is not None
    assert done.error.code == RunnerErrorCode.ITEM_NOT_EDITABLE.value
    assert done.error.stage is None
    with database.transaction() as connection:
        assert AIJobRepository(connection).list_by_content_item(item.id) == []


def test_a_run_for_another_channel_is_a_new_run(
    container: Container, database: Database
) -> None:
    runner = ScriptToVideoRunner(
        lambda: container.resolve(ScriptToVideoService),
        database,
        start_worker=False,
    )

    first, _ = runner.submit(_request(channel_id="channel_a"), actor=LOCAL_USER)
    other, created = runner.submit(_request(channel_id="channel_b"), actor=LOCAL_USER)

    assert created is True
    assert other.run_id != first.run_id
    runner.shutdown()


def test_runs_finished_at_the_same_moment_keep_their_order(
    database: Database,
) -> None:
    runner = ScriptToVideoRunner(
        lambda: _VanishingModeService(), database, clock=lambda: NOW, history=2
    )
    try:
        run_ids = []
        for style in ("a", "b", "c"):
            snapshot, _ = runner.submit(_request(visual_style=style), actor=LOCAL_USER)
            _wait_final(runner, snapshot.run_id)
            run_ids.append(snapshot.run_id)
    finally:
        runner.shutdown()

    assert runner.get(run_ids[0]) is None
    assert [run.run_id for run in runner.list_runs()] == [run_ids[2], run_ids[1]]
