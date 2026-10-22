import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.channel import Channel, YouTubeIdentifiers
from ai_youtube_agent.content.longform_project import (
    LongFormProjectStatus,
)
from ai_youtube_agent.content.longform_script_generator import LongFormDisabledError
from ai_youtube_agent.content.longform_service import (
    CREATED,
    REBOUND,
    TIMELINE_SAVED,
    LongFormProjectError,
    LongFormProjectErrorCode,
    LongFormProjectService,
)
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.shorts_timeline import (
    AssetRole,
    SceneAsset,
)
from ai_youtube_agent.content.strategy import (
    Audience,
    FormatSettings,
    LongFormFormat,
    Niche,
    Pillar,
    ShortsFormat,
    StrategyProfile,
)
from ai_youtube_agent.content.voice import AudioMetadata, VoiceProfile
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.migrate import migrate
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
from ai_youtube_agent.core.flags import FeatureFlags

USER = Actor(ActorKind.USER, "producer")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.now
        self.now = current + timedelta(seconds=1)
        return current


def _seed_longform_item(
    database: Database, clock: Clock, *, chapters: bool = True, captions: bool = True
) -> tuple[Channel, ContentItem, Script, Artifact]:
    channel = Channel.create(
        "LongForm Science",
        YouTubeIdentifiers("UC" + "1" * 22, "@science"),
        clock=clock,
    )
    strategy = StrategyProfile.create(
        channel.id,
        niche=Niche("Deep Science", (Pillar("physics"),)),
        audience=Audience("Adult learners"),
        format=FormatSettings(
            ShortsFormat(15, 60),
            LongFormFormat(
                181,
                900,
                captions=captions,
                chapters=chapters,
            ),
        ),
        actor=USER,
        clock=clock,
    )
    item = ContentItem.create(
        channel.id,
        strategy.id,
        strategy.version,
        ContentType.LONGFORM,
        "How Fusion Reactors Work",
        clock=clock,
    )
    sections = (
        ScriptSection(SectionKind.HOOK, "Fusion powers the stars.", seconds=15),
        ScriptSection(SectionKind.INTRO, "Why fusion is hard on Earth.", seconds=25),
        ScriptSection(
            SectionKind.CHAPTER,
            "Magnetic confinement keeps plasma away from walls.",
            title="Magnetic Confinement",
            seconds=60,
        ),
        ScriptSection(
            SectionKind.CHAPTER,
            "Inertial confinement compresses fuel pellets with lasers.",
            title="Laser Compression",
            seconds=60,
        ),
        ScriptSection(
            SectionKind.CHAPTER,
            "Net energy gain requires exceeding the Lawson criterion.",
            title="The Lawson Criterion",
            seconds=60,
        ),
        ScriptSection(SectionKind.OUTRO, "Summary of reactor milestones.", seconds=15),
        ScriptSection(SectionKind.CTA, "Subscribe for more physics.", seconds=15),
    )
    script = Script.create(item.id, sections=sections, created_by=USER, clock=clock)
    profile = VoiceProfile.create(
        channel.id,
        provider="mock_tts",
        voice_id="en-narrator",
        language="en-US",
        actor=USER,
        clock=clock,
    )
    audio_artifact = Artifact.create(
        item.id,
        ArtifactKind.AUDIO,
        uri=f"{item.id}/audio/v1.wav",
        sha256=hashlib.sha256(b"longform-voice").hexdigest(),
        size_bytes=48_000,
        media_type="audio/wav",
        clock=clock,
    )
    audio_meta = AudioMetadata.create(
        audio_artifact,
        script,
        profile,
        duration_ms=250_000,
        clock=clock,
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
        ScriptRepository(conn).add(script)
        VoiceProfileRepository(conn).add(profile)
        ArtifactRepository(conn).add(audio_artifact)
        AudioMetadataRepository(conn).add(audio_meta)
    return channel, item, script, audio_artifact


def test_longform_service_refuses_when_feature_flag_is_disabled(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    sink = InMemoryAuditSink()
    service = LongFormProjectService(
        database, AuditLog(sink), FeatureFlags(longform_enabled=False)
    )

    with pytest.raises(LongFormDisabledError):
        service.create("any-item", actor=USER)
    assert sink.events() == ()


def test_longform_service_lifecycle_create_plan_caption_and_ready(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    clock = Clock()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    channel, item, _, _ = _seed_longform_item(database, clock)
    service = LongFormProjectService(
        database, audit, FeatureFlags(longform_enabled=True), clock=clock
    )

    project = service.create(item.id, actor=USER)
    assert project.status is LongFormProjectStatus.DRAFT
    assert project.version == 1
    assert project.audio_within_target is True

    planned = service.plan(project.id, expected_version=1, actor=USER)
    assert planned.status is LongFormProjectStatus.PLANNED
    assert planned.version == 2
    assert len(planned.timeline.scenes) == 7
    assert planned.timeline.chapters == ()

    chaptered = service.generate_chapters(
        project.id, expected_version=planned.version, actor=USER
    )
    assert len(chaptered.timeline.chapters) == 4
    assert chaptered.timeline.chapters[0].start_ms == 0

    captioned = service.caption(
        project.id, expected_version=chaptered.version, actor=USER
    )
    assert captioned.status is LongFormProjectStatus.PLANNED
    assert {c.scene_id for c in captioned.timeline.captions} == {
        scene.id for scene in captioned.timeline.scenes
    }

    # Attach a background asset to every scene -> project becomes READY.
    registry = AssetRegistry(database, audit, clock=clock)
    asset = registry.register(
        channel.id,
        kind=AssetKind.VIDEO_CLIP,
        category=AssetCategory.LICENSED,
        title="Plasma Chamber Loop",
        source="https://assets.example/plasma.mp4",
        license_ref="CC-BY-4.0",
        attribution="Plasma Visuals",
        actor=USER,
    )
    registry.attach(asset.id, item.id, purpose="scene backgrounds", actor=USER)

    from dataclasses import replace

    scenes = tuple(
        replace(
            scene,
            assets=(SceneAsset(asset_id=asset.id, role=AssetRole.BACKGROUND),),
        )
        for scene in captioned.timeline.scenes
    )
    ready = service.save_timeline(
        project.id,
        replace(captioned.timeline, scenes=scenes),
        expected_version=captioned.version,
        actor=USER,
    )
    assert ready.status is LongFormProjectStatus.READY
    assert service.is_current(project.id) is True

    actions = [e.action for e in sink.events()]
    assert CREATED in actions
    assert TIMELINE_SAVED in actions


def test_longform_service_rebind_resets_to_draft(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    clock = Clock()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    _, item, _, _ = _seed_longform_item(database, clock)
    service = LongFormProjectService(
        database, audit, FeatureFlags(longform_enabled=True), clock=clock
    )

    created = service.create(item.id, actor=USER)
    planned = service.plan(created.id, expected_version=1, actor=USER)
    rebound = service.rebind(planned.id, expected_version=planned.version, actor=USER)

    assert rebound.status is LongFormProjectStatus.DRAFT
    assert rebound.timeline.scenes == ()
    assert any(e.action == REBOUND for e in sink.events())


def test_longform_service_refuses_chapters_when_disabled_in_config(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    clock = Clock()
    sink = InMemoryAuditSink()
    _, item, _, _ = _seed_longform_item(database, clock, chapters=False)
    service = LongFormProjectService(
        database, AuditLog(sink), FeatureFlags(longform_enabled=True), clock=clock
    )

    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)
    assert planned.timeline.chapters == ()

    with pytest.raises(LongFormProjectError) as exc_info:
        service.generate_chapters(
            project.id, expected_version=planned.version, actor=USER
        )
    assert exc_info.value.code == LongFormProjectErrorCode.CHAPTER_INVALID.value
