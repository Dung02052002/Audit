import io
import math
import struct
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from tests.test_longform_project import USER, Clock

from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import AssetRegistry
from ai_youtube_agent.content.channel import Channel, YouTubeIdentifiers
from ai_youtube_agent.content.longform_audio_mix import (
    LongFormAudioMixer,
    LongFormMixError,
    LongFormMixErrorCode,
)
from ai_youtube_agent.content.longform_service import LongFormProjectService
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.shorts_audio_mix import MIXED
from ai_youtube_agent.content.shorts_timeline import (
    AssetPlacement,
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
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
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


def _sine_wav(
    duration_ms: int,
    *,
    rate: int = 24_000,
    freq_hz: float = 220.0,
    amplitude: float = 0.25,
) -> bytes:
    frames = int(rate * duration_ms / 1000)
    peak = int(32767 * amplitude)
    samples = [
        int(peak * math.sin(2.0 * math.pi * freq_hz * i / rate)) for i in range(frames)
    ]
    pcm = struct.pack(f"<{frames}h", *samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def test_longform_audio_mixer_mixes_voice_music_and_sfx(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    storage = LocalFileStorage(tmp_path / "artifacts")
    clock = Clock()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    flags = FeatureFlags(longform_enabled=True)

    channel = Channel.create(
        "LongForm Audio Channel",
        YouTubeIdentifiers("UC" + "2" * 22, "@audio"),
        clock=clock,
    )
    strategy = StrategyProfile.create(
        channel.id,
        niche=Niche("Audio Engineering", (Pillar("audio"),)),
        audience=Audience("Engineers"),
        format=FormatSettings(
            ShortsFormat(15, 60),
            LongFormFormat(
                181,
                600,
                captions=True,
                chapters=False,
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
        "Mastering LongForm Audio",
        clock=clock,
    )
    script = Script.create(
        item.id,
        sections=(
            ScriptSection(SectionKind.HOOK, "Hook line.", seconds=2),
            ScriptSection(SectionKind.INTRO, "Intro line.", seconds=2),
            ScriptSection(SectionKind.BODY, "Body part one.", seconds=2),
            ScriptSection(SectionKind.OUTRO, "Outro line.", seconds=2),
            ScriptSection(SectionKind.CTA, "Subscribe.", seconds=2),
        ),
        created_by=USER,
        clock=clock,
    )
    profile = VoiceProfile.create(
        channel.id,
        provider="mock_tts",
        voice_id="en-host",
        language="en-US",
        actor=USER,
        clock=clock,
    )
    voice_wav = _sine_wav(10_000, freq_hz=220.0, amplitude=0.35)
    written_voice = storage.write(
        item.id, ArtifactKind.AUDIO, 1, voice_wav, media_type="audio/wav"
    )
    voice_artifact = Artifact.create(
        item.id,
        ArtifactKind.AUDIO,
        uri=written_voice.uri,
        sha256=written_voice.sha256,
        size_bytes=written_voice.size_bytes,
        media_type="audio/wav",
        clock=clock,
    )
    audio_meta = AudioMetadata.create(
        voice_artifact,
        script,
        profile,
        duration_ms=10_000,
        clock=clock,
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(strategy)
        ContentItemRepository(conn).add(item)
        ScriptRepository(conn).add(script)
        VoiceProfileRepository(conn).add(profile)
        ArtifactRepository(conn).add(voice_artifact)
        AudioMetadataRepository(conn).add(audio_meta)

    # Register and attach a music bed and a scene SFX
    music_wav = _sine_wav(10_000, freq_hz=110.0, amplitude=0.2)
    written_music = storage.write(
        item.id, ArtifactKind.AUDIO, 2, music_wav, media_type="audio/wav"
    )
    music_artifact = voice_artifact.next_version(
        uri=written_music.uri,
        sha256=written_music.sha256,
        size_bytes=written_music.size_bytes,
        media_type="audio/wav",
        clock=clock,
    )
    sfx_wav = _sine_wav(1_000, freq_hz=440.0, amplitude=0.2)
    written_sfx = storage.write(
        item.id, ArtifactKind.AUDIO, 3, sfx_wav, media_type="audio/wav"
    )
    sfx_artifact = music_artifact.next_version(
        uri=written_sfx.uri,
        sha256=written_sfx.sha256,
        size_bytes=written_sfx.size_bytes,
        media_type="audio/wav",
        clock=clock,
    )
    # Re-add voice_artifact as latest by version 4 so project binds to voice
    voice_latest = sfx_artifact.next_version(
        uri=written_voice.uri,
        sha256=written_voice.sha256,
        size_bytes=written_voice.size_bytes,
        media_type="audio/wav",
        clock=clock,
    )
    voice_latest_meta = AudioMetadata.create(
        voice_latest,
        script,
        profile,
        duration_ms=10_000,
        clock=clock,
    )
    with database.transaction() as conn:
        ArtifactRepository(conn).add(music_artifact)
        ArtifactRepository(conn).add(sfx_artifact)
        ArtifactRepository(conn).add(voice_latest)
        AudioMetadataRepository(conn).add(voice_latest_meta)

    registry = AssetRegistry(database, audit, clock=clock)
    music_asset = registry.register(
        channel.id,
        kind=AssetKind.MUSIC,
        category=AssetCategory.GENERATED,
        title="Ambient Bed",
        source="internal://music",
        artifact_id=music_artifact.id,
        actor=USER,
    )
    sfx_asset = registry.register(
        channel.id,
        kind=AssetKind.AUDIO,
        category=AssetCategory.GENERATED,
        title="Whoosh",
        source="internal://sfx",
        artifact_id=sfx_artifact.id,
        actor=USER,
    )
    registry.attach(music_asset.id, item.id, purpose="music", actor=USER)
    registry.attach(sfx_asset.id, item.id, purpose="sfx", actor=USER)

    service = LongFormProjectService(database, audit, flags, clock=clock)
    project = service.create(item.id, actor=USER)
    planned = service.plan(project.id, expected_version=1, actor=USER)

    scene0 = replace(
        planned.timeline.scenes[0],
        assets=(
            SceneAsset(
                asset_id=sfx_asset.id,
                role=AssetRole.SFX,
                start_ms=0,
                end_ms=1_000,
            ),
        ),
    )
    timeline = replace(
        planned.timeline,
        scenes=(scene0, *planned.timeline.scenes[1:]),
        music=(
            AssetPlacement(
                asset_id=music_asset.id,
                start_ms=0,
                end_ms=10_000,
            ),
        ),
    )
    saved = service.save_timeline(
        project.id, timeline, expected_version=planned.version, actor=USER
    )

    mixer = LongFormAudioMixer(database, storage, audit, flags)
    outcome = mixer.mix(saved.id, expected_version=saved.version, actor=USER)

    assert outcome.wav.startswith(b"RIFF")
    assert outcome.timeline_sha256 == saved.timeline.sha256
    assert outcome.voice_sha256 == saved.audio_sha256
    assert outcome.asset_ids == tuple(sorted((music_asset.id, sfx_asset.id)))

    events = [e for e in sink.events() if e.action == MIXED]
    assert len(events) == 1
    assert events[0].metadata["music_count"] == 1
    assert events[0].metadata["sfx_count"] == 1


def test_longform_audio_mixer_refuses_draft_project(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    database = Database(db_path)
    storage = LocalFileStorage(tmp_path / "artifacts")
    clock = Clock()
    audit = AuditLog(InMemoryAuditSink())
    flags = FeatureFlags(longform_enabled=True)

    from tests.test_longform_project import _seed_longform_item

    _, item, _, _ = _seed_longform_item(database, clock)
    service = LongFormProjectService(database, audit, flags, clock=clock)
    project = service.create(item.id, actor=USER)
    mixer = LongFormAudioMixer(database, storage, audit, flags)

    with pytest.raises(LongFormMixError) as exc_info:
        mixer.mix(project.id, expected_version=project.version, actor=USER)
    assert exc_info.value.code == LongFormMixErrorCode.NOT_READY.value
