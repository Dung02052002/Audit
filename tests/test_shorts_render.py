"""I-100 Shorts render service (Prompt Pack v8, prompt #100).

Rules decided by the user on 2026-10-18:

- ``ShortsRenderService.render`` stores the MP4 of a ``ready`` project as the
  next VIDEO artifact version (D2: the project row is not changed);
- the request: the project's canvas, frame rate and duration, one layer per
  visual scene asset (background, b-roll, overlays; absolute times; boxes in
  pixels), one text line per wrapped caption line (D4) and the mix as audio
  (D6: mixed again on every call);
- the job (D1): an ``AIJob`` of kind ``video.render``; the same inputs on top
  of the same latest video are a replay without a renderer call, write or
  audit; a running job is ``in_progress`` unless taken over;
- refusals before anything is stored: unknown project, another version, not
  ready, stale script or audio, the I-096 asset checks, a changed provenance
  (D7) and unreadable or unsupported files (D3);
- a renderer failure or an invalid MP4 fails the job and is audited as
  ``shorts.render_failed``; a failure after the file was written deletes it;
- the ``shorts.rendered`` event has ids, checksums and counts, no text.
"""

import dataclasses
import itertools
from datetime import timedelta
from pathlib import Path

import pytest
from factories import USER, at, make_approval_request, make_audio_metadata, make_channel
from test_audio_mix import tone
from test_shorts_audio_mix import DURATION_MS, T1, VOICE_RATE, FakeSource, Shop
from test_shorts_project import World

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.audio_mix import PcmAudio, encode_wav
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.shorts_audio_mix import (
    MIXED,
    ShortsMixError,
    ShortsMixErrorCode,
)
from ai_youtube_agent.content.shorts_captions import caption_box, wrap
from ai_youtube_agent.content.shorts_project import ShortsProjectStatus
from ai_youtube_agent.content.shorts_render import (
    HTTP_STATUS,
    RENDER_FAILED,
    RENDERED,
    RENDERED_STEP,
    USER_MESSAGES,
    ArtifactVisualSource,
    RenderResult,
    ShortsRenderError,
    ShortsRenderErrorCode,
    ShortsRenderService,
    VisualFile,
    VisualSource,
    build_render_request,
)
from ai_youtube_agent.content.shorts_timeline import (
    AssetFit,
    AssetRole,
    Box,
    SceneAsset,
    Timeline,
)
from ai_youtube_agent.content.strategy import ShortsFormat
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.idempotency import RENDER_KIND
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer, stub_mp4
from ai_youtube_agent.providers.video_render import (
    MP4_MEDIA_TYPE,
    LayerFit,
    PixelRect,
    RenderErrorCode,
    VideoRenderError,
    probe_mp4,
)

Code = ShortsRenderErrorCode


class FakeVisualSource:
    """Visual files kept in memory: asset id -> ``VisualFile``."""

    def __init__(self) -> None:
        self.files: dict[str, VisualFile] = {}
        self.reads: list[str] = []

    def read_file(self, asset: Asset) -> VisualFile:
        self.reads.append(asset.id)
        if asset.id not in self.files:
            raise FileNotFoundError(asset.id)
        return self.files[asset.id]


class Studio(Shop):
    """A ready Shorts project over a real voice file, with pictures to place."""

    def __init__(
        self,
        database: Database,
        tmp_path: Path,
        *,
        captions: bool = False,
        plan: bool = True,
        ready: bool = True,
        voice: PcmAudio | None = None,
    ) -> None:
        self.database = database
        self.world = World(database, ShortsFormat(15, 60, captions=captions))
        self.storage = LocalFileStorage(tmp_path / "artifacts")
        self.voice = voice or tone(DURATION_MS, 0.3, rate=VOICE_RATE)
        self.source = FakeSource()
        self.visuals = FakeVisualSource()
        self.renderer = MockVideoRenderer(clock=at(T1))
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))
        item = self.world.item
        script = Script.create(
            item.id,
            sections=[
                ScriptSection.create(SectionKind.BODY, text)
                for text in ("one two", "three four five", "six seven eight nine ten")
            ],
            clock=at(T1),
        )
        data = encode_wav(self.voice)
        stored = self.storage.write(
            item.id, ArtifactKind.AUDIO, 1, data, media_type="audio/wav"
        )
        self.voice_artifact = Artifact.create(
            item.id,
            ArtifactKind.AUDIO,
            uri=stored.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type="audio/wav",
            clock=at(T1),
        )
        with database.transaction() as connection:
            ScriptRepository(connection).add(script)
            ArtifactRepository(connection).add(self.voice_artifact)
            AudioMetadataRepository(connection).add(
                make_audio_metadata(
                    self.voice_artifact,
                    script,
                    self.world.voice,
                    duration_ms=self.voice.duration_ms,
                )
            )
        self.project = self.world.service.create(item.id, actor=USER)
        if plan:
            self.project = self.world.service.plan(
                self.project.id, expected_version=self.project.version, actor=USER
            )
        self.backgrounds: list[Asset] = []
        if plan and ready:
            self.backgrounds = [self.picture() for _ in self.project.timeline.scenes]
            self.save(
                self.build(
                    sfx=[
                        (index, SceneAsset(background.id, AssetRole.BACKGROUND))
                        for index, background in enumerate(self.backgrounds)
                    ]
                )
            )
            if captions:
                self.project = self.world.service.caption(
                    self.project.id, expected_version=self.project.version, actor=USER
                )

    def picture(
        self,
        kind=AssetKind.IMAGE,
        media_type: str = "image/png",
        data: bytes | None = None,
        category=AssetCategory.USER_OWNED,
        **flags,
    ) -> Asset:
        """A registered, attached visual whose file the fake source serves."""
        asset = self.add_asset(kind, category, **flags)
        content = data or f"picture {asset.id}".encode()
        self.visuals.files[asset.id] = VisualFile.of(content, media_type)
        return asset

    def save(self, timeline: Timeline) -> None:
        self.project = self.world.service.save_timeline(
            self.project.id,
            timeline,
            expected_version=self.project.version,
            actor=USER,
        )

    def swap_background(self, asset: Asset) -> None:
        """Store scene 1 with ``asset`` as background, without the save checks."""
        scenes = list(self.project.timeline.scenes)
        others = tuple(
            placed
            for placed in scenes[0].assets
            if placed.role is not AssetRole.BACKGROUND
        )
        scenes[0] = dataclasses.replace(
            scenes[0], assets=(SceneAsset(asset.id, AssetRole.BACKGROUND), *others)
        )
        self.force(dataclasses.replace(self.project.timeline, scenes=tuple(scenes)))

    def service(
        self, renderer=None, storage=None, source=None, mixer=None, clock=None
    ) -> ShortsRenderService:
        return ShortsRenderService(
            self.database,
            storage or self.storage,
            self.audit,
            renderer or self.renderer,
            mixer or self.mixer(),
            source or self.visuals,
            clock=clock or at(T1),
        )

    def render(self, service=None, **kwargs) -> RenderResult:
        return (service or self.service()).render(
            kwargs.pop("project_id", self.project.id),
            expected_version=kwargs.pop("expected_version", self.project.version),
            actor=USER,
            take_over=kwargs.pop("take_over", False),
        )

    def videos(self) -> list[Artifact]:
        with self.database.transaction() as connection:
            return [
                artifact
                for artifact in ArtifactRepository(connection).list_by_content_item(
                    self.world.item.id
                )
                if artifact.kind is ArtifactKind.VIDEO
            ]

    def mp4_files(self) -> list[Path]:
        return sorted(self.storage.root.rglob("*.mp4"))

    def jobs(self):
        with self.database.transaction() as connection:
            return [
                job
                for job in AIJobRepository(connection).list_by_content_item(
                    self.world.item.id
                )
                if job.kind == RENDER_KIND
            ]

    def render_events(self):
        """Every audit event except the mixer's ``audio.mixed``."""
        return [event for event in self.sink.events() if event.action != MIXED]

    def state(self) -> tuple:
        return (
            self.world.count("artifacts"),
            self.world.count("ai_jobs"),
            self.world.project_of(),
            self.mp4_files(),
        )

    def refuse(self, code: Code, service=None, **kwargs) -> ShortsRenderError:
        before = self.state()
        events = self.render_events()
        with pytest.raises(ShortsRenderError) as info:
            self.render(service, **kwargs)
        assert info.value.code == code.value
        assert self.state() == before
        assert self.render_events() == events
        return info.value


@pytest.fixture
def studio(database: Database, tmp_path: Path) -> Studio:
    return Studio(database, tmp_path)


def scaled(size: int, permille: int) -> int:
    return (size * permille + 500) // 1000


# The render


def test_a_ready_project_is_rendered_into_video_version_one(studio: Studio) -> None:
    assert studio.project.status is ShortsProjectStatus.READY
    before = studio.world.project_of()

    result = studio.render()

    artifact = result.artifact
    assert result.created is True
    assert artifact.kind is ArtifactKind.VIDEO
    assert artifact.version == 1
    assert artifact.media_type == MP4_MEDIA_TYPE == "video/mp4"
    assert artifact.content_item_id == studio.world.item.id
    data = studio.storage.read(artifact.uri)
    assert len(data) == artifact.size_bytes
    assert probe_mp4(data).duration_ms == DURATION_MS
    assert studio.mp4_files() == [
        studio.storage.root / studio.world.item.id / "video" / "v1.mp4"
    ]
    assert studio.videos() == [artifact]
    assert studio.world.project_of() == before  # D2: the project row is unchanged


def test_the_job_records_the_rendered_video(studio: Studio) -> None:
    result = studio.render()

    (job,) = studio.jobs()
    assert job.id == result.job_id
    assert job.status is AIJobStatus.SUCCEEDED
    assert job.attempts == 1
    assert job.idempotency_key.startswith("rnd:")
    saved = job.checkpoints[-1]
    assert saved.step == RENDERED_STEP == "video.rendered"
    assert saved.data["artifact_id"] == result.artifact.id
    assert saved.data["input_sha256"] == result.input_sha256
    assert saved.data["version"] == 1
    assert saved.data["created"] is True


def test_the_request_holds_the_canvas_the_layers_and_the_mix(studio: Studio) -> None:
    studio.render()

    (request,) = studio.renderer.calls
    config = studio.project.config
    assert (request.width, request.height, request.fps) == (
        config.width,
        config.height,
        config.fps,
    )
    assert request.duration_ms == studio.project.audio_duration_ms == DURATION_MS
    assert request.audio.sha256 == studio.mix().sha256
    assert request.texts == ()
    scenes = studio.project.timeline.scenes
    assert [item.key for item in request.inputs] == ["v1", "v2", "v3"]
    for layer, scene, background in zip(
        request.layers, scenes, studio.backgrounds, strict=True
    ):
        assert (layer.start_ms, layer.end_ms) == (scene.start_ms, scene.end_ms)
        assert layer.rect == PixelRect(0, 0, config.width, config.height)
        assert layer.fit is LayerFit.COVER
        assert request.input(layer.input_key).data == (
            studio.visuals.files[background.id].data
        )


def test_layers_are_background_then_b_roll_then_overlays(studio: Studio) -> None:
    clip = studio.picture(AssetKind.VIDEO_CLIP, "video/mp4")
    logo = studio.picture()
    timeline = studio.project.timeline
    first = timeline.scenes[0]
    overlay = SceneAsset(
        logo.id,
        AssetRole.OVERLAY,
        source_in_ms=10,
        fit=AssetFit.CONTAIN,
        box=Box(100, 200, 500, 300),
    )
    broll = SceneAsset(
        clip.id,
        AssetRole.B_ROLL,
        start_ms=100,
        end_ms=900,
        source_in_ms=1000,
        source_out_ms=1500,
    )
    scenes = (
        dataclasses.replace(first, assets=(overlay, broll, *first.assets)),
        *timeline.scenes[1:],
    )
    studio.save(dataclasses.replace(timeline, scenes=scenes))

    studio.render()

    (request,) = studio.renderer.calls
    config = studio.project.config
    background, b_roll, top = request.layers[:3]
    assert request.input(background.input_key).data == (
        studio.visuals.files[studio.backgrounds[0].id].data
    )
    assert (b_roll.start_ms, b_roll.end_ms) == (
        first.start_ms + 100,
        first.start_ms + 900,
    )
    assert (b_roll.source_in_ms, b_roll.source_out_ms) == (1000, 1500)
    assert request.input(b_roll.input_key).media_type == "video/mp4"
    assert top.fit is LayerFit.CONTAIN
    assert top.rect == PixelRect(
        scaled(config.width, 100),
        scaled(config.height, 200),
        scaled(config.width, 500),
        scaled(config.height, 300),
    )
    assert top.source_in_ms is None  # an image has no source window
    assert len(request.layers) == 5


def test_captions_become_one_text_line_per_wrapped_line(
    database: Database, tmp_path: Path
) -> None:
    studio = Studio(database, tmp_path, captions=True)
    assert studio.project.status is ShortsProjectStatus.READY

    studio.render()

    (request,) = studio.renderer.calls
    config = studio.project.config
    expected = []
    for caption in studio.project.timeline.captions:
        lines = wrap(caption.text, config.caption_rules)
        box = caption_box(lines, caption.position, caption.style, config.safe_rect)
        height = box.height // len(lines)
        for index, text in enumerate(lines):
            expected.append(
                (
                    text,
                    caption.start_ms,
                    caption.end_ms,
                    PixelRect(box.x, box.y + index * height, box.width, height),
                    caption.style.size_px,
                )
            )
    assert [
        (line.text, line.start_ms, line.end_ms, line.rect, line.size_px)
        for line in request.texts
    ] == expected
    assert expected
    assert all(line.font_key is None for line in request.texts)


def test_a_caption_font_asset_is_an_input_of_the_request(
    database: Database, tmp_path: Path
) -> None:
    studio = Studio(database, tmp_path, captions=True)
    font = studio.picture(AssetKind.FONT, "font/ttf", b"font bytes")
    timeline = studio.project.timeline
    captions = tuple(
        dataclasses.replace(
            caption,
            style=dataclasses.replace(caption.style, font_asset_id=font.id),
        )
        for caption in timeline.captions
    )
    studio.save(dataclasses.replace(timeline, captions=captions))

    result = studio.render()

    (request,) = studio.renderer.calls
    assert request.input("f1").data == b"font bytes"
    assert request.input("f1").media_type == "font/ttf"
    assert {line.font_key for line in request.texts} == {"f1"}
    event = studio.render_events()[-1]
    assert font.id in event.metadata["asset_ids"].split(",")
    assert result.created is True


def test_the_pure_builder_can_add_a_fallback_font(studio: Studio) -> None:
    mix = studio.mix()
    files = {asset.id: studio.visuals.files[asset.id] for asset in studio.backgrounds}
    fallback = VisualFile.of(b"fallback font", "font/otf")

    request = build_render_request(
        studio.project, mix, files, {}, font_fallback=fallback
    )

    assert request.input("font").media_type == "font/otf"
    assert request.texts == ()  # captions are off


def test_rendering_audits_ids_checksums_and_counts(studio: Studio) -> None:
    result = studio.render()

    (event,) = [e for e in studio.render_events() if e.action == RENDERED]
    assert event.action == "shorts.rendered"
    assert (event.entity.type, event.entity.id) == ("artifact", result.artifact.id)
    assert event.result is AuditResult.SUCCESS and event.actor == USER
    config = studio.project.config
    assert dict(event.metadata) == {
        "project_id": studio.project.id,
        "content_item_id": studio.world.item.id,
        "project_version": studio.project.version,
        "version": 1,
        "sha256": result.artifact.sha256,
        "size_bytes": result.artifact.size_bytes,
        "media_type": "video/mp4",
        "duration_ms": DURATION_MS,
        "width": config.width,
        "height": config.height,
        "fps": config.fps,
        "renderer": "mock",
        "input_sha256": result.input_sha256,
        "timeline_sha256": studio.project.timeline.sha256,
        "mix_sha256": studio.mix().sha256,
        "voice_sha256": studio.voice_artifact.sha256,
        "layer_count": 3,
        "text_line_count": 0,
        "caption_count": 0,
        "asset_ids": ",".join(sorted(asset.id for asset in studio.backgrounds)),
        "job_id": result.job_id,
        "attempts": 1,
    }


def test_the_audit_never_holds_caption_text_or_bytes(
    database: Database, tmp_path: Path
) -> None:
    studio = Studio(database, tmp_path, captions=True)

    studio.render()

    events = studio.render_events()
    assert [event.action for event in events] == [RENDERED]
    for caption in studio.project.timeline.captions:
        assert caption.text not in repr(events)
    for value in events[0].metadata.values():
        assert isinstance(value, str | int | float | bool | type(None))


# Replays and new versions


def test_the_same_inputs_again_are_a_replay(studio: Studio) -> None:
    first = studio.render()
    events = studio.render_events()

    again = studio.render()

    assert again.created is False
    assert again.artifact == first.artifact
    assert again.input_sha256 == first.input_sha256
    assert again.job_id == first.job_id
    assert len(studio.renderer.calls) == 1  # no renderer call
    assert studio.videos() == [first.artifact]
    assert len(studio.mp4_files()) == 1
    assert studio.render_events() == events
    mixed = [event for event in studio.sink.events() if event.action == MIXED]
    assert len(mixed) == 2  # D6: the mix runs on every call


def test_a_changed_picture_gives_version_two_and_invalidates_approvals(
    studio: Studio,
) -> None:
    first = studio.render()
    approval = make_approval_request(
        studio.world.item, [first.artifact], status=ApprovalStatus.APPROVED
    )
    with studio.database.transaction() as connection:
        ApprovalRequestRepository(connection).add(approval)
    background = studio.backgrounds[0]
    studio.visuals.files[background.id] = VisualFile.of(b"new picture", "image/png")

    second = studio.render()

    assert second.created is True
    assert second.artifact.version == 2
    assert second.artifact.sha256 != first.artifact.sha256
    assert second.input_sha256 != first.input_sha256
    assert second.job_id != first.job_id
    assert len(studio.mp4_files()) == 2
    with studio.database.transaction() as connection:
        (stored,) = ApprovalRequestRepository(connection).list_by_content_item(
            studio.world.item.id
        )
    assert stored.status is ApprovalStatus.INVALIDATED
    actions = [event.action for event in studio.render_events()]
    assert actions == [RENDERED, "approval.invalidated", RENDERED]


def test_identical_bytes_from_another_renderer_are_not_a_new_version(
    studio: Studio,
) -> None:
    first = studio.render()
    events = studio.render_events()
    other = MockVideoRenderer(name="other", clock=at(T1))

    again = studio.render(studio.service(renderer=other))

    assert len(other.calls) == 1  # another renderer name is another input
    assert again.created is False
    assert again.artifact == first.artifact
    assert again.input_sha256 != first.input_sha256
    assert studio.videos() == [first.artifact]
    assert len(studio.mp4_files()) == 1
    assert studio.render_events() == events
    jobs = {job.id: job for job in studio.jobs()}
    assert [job.status for job in jobs.values()] == [AIJobStatus.SUCCEEDED] * 2
    assert jobs[again.job_id].checkpoints[-1].data["created"] is False


# Refusals about the project


def test_an_unknown_project_is_refused(studio: Studio) -> None:
    studio.refuse(Code.PROJECT_NOT_FOUND, project_id="nope")

    assert HTTP_STATUS[Code.PROJECT_NOT_FOUND] == 404


def test_a_project_read_at_another_version_is_a_conflict(studio: Studio) -> None:
    studio.refuse(Code.CONFLICT, expected_version=studio.project.version + 1)


def test_a_draft_project_is_not_ready(database: Database, tmp_path: Path) -> None:
    draft = Studio(database, tmp_path, plan=False)
    assert draft.project.status is ShortsProjectStatus.DRAFT

    draft.refuse(Code.NOT_READY)


def test_a_planned_project_without_backgrounds_is_not_ready(
    database: Database, tmp_path: Path
) -> None:
    planned = Studio(database, tmp_path, ready=False)
    assert planned.project.status is ShortsProjectStatus.PLANNED

    planned.refuse(Code.NOT_READY)
    assert planned.renderer.calls == []


def test_a_project_with_a_newer_voice_is_stale(studio: Studio) -> None:
    script = studio.world.add_script()
    studio.world.add_audio(script)

    studio.refuse(Code.STALE_INPUT)


# Refusals about the assets


def test_an_asset_not_attached_to_the_item_is_unavailable(studio: Studio) -> None:
    studio.swap_background(studio.picture(attach=False))

    studio.refuse(Code.ASSET_UNAVAILABLE)


def test_an_asset_of_another_channel_is_unavailable(studio: Studio) -> None:
    other = make_channel()
    with studio.database.transaction() as connection:
        ChannelRepository(connection).add(other)
    studio.swap_background(studio.picture(channel_id=other.id, attach=False))

    studio.refuse(Code.ASSET_UNAVAILABLE)


def test_an_asset_with_unknown_rights_is_refused(studio: Studio) -> None:
    unknown = studio.picture(category=AssetCategory.UNKNOWN)
    studio.swap_background(unknown)

    error = studio.refuse(Code.ASSET_RIGHTS)

    assert unknown.id in error.detail
    assert studio.visuals.reads == []  # no file is read for a refused asset


def test_an_asset_of_the_wrong_kind_is_unsupported(studio: Studio) -> None:
    studio.swap_background(studio.picture(AssetKind.MUSIC))

    studio.refuse(Code.ASSET_UNSUPPORTED)


def test_an_asset_whose_provenance_changed_is_refused(studio: Studio) -> None:
    studio.add_provenance(studio.backgrounds[1].id)

    error = studio.refuse(Code.ASSET_CHANGED)

    assert HTTP_STATUS[Code.ASSET_CHANGED] == 409
    assert studio.backgrounds[1].id in error.detail


def test_a_missing_picture_file_is_unavailable(studio: Studio) -> None:
    del studio.visuals.files[studio.backgrounds[2].id]

    studio.refuse(Code.ASSET_UNAVAILABLE)
    assert studio.renderer.calls == []


@pytest.mark.parametrize("media_type", ["image/gif", "font/ttf", "audio/wav"])
def test_a_picture_in_another_format_is_unsupported(
    studio: Studio, media_type: str
) -> None:
    background = studio.backgrounds[0]
    studio.visuals.files[background.id] = VisualFile.of(b"some file", media_type)

    studio.refuse(Code.ASSET_UNSUPPORTED)


def test_a_font_that_is_not_a_font_file_is_unsupported(
    database: Database, tmp_path: Path
) -> None:
    studio = Studio(database, tmp_path, captions=True)
    font = studio.picture(AssetKind.FONT, "image/png")
    timeline = studio.project.timeline
    captions = tuple(
        dataclasses.replace(
            caption, style=dataclasses.replace(caption.style, font_asset_id=font.id)
        )
        for caption in timeline.captions
    )
    studio.save(dataclasses.replace(timeline, captions=captions))

    studio.refuse(Code.ASSET_UNSUPPORTED)


def test_a_mix_failure_passes_through(studio: Studio) -> None:
    studio.storage.delete(studio.voice_artifact.uri)

    with pytest.raises(ShortsMixError) as info:
        studio.render()

    assert info.value.code == ShortsMixErrorCode.ASSET_UNAVAILABLE.value
    assert studio.jobs() == []
    assert studio.videos() == []


# Renderer failures


def test_a_renderer_failure_fails_the_job_and_is_audited(studio: Studio) -> None:
    studio.renderer.fail_next(RenderErrorCode.FAILED)

    with pytest.raises(VideoRenderError) as info:
        studio.render()

    assert info.value.render_code is RenderErrorCode.FAILED
    (job,) = studio.jobs()
    assert job.status is AIJobStatus.FAILED
    assert job.last_error == "render.failed"
    assert job.attempts == 1
    (event,) = studio.render_events()
    assert event.action == RENDER_FAILED == "shorts.render_failed"
    assert event.result is AuditResult.FAILURE
    assert (event.entity.type, event.entity.id) == ("shorts_project", studio.project.id)
    assert dict(event.metadata) == {
        "project_id": studio.project.id,
        "content_item_id": studio.world.item.id,
        "job_id": job.id,
        "attempts": 1,
        "input_sha256": event.metadata["input_sha256"],
        "renderer": "mock",
        "code": "render.failed",
    }
    assert studio.videos() == [] and studio.mp4_files() == []


def test_a_failed_render_is_retried_as_the_next_attempt(studio: Studio) -> None:
    studio.renderer.fail_next(RenderErrorCode.TIMEOUT)
    with pytest.raises(VideoRenderError):
        studio.render()

    result = studio.render()

    (job,) = studio.jobs()
    assert result.created is True and result.job_id == job.id
    assert job.status is AIJobStatus.SUCCEEDED
    assert job.attempts == 2
    assert job.last_error is None


class Crashing(MockVideoRenderer):
    """A renderer whose first call dies without a provider error."""

    def __init__(self) -> None:
        super().__init__(clock=at(T1))
        self.crash = True

    def render(self, request):
        if self.crash:
            self.crash = False
            raise RuntimeError("the worker died")
        return super().render(request)


def test_a_running_job_is_in_progress_unless_taken_over(studio: Studio) -> None:
    crashing = Crashing()
    service = studio.service(renderer=crashing)
    with pytest.raises(RuntimeError):
        studio.render(service)
    (job,) = studio.jobs()
    assert job.status is AIJobStatus.RUNNING

    error = studio.refuse(Code.IN_PROGRESS, service)
    assert HTTP_STATUS[Code.IN_PROGRESS] == 409
    assert error.code == "domain.shorts_render_in_progress"

    result = studio.render(service, take_over=True)

    (job,) = studio.jobs()
    assert result.created is True
    assert job.status is AIJobStatus.SUCCEEDED
    assert job.attempts == 2


def test_an_answer_that_is_not_an_mp4_is_invalid(studio: Studio) -> None:
    studio.renderer.queue(b"not an mp4 at all")

    with pytest.raises(ShortsRenderError) as info:
        studio.render()

    assert info.value.code == Code.RENDER_INVALID.value
    (job,) = studio.jobs()
    assert job.status is AIJobStatus.FAILED
    assert job.last_error == Code.RENDER_INVALID.value
    (event,) = studio.render_events()
    assert event.action == RENDER_FAILED
    assert event.metadata["code"] == Code.RENDER_INVALID.value
    assert studio.videos() == [] and studio.mp4_files() == []


def test_a_video_of_the_wrong_length_is_invalid(studio: Studio) -> None:
    studio.renderer.queue(stub_mp4(DURATION_MS + 1000, b"too long"))

    with pytest.raises(ShortsRenderError) as info:
        studio.render()

    assert info.value.code == Code.RENDER_INVALID.value
    assert studio.videos() == []


def test_a_small_length_difference_is_accepted(studio: Studio) -> None:
    studio.renderer.queue(stub_mp4(DURATION_MS + 200, b"close enough"))

    assert studio.render().created is True


# Failures while storing


def test_a_failing_storage_fails_the_job_and_stores_nothing(studio: Studio) -> None:
    class Broken(LocalFileStorage):
        def write(self, *args, **kwargs):
            raise OSError("disk full")

    with pytest.raises(ShortsRenderError) as info:
        studio.render(studio.service(storage=Broken(studio.storage.root)))

    assert info.value.code == Code.STORAGE_FAILED.value
    (job,) = studio.jobs()
    assert job.last_error == Code.STORAGE_FAILED.value
    assert studio.videos() == [] and studio.mp4_files() == []


def test_a_failing_artifact_insert_deletes_the_file(
    studio: Studio, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self, artifact):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(ArtifactRepository, "add", broken)

    with pytest.raises(RuntimeError, match="insert failed"):
        studio.render()

    assert studio.mp4_files() == []
    monkeypatch.undo()
    assert studio.videos() == []


class Meddling(MockVideoRenderer):
    """A renderer that changes the world while it renders."""

    def __init__(self, change) -> None:
        super().__init__(clock=at(T1))
        self.change = change

    def render(self, request):
        self.change()
        return super().render(request)


def test_a_project_changed_during_the_render_is_a_conflict(studio: Studio) -> None:
    meddling = Meddling(lambda: studio.save(studio.project.timeline))
    service = studio.service(renderer=meddling)

    with pytest.raises(ShortsRenderError) as info:
        service.render(
            studio.project.id, expected_version=studio.project.version, actor=USER
        )

    assert info.value.code == Code.CONFLICT.value
    (job,) = studio.jobs()
    assert job.status is AIJobStatus.FAILED
    assert job.last_error == Code.CONFLICT.value
    assert studio.videos() == [] and studio.mp4_files() == []


def test_newer_audio_during_the_render_is_stale(studio: Studio) -> None:
    world = studio.world
    meddling = Meddling(lambda: world.add_audio(world.latest_script()))

    with pytest.raises(ShortsRenderError) as info:
        studio.render(studio.service(renderer=meddling))

    assert info.value.code == Code.STALE_INPUT.value
    assert studio.videos() == [] and studio.mp4_files() == []


def test_a_worker_that_was_taken_over_cannot_store_its_video(studio: Studio) -> None:
    # A clock that moves on with every call, so each job save has a new
    # updated_at (a fixed clock would repeat it and weaken the guard).
    ticks = itertools.count(1)

    def clock():
        return T1 + timedelta(milliseconds=next(ticks))

    inner = MockVideoRenderer(clock=at(T1))
    inner.fail_next(RenderErrorCode.FAILED)
    taker = studio.service(renderer=inner, clock=clock)

    def take_over() -> None:
        with pytest.raises(VideoRenderError):
            studio.render(taker, take_over=True)

    worker = studio.service(renderer=Meddling(take_over), clock=clock)

    with pytest.raises(ConcurrencyError):
        studio.render(worker)

    (job,) = studio.jobs()
    assert job.status is AIJobStatus.FAILED  # as the taker left it
    assert job.attempts == 2
    assert studio.videos() == [] and studio.mp4_files() == []


# The default visual source


def generated_picture(studio: Studio, data: bytes) -> Asset:
    """A generated image asset whose file is an artifact in the storage."""
    item = studio.world.item
    with studio.database.transaction() as connection:
        latest = ArtifactRepository(connection).latest(item.id, ArtifactKind.THUMBNAIL)
    version = latest.version + 1 if latest else 1
    # The storage keeps only wav, srt and mp4 files; the bytes are what count.
    stored = studio.storage.write(
        item.id, ArtifactKind.THUMBNAIL, version, data, media_type="video/mp4"
    )
    artifact = Artifact.create(
        item.id,
        ArtifactKind.THUMBNAIL,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type="image/png",
        clock=at(T1),
    )
    if latest is not None:
        artifact = dataclasses.replace(artifact, version=version)
    asset = Asset.create(
        studio.world.channel.id,
        AssetKind.IMAGE,
        AssetCategory.GENERATED,
        title=f"Generated picture {artifact.id}",
        source="image-provider",
        artifact_id=artifact.id,
        clock=at(T1),
    )
    with studio.database.transaction() as connection:
        ArtifactRepository(connection).add(artifact)
        AssetRepository(connection).add(
            asset, source_key=artifact.id, title_key=artifact.id
        )
        AssetUsageRepository(connection).add(
            AssetUsage.create(asset.id, item.id, attached_by=USER, clock=at(T1))
        )
    return asset


def test_the_default_source_reads_generated_pictures(
    database: Database, tmp_path: Path
) -> None:
    studio = Studio(database, tmp_path, ready=False)
    pictures = [
        generated_picture(studio, f"generated {index}".encode())
        for index in range(len(studio.project.timeline.scenes))
    ]
    studio.save(
        studio.build(
            sfx=[
                (index, SceneAsset(picture.id, AssetRole.BACKGROUND))
                for index, picture in enumerate(pictures)
            ]
        )
    )
    service = ShortsRenderService(
        studio.database,
        studio.storage,
        studio.audit,
        studio.renderer,
        studio.mixer(),
        clock=at(T1),
    )

    result = studio.render(service)

    assert result.created is True
    (request,) = studio.renderer.calls
    assert [item.data for item in request.inputs] == [
        f"generated {index}".encode() for index in range(len(pictures))
    ]
    assert isinstance(ArtifactVisualSource(database, studio.storage), VisualSource)
    assert isinstance(FakeVisualSource(), VisualSource)


def test_the_default_source_refuses_an_asset_without_a_stored_file(
    studio: Studio,
) -> None:
    service = ShortsRenderService(
        studio.database,
        studio.storage,
        studio.audit,
        studio.renderer,
        studio.mixer(),
        clock=at(T1),
    )

    studio.refuse(Code.ASSET_UNAVAILABLE, service)


def test_the_default_source_checks_the_checksum(studio: Studio) -> None:
    asset = generated_picture(studio, b"generated picture")
    source = ArtifactVisualSource(studio.database, studio.storage)
    read = source.read_file(asset)
    assert read == VisualFile.of(b"generated picture", "image/png")
    studio.storage.write(
        studio.world.item.id,
        ArtifactKind.THUMBNAIL,
        1,
        b"another picture",
        media_type="video/mp4",
    )

    with pytest.raises(ValueError):
        source.read_file(asset)


def test_the_default_source_refuses_an_unknown_artifact(studio: Studio) -> None:
    asset = generated_picture(studio, b"generated picture")
    ghost = dataclasses.replace(asset, artifact_id="no-such-artifact")

    with pytest.raises(ValueError):
        ArtifactVisualSource(studio.database, studio.storage).read_file(ghost)


@pytest.mark.parametrize(
    "data, media_type, sha256",
    [(b"", "image/png", None), (b"x", "", None), (b"x", "image/png", "0" * 64)],
)
def test_a_visual_file_checks_its_values(data, media_type, sha256) -> None:
    with pytest.raises(ValueError):
        if sha256 is None:
            VisualFile.of(data, media_type)
        else:
            VisualFile(data, media_type, sha256)


# Error model


def test_every_code_has_a_message_and_the_conflicts_are_409() -> None:
    assert set(USER_MESSAGES) == set(Code)
    for code in Code:
        assert code.value.startswith("domain.shorts_render")
        error = ShortsRenderError("detail", code=code)
        assert error.code == code.value
        assert error.user_message == USER_MESSAGES[code]
    assert {code for code, status in HTTP_STATUS.items() if status == 409} == {
        Code.CONFLICT,
        Code.STALE_INPUT,
        Code.IN_PROGRESS,
        Code.ASSET_CHANGED,
    }
    assert HTTP_STATUS[Code.PROJECT_NOT_FOUND] == 404
    assert ShortsRenderError().code == "domain.shorts_render"
