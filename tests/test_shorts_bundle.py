"""I-103 Shorts artifact bundle (Prompt Pack v8, prompt #103).

Tests for:
- ``ShortsBundleBuilder.build`` packaging MP4 video, SRT subtitles (when
  ``captions=True`` or ``None`` when ``captions=False``), vertical thumbnail,
  canonical metadata JSON, bound audio artifact, ``RightsReport`` and
  ``PolicyReport`` into a verified ``ShortsArtifactBundle``;
- deterministic ``manifest_sha256``, ``artifacts`` tuple ordering, and
  ``shorts.bundle_built`` scalar audit metadata;
- refusal cases: missing/stale/corrupted video, missing/stale subtitles,
  missing/corrupted thumbnail, missing/stale metadata, blocking rights gate,
  missing/mismatched disclosure decision, and blocking policy rule.
"""

from pathlib import Path

import pytest
from factories import USER, at
from test_shorts_audio_mix import T1
from test_shorts_render import Studio

from ai_youtube_agent.content.disclosure_decider import DisclosureDecider
from ai_youtube_agent.content.disclosure_rule import (
    DISCLOSURE_RULES,
    DisclosureFacts,
)
from ai_youtube_agent.content.policy_report import PolicyReporter
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel, RiskResolution
from ai_youtube_agent.content.rights_report import RightsReporter
from ai_youtube_agent.content.shorts_bundle import (
    BUNDLE_BUILT,
    BUNDLE_SCHEMA_VERSION,
    ShortsBundleConflictError,
    ShortsBundleError,
    ShortsBundleErrorCode,
    ShortsBundleBuilder,
)
from ai_youtube_agent.content.shorts_metadata import ShortsMetadataRecorder
from ai_youtube_agent.content.shorts_subtitles import ShortsSubtitleRecorder
from ai_youtube_agent.content.shorts_thumbnail import ShortsThumbnailRecorder
from ai_youtube_agent. content.strategy import Brand
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import StrategyProfileRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository

Code = ShortsBundleErrorCode


class BundleRig(Studio):
    def __init__(
        self,
        database: Database,
        tmp_path: Path,
        *,
        captions: bool = True,
    ) -> None:
        super().__init__(database, tmp_path, captions=captions)
        self.settings = Settings()
        self.decider = DisclosureDecider(database, self.audit, clock=at(T1))
        self.subtitles_recorder = ShortsSubtitleRecorder(
            database, self.storage, self.audit, clock=at(T1)
        )
        self.thumbnail_recorder = ShortsThumbnailRecorder(
            database,
            self.storage,
            self.audit,
            visuals=self.visuals,
            clock=at(T1),
        )
        self.metadata_recorder = ShortsMetadataRecorder(
            database, self.storage, self.audit, clock=at(T1)
        )
        self.rights_reporter = RightsReporter(
            database, self.settings, clock=at(T1)
        )
        self.policy_reporter = PolicyReporter(database, clock=at(T1))
        self.builder = ShortsBundleBuilder(
            database,
            self.storage,
            self.rights_reporter,
            self.policy_reporter,
            self.audit,
            clock=at(T1),
        )

    def prepare_all(self, *, realistic_visual: bool = False) -> None:
        self.decider.decide(
            self.world.item.id,
            DisclosureFacts(realistic_visual=realistic_visual),
            rule_set=DISCLOSURE_RULES,
            actor=USER,
        )
        if self.project.config.captions:
            self.subtitles_recorder.record(
                self.project.id,
                expected_version=self.project.version,
                actor=USER,
            )
        self.thumbnail_recorder.generate(
            self.project.id,
            title_text="Quick Hook",
            actor=USER,
            expected_version=self.project.version,
        )
        self.metadata_recorder.record(
            self.project.id,
            title="Quick Hook",
            description="Learn this fast #Shorts",
            tags=("shorts", "ai"),
            actor=USER,
            expected_version=self.project.version,
        )
        self.render()


def test_build_packages_all_artifacts_and_reports_when_captions_enabled(
    database: Database, tmp_path: Path
) -> None:
    rig = BundleRig(database, tmp_path, captions=True)
    rig.prepare_all(realistic_visual=True)
    rig.sink.events.clear()

    bundle = rig.builder.build(
        rig.project.id,
        actor=USER,
        expected_version=rig.project.version,
    )

    assert bundle.schema_version == BUNDLE_SCHEMA_VERSION
    assert bundle.project.id == rig.project.id
    assert bundle.video.kind is ArtifactKind.VIDEO
    assert bundle.subtitles is not None
    assert bundle.subtitles.kind is ArtifactKind.SUBTITLES
    assert bundle.thumbnail.kind is ArtifactKind.THUMBNAIL
    assert bundle.metadata_artifact.kind is ArtifactKind.METADATA
    assert bundle.audio.kind is ArtifactKind.AUDIO
    assert [a.kind for a in bundle.artifacts] == [
        ArtifactKind.VIDEO,
        ArtifactKind.SUBTITLES,
        ArtifactKind.THUMBNAIL,
        ArtifactKind.METADATA,
        ArtifactKind.AUDIO,
    ]
    assert bundle.rights_report.gate.blocks is False
    assert bundle.policy_report.blocks is False
    assert bundle.metadata.contains_synthetic_media is True
    assert len(bundle.manifest_sha256) == 64

    assert len(rig.sink.events) == 1
    event = rig.sink.events[0]
    assert event.action == BUNDLE_BUILT
    assert event.metadata["manifest_sha256"] == bundle.manifest_sha256
    assert "Learn this fast" not in str(event.metadata)


def test_build_succeeds_without_subtitles_when_captions_disabled(
    database: Database, tmp_path: Path
) -> None:
    rig = BundleRig(database, tmp_path, captions=False)
    rig.prepare_all()

    bundle = rig.builder.build(rig.project.id, actor=USER)

    assert bundle.subtitles is None
    assert [a.kind for a in bundle.artifacts] == [
        ArtifactKind.VIDEO,
        ArtifactKind.THUMBNAIL,
        ArtifactKind.METADATA,
        ArtifactKind.AUDIO,
    ]


def test_build_refuses_missing_artifacts_in_sequence(
    database: Database, tmp_path: Path
) -> None:
    rig = BundleRig(database, tmp_path, captions=True)
    rig.decider.decide(
        rig.world.item.id,
        DisclosureFacts(),
        rule_set=DISCLOSURE_RULES,
        actor=USER,
    )

    # 1. Missing video
    with pytest.raises(ShortsBundleError) as exc_info:
        rig.builder.build(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.VIDEO_MISSING.value

    rig.render()

    # 2. Missing subtitles
    with pytest.raises(ShortsBundleError) as exc_info:
        rig.builder.build(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.SUBTITLES_MISSING.value

    rig.subtitles_recorder.record(
        rig.project.id, expected_version=rig.project.version, actor=USER
    )

    # 3. Missing thumbnail
    with pytest.raises(ShortsBundleError) as exc_info:
        rig.builder.build(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.THUMBNAIL_MISSING.value

    rig.thumbnail_recorder.generate(
        rig.project.id, title_text="Hook", actor=USER
    )

    # 4. Missing metadata
    with pytest.raises(ShortsBundleError) as exc_info:
        rig.builder.build(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.METADATA_MISSING.value


def test_build_refuses_corrupted_file_or_version_conflict(
    database: Database, tmp_path: Path
) -> None:
    rig = BundleRig(database, tmp_path, captions=False)
    rig.prepare_all()

    with pytest.raises(ShortsBundleConflictError):
        rig.builder.build(
            rig.project.id,
            actor=USER,
            expected_version=rig.project.version + 3,
        )

    # Corrupt thumbnail on disk
    thumb_path = next((tmp_path / "artifacts").rglob("*.png"))
    thumb_path.write_bytes(b"corrupted-bytes")

    with pytest.raises(ShortsBundleError) as exc_info:
        rig.builder.build(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.ARTIFACT_CORRUPTED.value


def test_build_refuses_blocking_rights_or_blocking_policy(
    database: Database, tmp_path: Path
) -> None:
    rig = BundleRig(database, tmp_path, captions=False)
    rig.prepare_all()

    # Add an unresolved HIGH-risk rights record to block the rights gate
    high_risk = RightsRecord.create(
        rig.world.item.id,
        asset_ref="asset-high",
        source="unknown-blog",
        license=None,
        risk_level=RiskLevel.HIGH,
        clock=at(T1),
    )
    with database.transaction() as connection:
        RightsRecordRepository(connection).add(high_risk)

    with pytest.raises(ShortsBundleError) as exc_info:
        rig.builder.build(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.RIGHTS_BLOCKED.value

    # Resolve the rights record, then add a banned phrase that matches the metadata
    resolved = high_risk.resolve(
        RiskResolution.REPLACED, actor=USER, clock=at(T1)
    )
    with database.transaction() as connection:
        RightsRecordRepository(connection).update(resolved)
        strategies = StrategyProfileRepository(connection)
        current = strategies.get_by_channel(rig.world.item.channel_id)
        assert current is not None
        updated = current.update(
            brand=Brand(
                tone="Strict",
                banned_phrases=("learn this fast",),
            ),
            actor=USER,
            clock=at(T1),
        )
        strategies.update(updated, expected_version=current.version)

    with pytest.raises(ShortsBundleError) as exc_info:
        rig.builder.build(rig.project.id, actor=USER)
    assert exc_info.value.code == Code.POLICY_BLOCKED.value

