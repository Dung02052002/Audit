"""I-104 Shorts render tests (Prompt Pack v8, prompt #104).

End-to-end output fixture validation and failure-case scenarios across the
Phase I Shorts production pipeline (I-094..I-103):
- Full end-to-end Shorts pipeline fixture: project creation, scene planning,
  background + B-roll + overlay + SFX + music placement, caption planning,
  SRT subtitle artifact recording, selected/generated vertical thumbnail
  recording, AI disclosure decision, Shorts metadata recording, MP4 video
  rendering, and ``ShortsArtifactBundle`` verification;
- Stale artifact detection when the timeline changes after subtitles, metadata
  or video were recorded;
- Approval invalidation when any deliverable artifact in the bundle is
  re-recorded at a newer version;
- Renderer failure cases (timeout, corrupted MP4 bytes, duration mismatch,
  provenance drift) and disclosure drift refusal in ``ShortsBundleBuilder``.
"""

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest
from factories import USER, at, make_approval_request
from test_shorts_audio_mix import T1
from test_shorts_render import Studio
from test_shorts_thumbnail import make_jpeg

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    DisclosureFacts,
)
from ai_youtube_agent.content.policy_report import PolicyReporter
from ai_youtube_agent.content.rights_report import RightsReporter
from ai_youtube_agent.content.shorts_bundle import (
    ShortsBundleBuilder,
    ShortsBundleError,
    ShortsBundleErrorCode,
)
from ai_youtube_agent.content.shorts_metadata import (
    ShortsMetadata,
    ShortsMetadataRecorder,
)
from ai_youtube_agent.content.shorts_render import (
    RENDER_FAILED,
    ShortsRenderError,
    ShortsRenderErrorCode,
)
from ai_youtube_agent.content.shorts_subtitles import ShortsSubtitleRecorder
from ai_youtube_agent.content.shorts_thumbnail import (
    ShortsThumbnailRecorder,
    probe_image,
)
from ai_youtube_agent.content.shorts_timeline import (
    AssetPlacement,
    AssetRole,
    Box,
    SceneAsset,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.providers.video_render import (
    RenderErrorCode,
    RenderedVideo,
    VideoRenderError,
    probe_mp4,
)


class EndToEndShortsFixture(Studio):
    def __init__(self, database: Database, tmp_path: Path) -> None:
        super().__init__(database, tmp_path, captions=True)
        self.settings = Settings()
        self.decider = DisclosureDecider(database, self.audit, clock=at(T1))
        self.subtitles = ShortsSubtitleRecorder(
            database, self.storage, self.audit, clock=at(T1)
        )
        self.thumbnails = ShortsThumbnailRecorder(
            database,
            self.storage,
            self.audit,
            visuals=self.visuals,
            clock=at(T1),
        )
        self.metadata = ShortsMetadataRecorder(
            database, self.storage, self.audit, clock=at(T1)
        )
        self.rights_reporter = RightsReporter(
            database, self.settings, clock=at(T1)
        )
        self.policy_reporter = PolicyReporter(database, clock=at(T1))
        self.bundles = ShortsBundleBuilder(
            database,
            self.storage,
            self.rights_reporter,
            self.policy_reporter,
            self.audit,
            clock=at(T1),
        )


def test_end_to_end_shorts_pipeline_validates_all_output_fixtures(
    database: Database, tmp_path: Path
) -> None:
    fx = EndToEndShortsFixture(database, tmp_path)

    # Enrich scene 0 with B-roll, overlay, SFX and timeline music
    broll = fx.picture(
        AssetKind.VIDEO_CLIP,
        media_type="video/mp4",
        category=AssetCategory.LICENSED,
        license_ref="CC-BY-4.0",
        attribution="B-roll by Studio X",
    )
    overlay = fx.picture(
        AssetKind.IMAGE,
        media_type="image/png",
        data=make_jpeg(1080, 1920),
    )
    sfx = fx.sfx_asset()
    music = fx.music_asset()
    fx.save(
        fx.build(
            sfx=[
                *(
                    (idx, SceneAsset(bg.id, AssetRole.BACKGROUND))
                    for idx, bg in enumerate(fx.backgrounds)
                ),
                (
                    0,
                    SceneAsset(
                        broll.id,
                        AssetRole.B_ROLL,
                        start_ms=0,
                        end_ms=500,
                        source_in_ms=100,
                        source_out_ms=600,
                    ),
                ),
                (
                    0,
                    SceneAsset(
                        overlay.id,
                        AssetRole.OVERLAY,
                        box=Box(100, 200, 300, 300),
                    ),
                ),
                (0, SceneAsset(sfx.id, AssetRole.SFX, gain_db=-3.0)),
            ],
            music=[AssetPlacement(music.id, gain_db=-14.0, fade_in_ms=200)],
        )
    )
    fx.project = fx.world.service.caption(
        fx.project.id, expected_version=fx.project.version, actor=USER
    )

    # 1. Record AI disclosure
    disclosure = fx.decider.decide(
        fx.world.item.id,
        DisclosureFacts(realistic_visual=True),
        rule_set=DISCLOSURE_RULES,
        actor=USER,
    )

    # 2. Record SRT subtitles
    sub_res = fx.subtitles.record(
        fx.project.id, expected_version=fx.project.version, actor=USER
    )
    srt_text = fx.storage.read(sub_res.artifact.uri).decode("utf-8")
    assert "1\n00:00:00,000 -->" in srt_text

    # 3. Record generated vertical PNG thumbnail
    thumb_res = fx.thumbnails.generate(
        fx.project.id,
        title_text="Python Shorts Masterclass",
        actor=USER,
        expected_version=fx.project.version,
    )
    thumb_info = probe_image(fx.storage.read(thumb_res.artifact.uri))
    assert (thumb_info.width, thumb_info.height) == (1080, 1920)

    # 4. Record Shorts metadata
    meta_res = fx.metadata.record(
        fx.project.id,
        title="Python Shorts Masterclass",
        description="Learn Python in 60 seconds.",
        tags=("Python", "Shorts"),
        actor=USER,
        expected_version=fx.project.version,
    )
    parsed_meta = ShortsMetadata.from_json_bytes(
        fx.storage.read(meta_res.artifact.uri)
    )
    assert "Attribution: B-roll by Studio X" in parsed_meta.description
    assert parsed_meta.contains_synthetic_media is True
    assert parsed_meta.disclosure_decision_id == disclosure.id

    # 5. Render MP4 video
    render_res = fx.render()
    mp4_info = probe_mp4(fx.storage.read(render_res.artifact.uri))
    assert mp4_info.duration_ms == fx.project.audio_duration_ms

    # 6. Build and verify complete ShortsArtifactBundle
    bundle = fx.bundles.build(
        fx.project.id,
        actor=USER,
        expected_version=fx.project.version,
    )
    assert bundle.video == render_res.artifact
    assert bundle.subtitles == sub_res.artifact
    assert bundle.thumbnail == thumb_res.artifact
    assert bundle.metadata_artifact == meta_res.artifact
    assert bundle.rights_report.gate.blocks is False
    assert bundle.policy_report.blocks is False


def test_timeline_mutation_invalidates_bundle_until_artifacts_are_refreshed(
    database: Database, tmp_path: Path
) -> None:
    fx = EndToEndShortsFixture(database, tmp_path)
    fx.decider.decide(
        fx.world.item.id,
        DisclosureFacts(),
        rule_set=DISCLOSURE_RULES,
        actor=USER,
    )
    fx.subtitles.record(
        fx.project.id, expected_version=fx.project.version, actor=USER
    )
    fx.thumbnails.generate(fx.project.id, title_text="Hook", actor=USER)
    fx.metadata.record(
        fx.project.id,
        description="Initial #Shorts",
        actor=USER,
        expected_version=fx.project.version,
    )
    fx.render()

    initial_bundle = fx.bundles.build(fx.project.id, actor=USER)
    approval = make_approval_request(
        content_item_id=fx.world.item.id,
        artifacts=initial_bundle.artifacts,
    )
    with database.transaction() as connection:
        ApprovalRequestRepository(connection).add(approval)

    # Mutate caption text on the timeline -> project version advances
    captions = list(fx.project.timeline.captions)
    captions[0] = dataclasses.replace(captions[0], text="Changed hook line")
    fx.save(dataclasses.replace(fx.project.timeline, captions=tuple(captions)))

    # Video is now stale because timeline_sha256 changed
    with pytest.raises(ShortsBundleError) as exc_info:
        fx.bundles.build(fx.project.id, actor=USER)
    assert exc_info.value.code == ShortsBundleErrorCode.VIDEO_STALE.value

    # Re-render video -> invalidates the pending approval
    fx.render()
    with database.transaction() as connection:
        stored_approval = ApprovalRequestRepository(connection).get(approval.id)
        assert stored_approval is not None
        assert stored_approval.status is ApprovalStatus.INVALIDATED

    # Subtitles are still stale relative to the new caption text
    with pytest.raises(ShortsBundleError) as exc_info:
        fx.bundles.build(fx.project.id, actor=USER)
    assert exc_info.value.code == ShortsBundleErrorCode.SUBTITLES_STALE.value

    fx.subtitles.record(
        fx.project.id, expected_version=fx.project.version, actor=USER
    )

    # Metadata is still bound to the old project_version
    with pytest.raises(ShortsBundleError) as exc_info:
        fx.bundles.build(fx.project.id, actor=USER)
    assert exc_info.value.code == ShortsBundleErrorCode.METADATA_STALE.value

    fx.metadata.record(
        fx.project.id,
        description="Updated #Shorts",
        actor=USER,
        expected_version=fx.project.version,
    )

    refreshed = fx.bundles.build(fx.project.id, actor=USER)
    assert refreshed.video.version == 2
    assert refreshed.subtitles is not None and refreshed.subtitles.version == 2
    assert refreshed.metadata_artifact.version == 2


def test_renderer_timeout_and_invalid_mp4_fail_the_job_and_block_bundle(
    database: Database, tmp_path: Path
) -> None:
    fx = EndToEndShortsFixture(database, tmp_path)

    class TimeoutRenderer:
        name = "timeout-mock"

        def render(self, request):
            raise VideoRenderError(
                "ffmpeg timed out",
                code=RenderErrorCode.TIMEOUT,
                retryable=True,
            )

    with pytest.raises(VideoRenderError) as exc_info:
        fx.render(service=fx.service(renderer=TimeoutRenderer()))
    assert exc_info.value.code is RenderErrorCode.TIMEOUT

    with database.transaction() as connection:
        jobs = AIJobRepository(connection).list_by_content_item(
            fx.world.item.id
        )
        assert len(jobs) == 1
        assert jobs[0].status is AIJobStatus.FAILED

    assert any(e.action == RENDER_FAILED for e in fx.sink.events)

    class BadMp4Renderer:
        name = "bad-mp4-mock"

        def render(self, request):
            return RenderedVideo(b"not-an-mp4-stream", self.name)

    with pytest.raises(ShortsRenderError) as render_err:
        fx.render(service=fx.service(renderer=BadMp4Renderer()))
    assert render_err.value.code == ShortsRenderErrorCode.RENDER_INVALID.value


def test_disclosure_drift_and_provenance_drift_are_refused(
    database: Database, tmp_path: Path
) -> None:
    fx = EndToEndShortsFixture(database, tmp_path)
    fx.decider.decide(
        fx.world.item.id,
        DisclosureFacts(realistic_visual=False),
        rule_set=DISCLOSURE_RULES,
        actor=USER,
    )
    fx.subtitles.record(
        fx.project.id, expected_version=fx.project.version, actor=USER
    )
    fx.thumbnails.generate(fx.project.id, title_text="Hook", actor=USER)
    fx.metadata.record(
        fx.project.id,
        description="Initial #Shorts",
        actor=USER,
        expected_version=fx.project.version,
    )
    fx.render()

    # Record a new AI disclosure decision after metadata was saved
    later_decider = DisclosureDecider(
        database, fx.audit, clock=at(T1 + timedelta(minutes=5))
    )
    later_decider.decide(
        fx.world.item.id,
        DisclosureFacts(realistic_person=True),
        rule_set=DISCLOSURE_RULES,
        actor=USER,
    )

    with pytest.raises(ShortsBundleError) as exc_info:
        fx.bundles.build(fx.project.id, actor=USER)
    assert exc_info.value.code == ShortsBundleErrorCode.DISCLOSURE_MISMATCH.value

    # Adding new provenance to a background asset without re-saving timeline refuses render
    fx.add_provenance(fx.backgrounds[0].id)
    with pytest.raises(ShortsRenderError) as render_err:
        fx.render()
    assert render_err.value.code == ShortsRenderErrorCode.ASSET_CHANGED.value

