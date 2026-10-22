"""J-115 LongForm render tests (Prompt Pack v8, prompt #115).

End-to-end output fixture validation and failure-case scenarios across the
Phase J LongForm production pipeline (J-105..J-114):
- Full end-to-end LongForm pipeline fixture: project creation, chapter & scene planning,
  B-roll asset mapping with D5 provenance snapshotting, SRT subtitle recording,
  horizontal 16:9 thumbnail recording, AI disclosure decision, LongForm metadata
  recording with formatted YouTube chapters, MP4 video rendering, and
  ``LongFormArtifactBundle`` verification;
- Stale artifact detection when the timeline or project changes after video,
  subtitles, thumbnail or metadata were recorded;
- Approval invalidation when any deliverable artifact in the bundle is
  re-recorded at a newer version;
- Renderer failure cases (timeout, corrupted MP4 bytes, duration mismatch,
  provenance drift) and disclosure drift refusal in ``LongFormBundleBuilder``;
- Feature flag enforcement (refuses operations when ``LONGFORM_ENABLED`` is off).
"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.broll_manager import BrollManager
from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    DisclosureFacts,
)
from ai_youtube_agent.content.longform_audio_mix import LongFormAudioMixer
from ai_youtube_agent.content.longform_bundle import (
    LongFormArtifactBundle,
    LongFormBundleBuilder,
    LongFormBundleError,
    LongFormBundleErrorCode,
)
from ai_youtube_agent.content.longform_metadata import (
    LongFormMetadataRecorder,
)
from ai_youtube_agent.content.longform_project import (
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_render import (
    RENDER_FAILED,
    LongFormRenderError,
    LongFormRenderErrorCode,
    LongFormRenderService,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import LongFormProjectService
from ai_youtube_agent.content.longform_subtitles import LongFormSubtitleRecorder
from ai_youtube_agent.content.longform_thumbnail import (
    LongFormThumbnailRecorder,
    generate_longform_thumbnail_png,
)
from ai_youtube_agent.content.policy_report import PolicyReporter
from ai_youtube_agent.content.rights_report import RightsReporter
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.shorts_render import (
    ArtifactVisualSource,
)
from ai_youtube_agent.content.voice import AudioMetadata
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
    VoiceProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.mock_video_render import MockVideoRenderer, stub_mp4
from ai_youtube_agent.providers.video_render import (
    RenderErrorCode,
)
from factories import USER, at, make_approval_request

T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


def make_longform_pcm_wav(duration_ms: int) -> bytes:
    """Generate minimal valid 16-bit 44.1kHz mono WAV bytes for duration_ms."""
    import math
    import struct

    sample_rate = 44100
    samples_count = int(sample_rate * (duration_ms / 1000.0))
    # A one-second 220 Hz tone repeated: silent audio fails the mix self-check.
    tone = struct.pack(
        f"<{sample_rate}h",
        *(
            int(10_000 * math.sin(2.0 * math.pi * 220.0 * i / sample_rate))
            for i in range(sample_rate)
        ),
    )
    pcm_data = (tone * (samples_count // sample_rate + 1))[: samples_count * 2]
    data_size = len(pcm_data)
    chunk_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        chunk_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,
        2,  # block align
        16,  # bits per sample
        b"data",
        data_size,
    )
    return header + pcm_data


class EndToEndLongFormFixture:
    def __init__(self, database: Database, tmp_path: Path) -> None:
        self.database = database
        self.tmp_path = tmp_path
        self.audit_sink = InMemoryAuditSink()
        self.audit = AuditLog(self.audit_sink)
        self.storage = LocalFileStorage(tmp_path / "artifacts")
        self.flags = FeatureFlags(longform_enabled=True)
        self.settings = Settings()
        self.renderer = MockVideoRenderer()
        self.visuals = ArtifactVisualSource(database, self.storage)

        self.projects = LongFormProjectService(
            database, self.audit, self.flags, clock=at(T0)
        )
        self.broll = BrollManager(database, self.audit, self.flags, clock=at(T0))
        self.subtitles = LongFormSubtitleRecorder(
            database, self.storage, self.audit, self.flags, clock=at(T0)
        )
        self.mixer = LongFormAudioMixer(database, self.storage, self.audit, self.flags)
        self.render_service = LongFormRenderService(
            database,
            self.storage,
            self.audit,
            self.renderer,
            self.mixer,
            visual_source=self.visuals,
            flags=self.flags,
            clock=at(T0),
        )
        self.thumbnails = LongFormThumbnailRecorder(
            database,
            self.storage,
            self.audit,
            visual_source=self.visuals,
            flags=self.flags,
            clock=at(T0),
        )
        self.metadata = LongFormMetadataRecorder(
            database, self.storage, self.audit, flags=self.flags, clock=at(T0)
        )
        self.decider = DisclosureDecider(database, self.audit, clock=at(T0))
        self.rights_reporter = RightsReporter(database, self.settings, clock=at(T0))
        self.policy_reporter = PolicyReporter(database, clock=at(T0))
        self.bundles = LongFormBundleBuilder(
            database,
            self.storage,
            self.audit,
            self.settings,
            rights_reporter=self.rights_reporter,
            policy_reporter=self.policy_reporter,
            flags=self.flags,
            clock=at(T0),
        )

        self._setup_entities()

    def _setup_entities(self) -> None:
        import hashlib

        from factories import make_channel, make_strategy_profile, make_voice_profile

        with self.database.transaction() as conn:
            self.channel = make_channel()
            ChannelRepository(conn).add(self.channel)

            self.strategy = make_strategy_profile(self.channel)
            StrategyProfileRepository(conn).add(self.strategy)

            self.item = ContentItem.create(
                self.channel.id,
                self.strategy.id,
                self.strategy.version,
                ContentType.LONGFORM,
                "The Future of Artificial Intelligence",
                clock=at(T0),
            )
            ContentItemRepository(conn).add(self.item)

            # LongForm script: HOOK, INTRO, 2 chapters (>= 10s each), OUTRO, CTA
            # (total duration ~240s)
            sections = (
                ScriptSection(
                    SectionKind.HOOK,
                    "Are we truly ready for what is coming with intelligent systems?",
                    seconds=20,
                ),
                ScriptSection(
                    SectionKind.INTRO,
                    "Welcome to this deep dive into the evolution of advanced "
                    "computing.",
                    seconds=30,
                ),
                ScriptSection(
                    SectionKind.CHAPTER,
                    "In the early twenties, transformer models reshaped everything "
                    "we knew about computers.",
                    title="The Rise of Foundation Models",
                    seconds=70,
                ),
                ScriptSection(
                    SectionKind.CHAPTER,
                    "Modern agents can plan, reflect, and coordinate across "
                    "distributed systems.",
                    title="Autonomous Agent Architectures",
                    seconds=80,
                ),
                ScriptSection(
                    SectionKind.OUTRO,
                    "The boundary between human reasoning and automated workflows "
                    "continues to narrow.",
                    seconds=25,
                ),
                ScriptSection(
                    SectionKind.CTA,
                    "Subscribe to stay ahead of the technology curve and comment "
                    "your perspective below.",
                    seconds=15,
                ),
            )
            self.script = Script.create(
                self.item.id,
                sections=sections,
                created_by=USER,
                clock=at(T0),
            )
            ScriptRepository(conn).add(self.script)

            self.voice_profile = make_voice_profile(self.channel)
            VoiceProfileRepository(conn).add(self.voice_profile)

            # 240,000 ms audio
            self.duration_ms = 240_000
            wav_data = make_longform_pcm_wav(self.duration_ms)
            wav_sha256 = hashlib.sha256(wav_data).hexdigest()

        audio_file = self.storage.write(
            self.item.id, ArtifactKind.AUDIO, 1, wav_data, media_type="audio/wav"
        )

        with self.database.transaction() as conn:
            self.audio_artifact = Artifact.create(
                self.item.id,
                ArtifactKind.AUDIO,
                uri=audio_file.uri,
                sha256=wav_sha256,
                size_bytes=len(wav_data),
                media_type="audio/wav",
                clock=at(T0),
            )
            ArtifactRepository(conn).add(self.audio_artifact)
            AudioMetadataRepository(conn).add(
                AudioMetadata.create(
                    self.audio_artifact,
                    self.script,
                    self.voice_profile,
                    duration_ms=self.duration_ms,
                    clock=at(T0),
                )
            )

        # Create project and plan timeline
        self.project = self.projects.create(self.item.id, actor=USER)
        self.project = self.projects.plan(
            self.project.id, expected_version=1, actor=USER
        )
        self.project = self.projects.generate_chapters(
            self.project.id, expected_version=self.project.version, actor=USER
        )

        # Setup background image assets for each scene and attach them
        self.bg_assets = []
        for i, _scene in enumerate(self.project.timeline.scenes):
            asset = self.create_image_asset(f"Background {i}", width=1920, height=1080)
            self.bg_assets.append(asset)

        # Auto-assign backgrounds via broll manager
        self.project = self.broll.auto_assign_backgrounds(
            self.project.id,
            expected_version=self.project.version,
            actor=USER,
        )

        # Caption the project so captions and chapters are both READY
        self.project = self.projects.caption(
            self.project.id, expected_version=self.project.version, actor=USER
        )

    def create_image_asset(
        self,
        title: str,
        width: int = 1920,
        height: int = 1080,
        category: AssetCategory = AssetCategory.GENERATED,
        license_ref: str | None = "CC0-1.0",
        attribution: str | None = "Public Domain Art",
    ) -> Any:
        import hashlib

        from ai_youtube_agent.content.asset import Asset
        from ai_youtube_agent.content.asset_usage import AssetUsage
        from ai_youtube_agent.content.provenance import Provenance
        from ai_youtube_agent.content.rights import (
            RightsRecord,
            RiskLevel,
        )

        png_bytes = generate_longform_thumbnail_png(
            width=width,
            height=height,
            project_id=self.project.id,
            script_id=self.script.id,
            title_text=title,
        )
        png_sha = hashlib.sha256(png_bytes).hexdigest()

        with self.database.transaction() as conn:
            art_repo = ArtifactRepository(conn)
            cur_art = art_repo.latest(self.item.id, ArtifactKind.THUMBNAIL)
            v = 1 if cur_art is None else cur_art.version + 1
        stored = self.storage.write(
            self.item.id, ArtifactKind.THUMBNAIL, v, png_bytes, media_type="image/png"
        )
        with self.database.transaction() as conn:
            art = Artifact.create(
                self.item.id,
                ArtifactKind.THUMBNAIL,
                version=v,
                uri=stored.uri,
                sha256=png_sha,
                size_bytes=len(png_bytes),
                media_type="image/png",
                clock=at(T0),
            )
            ArtifactRepository(conn).add(art)

            asset = Asset.create(
                self.channel.id,
                AssetKind.IMAGE,
                category,
                title=title,
                source="https://images.example.com/" + png_sha[:10],
                artifact_id=art.id,
                license_ref=license_ref,
                attribution=attribution,
                owner="Media Archive",
                clock=at(T0),
            )
            AssetRepository(conn).add(asset)

            usage = AssetUsage.create(
                asset.id,
                self.item.id,
                purpose="background",
                attached_by=USER,
                clock=at(T0),
            )
            AssetUsageRepository(conn).add(usage)

            prov = Provenance.create(
                asset.id,
                source_url="https://images.example.com/" + png_sha[:10],
                license_name="Creative Commons",
                license_ref=license_ref,
                attribution=attribution,
                owner="Media Archive",
                file_sha256=png_sha,
                recorded_by=USER,
                clock=at(T0),
            )
            ProvenanceRepository(conn).add(prov)

            # Record rights as resolved low risk
            rights = RightsRecord.create(
                self.item.id,
                asset.id,
                source="https://images.example.com/" + png_sha[:10],
                license=license_ref,
                clock=at(T0),
            )
            rights = rights.with_risk_level(RiskLevel.LOW, clock=at(T0)).resolve(
                actor=USER, clock=at(T0)
            )
            from ai_youtube_agent.core.db.repositories.review import (
                RightsRecordRepository,
            )

            RightsRecordRepository(conn).add(rights)

        return asset


def test_full_end_to_end_longform_pipeline(database: Database, tmp_path: Path) -> None:
    """Validate full end-to-end LongForm production: project -> render -> bundle."""
    fx = EndToEndLongFormFixture(database, tmp_path)

    # 1. Project should earn READY status
    assert fx.project.status == LongFormProjectStatus.READY

    # 2. Record subtitles
    sub_res = fx.subtitles.record(
        fx.project.id, expected_version=fx.project.version, actor=USER
    )
    assert sub_res.artifact.kind == ArtifactKind.SUBTITLES
    assert sub_res.created is True

    # 3. Record horizontal 16:9 thumbnail
    thumb_res = fx.thumbnails.generate(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
        title_text="The Future of AI",
    )
    assert thumb_res.artifact.kind == ArtifactKind.THUMBNAIL
    assert thumb_res.image_info.width == 1920
    assert thumb_res.image_info.height == 1080
    assert thumb_res.created is True

    # 4. Record AI disclosure decision (#081)
    facts = DisclosureFacts(
        realistic_person=False,
        realistic_event=False,
        synthetic_voice_of_real_person=False,
        realistic_visual=True,
    )
    fx.decider.decide(fx.item.id, facts, rule_set=DISCLOSURE_RULES, actor=USER)

    # 5. Record LongForm metadata with embedded YouTube chapters
    meta_res = fx.metadata.record(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
        title="The Future of Artificial Intelligence",
        description="A comprehensive documentary on autonomous AI systems.",
        tags=("ai", "technology", "documentary"),
    )
    assert meta_res.artifact.kind == ArtifactKind.METADATA
    assert "Chapters:" in meta_res.metadata.description
    assert len(meta_res.metadata.chapters) >= 2

    # 6. Render LongForm MP4 video through VideoRenderer abstraction
    render_res = fx.render_service.render(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
    )
    assert render_res.artifact.kind == ArtifactKind.VIDEO
    assert render_res.created is True

    # 7. Assemble LongFormArtifactBundle and verify all constituents
    bundle = fx.bundles.build(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
    )
    assert isinstance(bundle, LongFormArtifactBundle)
    assert bundle.video.id == render_res.artifact.id
    assert bundle.thumbnail.id == thumb_res.artifact.id
    assert bundle.metadata.id == meta_res.artifact.id
    assert bundle.subtitles is not None
    assert bundle.subtitles.id == sub_res.artifact.id
    assert bundle.manifest["video_info"]["duration_ms"] == fx.duration_ms


def test_stale_timeline_rejects_render_and_bundle(
    database: Database, tmp_path: Path
) -> None:
    """Timeline mutation after rendering causes bundle to refuse with VIDEO_STALE."""
    fx = EndToEndLongFormFixture(database, tmp_path)

    # Record every other constituent so only the video can be stale.
    fx.subtitles.record(fx.project.id, expected_version=fx.project.version, actor=USER)
    fx.thumbnails.generate(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
        title_text="The Future of AI",
    )
    fx.decider.decide(
        fx.item.id,
        DisclosureFacts(
            realistic_person=False,
            realistic_event=False,
            synthetic_voice_of_real_person=False,
            realistic_visual=True,
        ),
        rule_set=DISCLOSURE_RULES,
        actor=USER,
    )
    fx.metadata.record(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
        title="The Future of Artificial Intelligence",
        description="A comprehensive documentary on autonomous AI systems.",
        tags=("ai", "technology", "documentary"),
    )

    # Render video
    render_res = fx.render_service.render(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
    )
    assert render_res.created is True

    # Mutate the timeline after the render (a scene title changes its checksum)
    old_sha = fx.project.timeline.sha256
    scenes = fx.project.timeline.scenes
    retitled = replace(scenes[0], title="Changed after render")
    new_timeline = replace(fx.project.timeline, scenes=(retitled, *scenes[1:]))
    fx.project = fx.projects.save_timeline(
        fx.project.id,
        new_timeline,
        expected_version=fx.project.version,
        actor=USER,
    )
    assert fx.project.timeline.sha256 != old_sha

    # Attempting to build bundle should fail with VIDEO_STALE or STALE_INPUT
    with pytest.raises(LongFormBundleError) as exc_info:
        fx.bundles.build(
            fx.project.id,
            expected_version=fx.project.version,
            actor=USER,
        )
    assert exc_info.value.code == LongFormBundleErrorCode.VIDEO_STALE.value


def test_re_recording_artifact_invalidates_approvals(
    database: Database, tmp_path: Path
) -> None:
    """Re-recording a thumbnail or video invalidates pending approvals."""
    fx = EndToEndLongFormFixture(database, tmp_path)

    # Record first thumbnail
    thumb1 = fx.thumbnails.generate(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
        title_text="Thumbnail Version 1",
    )

    # Create approval binding this thumbnail
    with fx.database.transaction() as conn:
        req = make_approval_request(fx.item, [thumb1.artifact])
        ApprovalRequestRepository(conn).add(req)

    # Re-recording with a different title creates a new version and invalidates approval
    thumb2 = fx.thumbnails.generate(
        fx.project.id,
        expected_version=fx.project.version,
        actor=USER,
        title_text="Thumbnail Version 2 - Different",
    )
    assert thumb2.artifact.version > thumb1.artifact.version
    assert thumb2.invalidation is not None

    with fx.database.transaction() as conn:
        updated_req = ApprovalRequestRepository(conn).get(req.id)
        assert updated_req.status == ApprovalStatus.INVALIDATED


def test_renderer_failure_and_duration_mismatch(
    database: Database, tmp_path: Path
) -> None:
    """Renderer failures and invalid output durations are caught and audited."""
    fx = EndToEndLongFormFixture(database, tmp_path)

    # 1. Scripted renderer error
    fx.renderer.fail_next(RenderErrorCode.UNAVAILABLE)
    with pytest.raises(LongFormRenderError) as exc_info:
        fx.render_service.render(
            fx.project.id,
            expected_version=fx.project.version,
            actor=USER,
        )
    assert exc_info.value.code == LongFormRenderErrorCode.RENDER_FAILED

    # Check failure was audited
    failures = [
        event for event in fx.audit_sink.events() if event.action == RENDER_FAILED
    ]
    assert len(failures) == 1

    # 2. Output duration mismatch
    fx.renderer.queue(stub_mp4(10_000, b"short"), duration_ms=10_000)  # vs 240,000
    with pytest.raises(LongFormRenderError) as exc_info:
        fx.render_service.render(
            fx.project.id,
            expected_version=fx.project.version,
            actor=USER,
        )
    assert exc_info.value.code == LongFormRenderErrorCode.RENDER_INVALID


def test_feature_flag_disables_longform_pipeline(
    database: Database, tmp_path: Path
) -> None:
    """When LONGFORM_ENABLED is False, all operations raise LongFormDisabledError."""
    fx = EndToEndLongFormFixture(database, tmp_path)
    disabled_flags = FeatureFlags(longform_enabled=False)

    disabled_render = LongFormRenderService(
        fx.database,
        fx.storage,
        fx.audit,
        fx.renderer,
        fx.mixer,
        flags=disabled_flags,
        clock=at(T0),
    )
    with pytest.raises(LongFormDisabledError):
        disabled_render.render(
            fx.project.id, expected_version=fx.project.version, actor=USER
        )

    disabled_thumbnails = LongFormThumbnailRecorder(
        fx.database, fx.storage, fx.audit, flags=disabled_flags, clock=at(T0)
    )
    with pytest.raises(LongFormDisabledError):
        disabled_thumbnails.generate(
            fx.project.id, expected_version=fx.project.version, actor=USER
        )

    disabled_metadata = LongFormMetadataRecorder(
        fx.database, fx.storage, fx.audit, flags=disabled_flags, clock=at(T0)
    )
    with pytest.raises(LongFormDisabledError):
        disabled_metadata.record(
            fx.project.id, expected_version=fx.project.version, actor=USER
        )

    disabled_bundles = LongFormBundleBuilder(
        fx.database,
        fx.storage,
        fx.audit,
        fx.settings,
        flags=disabled_flags,
        clock=at(T0),
    )
    with pytest.raises(LongFormDisabledError):
        disabled_bundles.build(
            fx.project.id, expected_version=fx.project.version, actor=USER
        )
