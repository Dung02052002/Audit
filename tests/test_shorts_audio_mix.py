"""I-099 Shorts audio mix service (Prompt Pack v8, prompt #099).

Rules decided on 2026-10-17:

- ``ShortsAudioMixer.mix`` returns the mix in memory (``MixOutcome``); nothing
  is stored and the only write is the ``audio.mixed`` audit event, which carries
  ids, checksums, counts, constraint values and report numbers, never bytes;
- the project must be read at ``expected_version``, not a draft and still bound
  to the latest script and audio; the assets of the timeline pass the I-096
  checks again;
- music is absolute, scene sound effects are scene relative (made absolute);
- the bytes come from an ``AssetSource`` (default: the artifact of a generated
  asset through the storage, checksum verified); the voice from the project's
  audio artifact;
- the result is self-checked with ``audio_validation.analyze``.
"""

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.audio_mix import (
    MixConstraints,
    PcmAudio,
    decode_wav,
    encode_wav,
)
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.shorts_audio_mix import (
    HTTP_STATUS,
    MIXED,
    USER_MESSAGES,
    ArtifactAssetSource,
    AssetSource,
    MixOutcome,
    ShortsAudioMixer,
    ShortsMixError,
    ShortsMixErrorCode,
)
from ai_youtube_agent.content.shorts_project import ShortsProjectStatus
from ai_youtube_agent.content.shorts_timeline import (
    AssetPlacement,
    AssetRole,
    SceneAsset,
    Timeline,
)
from ai_youtube_agent.content.strategy import ShortsFormat
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, AuditResult, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
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
from ai_youtube_agent.core.db.repositories.shorts import ShortsProjectRepository
from factories import (
    T0,
    USER,
    at,
    make_audio_metadata,
    make_channel,
)
from test_audio_mix import dc, tone
from test_shorts_project import World
from test_shorts_scene import Stage

Code = ShortsMixErrorCode
T1 = T0 + timedelta(hours=1)
VOICE_RATE = 16000
DURATION_MS = 6000


class FakeSource:
    """Asset audio kept in memory: asset id -> bytes."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.reads: list[str] = []

    def read_audio(self, asset: Asset) -> bytes:
        self.reads.append(asset.id)
        if asset.id not in self.files:
            raise FileNotFoundError(asset.id)
        return self.files[asset.id]


class Shop(Stage):
    """A Shorts project over a real voice file, with sounds ready to place."""

    def __init__(
        self,
        database: Database,
        tmp_path: Path,
        *,
        plan: bool = True,
        voice: PcmAudio | None = None,
    ) -> None:
        self.database = database
        self.world = World(database, ShortsFormat(15, 60, captions=False))
        self.storage = LocalFileStorage(tmp_path / "artifacts")
        self.voice = voice or tone(DURATION_MS, 0.3, rate=VOICE_RATE)
        self.source = FakeSource()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))
        item = self.world.item
        script = Script.create(
            item.id,
            sections=[
                ScriptSection.create(SectionKind.BODY, text)
                for text in ("one two", "three four five", "six seven eight nine ten")
            ],
            clock=at(T0),
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
            clock=at(T0),
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

    def mixer(self, source=None, constraints=None, storage=None) -> ShortsAudioMixer:
        return ShortsAudioMixer(
            self.database,
            storage or self.storage,
            self.audit,
            source or self.source,
            constraints=constraints,
        )

    def mix(self, mixer=None, **kwargs) -> MixOutcome:
        return (mixer or self.mixer()).mix(
            kwargs.pop("project_id", self.project.id),
            expected_version=kwargs.pop("expected_version", self.project.version),
            actor=USER,
        )

    def sound(
        self,
        kind=AssetKind.AUDIO,
        pcm=None,
        category=AssetCategory.USER_OWNED,
        **flags,
    ) -> Asset:
        """A registered, attached sound whose bytes the fake source serves."""
        asset = self.add_asset(kind, category, **flags)
        pcm = pcm or tone(3000, 0.5, freq=440, rate=22050)
        self.source.files[asset.id] = encode_wav(pcm)
        return asset

    def build(self, *, music=(), sfx=()) -> Timeline:
        """The planned timeline plus music and ``(scene index, SceneAsset)``."""
        scenes = list(self.project.timeline.scenes)
        for index, placed in sfx:
            scenes[index] = dataclasses.replace(
                scenes[index], assets=(*scenes[index].assets, placed)
            )
        return dataclasses.replace(
            self.project.timeline, scenes=tuple(scenes), music=tuple(music)
        )

    def force(self, timeline: Timeline) -> None:
        """Store a timeline without the save-time checks (assets changed since)."""
        updated = self.project.with_timeline(
            timeline, self.project.status, actor=USER, clock=at(T1)
        )
        with self.database.transaction() as connection:
            ShortsProjectRepository(connection).update(
                updated, expected_version=self.project.version
            )
        self.project = updated

    def scene_start(self, index: int) -> int:
        return self.project.timeline.scenes[index].start_ms

    def snapshot(self) -> tuple:
        return (
            self.world.count("shorts_projects"),
            self.world.count("artifacts"),
            self.world.count("qc_results"),
            self.world.project_of(),
            sorted(p.name for p in self.storage.root.rglob("*") if p.is_file()),
        )

    def refuse(self, code: Code, mixer=None, **kwargs) -> ShortsMixError:
        before = self.snapshot()
        events = len(self.sink.events())
        with pytest.raises(ShortsMixError) as info:
            self.mix(mixer, **kwargs)
        assert info.value.code == code.value
        assert self.snapshot() == before
        assert len(self.sink.events()) == events
        return info.value


@pytest.fixture
def shop(database: Database, tmp_path: Path) -> Shop:
    return Shop(database, tmp_path)


def full_shop(shop: Shop) -> tuple[Shop, str, str]:
    """Music on the timeline and a sound effect in the second scene."""
    song = shop.sound(AssetKind.MUSIC)
    ding = shop.sound(AssetKind.AUDIO, tone(500, 0.5, freq=900, rate=8000))
    shop.project = shop.world.service.save_timeline(
        shop.project.id,
        shop.build(
            music=(AssetPlacement(song.id, fade_in_ms=300),),
            sfx=((1, SceneAsset(ding.id, AssetRole.SFX, start_ms=100, end_ms=400)),),
        ),
        expected_version=shop.project.version,
        actor=USER,
    )
    return shop, song.id, ding.id


# The mix


def test_mix_returns_the_voice_format_and_length_in_memory(shop: Shop) -> None:
    shop, song, ding = full_shop(shop)
    before = shop.snapshot()

    outcome = shop.mix()

    out = decode_wav(outcome.wav)
    assert (out.sample_rate, out.channels, out.frames) == (
        VOICE_RATE,
        1,
        VOICE_RATE * DURATION_MS // 1000,
    )
    assert outcome.timeline_sha256 == shop.project.timeline.sha256
    assert outcome.voice_sha256 == shop.voice_artifact.sha256
    assert outcome.asset_ids == tuple(sorted((song, ding)))
    assert outcome.report.duration_ms == DURATION_MS
    assert shop.snapshot() == before  # nothing stored


def test_the_report_places_music_and_scene_effects_in_absolute_time(
    shop: Shop,
) -> None:
    shop, song, ding = full_shop(shop)

    report = shop.mix().report

    tracks = {item.label: item for item in report.tracks}
    assert tracks[ding].start_ms == shop.scene_start(1) + 100
    assert tracks[ding].end_ms == shop.scene_start(1) + 400
    assert tracks[song].start_ms == 0
    assert tracks[song].gain_db == -18.0 and tracks[ding].gain_db == -6.0


def test_a_project_without_sounds_mixes_the_normalised_voice(shop: Shop) -> None:
    outcome = shop.mix()

    assert outcome.asset_ids == ()
    assert outcome.report.tracks == ()
    assert outcome.report.rms_dbfs == pytest.approx(-18.0, abs=0.2)


def test_the_same_project_gives_the_same_mix(shop: Shop) -> None:
    shop, *_ = full_shop(shop)

    first = shop.mix()
    second = shop.mix()

    assert first.wav == second.wav and first.sha256 == second.sha256
    assert len(shop.sink.events()) == 2


def test_the_mix_passes_its_own_checks(shop: Shop) -> None:
    shop, *_ = full_shop(shop)

    outcome = shop.mix()

    assert outcome.qc_warnings == ()
    assert outcome.report.peak_dbfs <= -1.0 + 0.01


def test_quiet_results_are_returned_with_their_warnings(shop: Shop) -> None:
    constraints = MixConstraints(target_rms_dbfs=-40, max_boost_db=24)

    outcome = shop.mix(shop.mixer(constraints=constraints))

    assert outcome.qc_warnings == ("audio.level",)
    event = shop.sink.events()[0]
    assert event.metadata["qc_warned"] == "audio.level"
    assert event.metadata["target_rms_dbfs"] == -40


def test_the_constraints_of_the_constructor_are_used(shop: Shop) -> None:
    outcome = shop.mix(shop.mixer(constraints=MixConstraints(target_rms_dbfs=-24)))

    assert outcome.report.rms_dbfs == pytest.approx(-24.0, abs=0.2)


def test_mixing_audits_ids_checksums_and_numbers_only(shop: Shop) -> None:
    shop, song, ding = full_shop(shop)

    outcome = shop.mix()

    (event,) = shop.sink.events()
    assert event.action == MIXED == "audio.mixed"
    assert event.entity.type == "shorts_project" and event.entity.id == shop.project.id
    assert event.result is AuditResult.SUCCESS and event.actor == USER
    metadata = dict(event.metadata)
    assert metadata["project_id"] == shop.project.id
    assert metadata["content_item_id"] == shop.world.item.id
    assert metadata["project_version"] == shop.project.version
    assert metadata["timeline_sha256"] == outcome.timeline_sha256
    assert metadata["voice_sha256"] == outcome.voice_sha256
    assert metadata["sha256"] == outcome.sha256
    assert metadata["size_bytes"] == len(outcome.wav)
    assert metadata["asset_ids"] == ",".join(sorted((song, ding)))
    assert metadata["asset_count"] == 2
    assert (metadata["music_count"], metadata["sfx_count"]) == (1, 1)
    assert metadata["duck_db"] == 12.0 and metadata["peak_ceiling_dbfs"] == -1.0
    assert metadata["peak_dbfs"] == outcome.report.peak_dbfs
    assert metadata["normalisation_gain_db"] == outcome.report.normalisation_gain_db
    assert metadata["ducked_ratio"] == outcome.report.ducked_ratio
    assert all(
        isinstance(v, str | int | float | bool | None) for v in metadata.values()
    )
    assert not any(isinstance(v, bytes) for v in metadata.values())


# Refusals about the project


def test_an_unknown_project_is_refused(shop: Shop) -> None:
    error = shop.refuse(Code.PROJECT_NOT_FOUND, project_id="nope")

    assert HTTP_STATUS[Code.PROJECT_NOT_FOUND] == 404 and error.code.endswith("found")


def test_a_project_read_at_another_version_is_a_conflict(shop: Shop) -> None:
    shop.refuse(Code.CONFLICT, expected_version=shop.project.version + 1)


def test_a_draft_project_is_not_ready(database: Database, tmp_path: Path) -> None:
    draft = Shop(database, tmp_path, plan=False)
    assert draft.project.status is ShortsProjectStatus.DRAFT

    draft.refuse(Code.NOT_READY)


def test_a_project_with_a_newer_voice_is_stale(shop: Shop) -> None:
    script = shop.world.add_script()
    shop.world.add_audio(script)

    shop.refuse(Code.STALE_INPUT)


# Refusals about the assets


def test_an_unknown_asset_is_unavailable(shop: Shop) -> None:
    shop.force(shop.build(music=(AssetPlacement("missing"),)))

    shop.refuse(Code.ASSET_UNAVAILABLE)


def test_an_asset_of_another_channel_is_unavailable(
    shop: Shop, database: Database
) -> None:
    other = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(other)
    foreign = shop.sound(AssetKind.MUSIC, channel_id=other.id, attach=False)
    shop.force(shop.build(music=(AssetPlacement(foreign.id),)))

    shop.refuse(Code.ASSET_UNAVAILABLE)


def test_an_asset_not_attached_to_the_item_is_unavailable(shop: Shop) -> None:
    loose = shop.sound(AssetKind.MUSIC, attach=False)
    shop.force(shop.build(music=(AssetPlacement(loose.id),)))

    shop.refuse(Code.ASSET_UNAVAILABLE)


def test_an_asset_with_unknown_rights_is_refused_with_its_detail(shop: Shop) -> None:
    unknown = shop.sound(AssetKind.MUSIC, category=AssetCategory.UNKNOWN)
    shop.force(shop.build(music=(AssetPlacement(unknown.id),)))

    error = shop.refuse(Code.ASSET_RIGHTS)

    assert unknown.id in error.detail
    assert shop.source.reads == []  # no file is read for a refused asset


def test_an_asset_that_is_not_audio_is_refused(shop: Shop) -> None:
    picture = shop.sound(AssetKind.IMAGE)
    shop.force(shop.build(music=(AssetPlacement(picture.id),)))

    shop.refuse(Code.ASSET_NOT_AUDIO)


def test_sound_effects_must_be_audio_assets_not_music(shop: Shop) -> None:
    song = shop.sound(AssetKind.MUSIC)
    shop.force(shop.build(sfx=((0, SceneAsset(song.id, AssetRole.SFX)),)))

    shop.refuse(Code.ASSET_NOT_AUDIO)


def test_a_missing_asset_file_is_unavailable(shop: Shop) -> None:
    song = shop.sound(AssetKind.MUSIC)
    del shop.source.files[song.id]
    shop.force(shop.build(music=(AssetPlacement(song.id),)))

    shop.refuse(Code.ASSET_UNAVAILABLE)


@pytest.mark.parametrize("data", [b"not audio", b""])
def test_an_asset_that_is_not_a_wav_is_unsupported(shop: Shop, data) -> None:
    song = shop.sound(AssetKind.MUSIC)
    shop.source.files[song.id] = data
    shop.force(shop.build(music=(AssetPlacement(song.id),)))

    shop.refuse(Code.FORMAT_UNSUPPORTED)


def test_a_source_window_that_is_empty_is_an_invalid_mix(shop: Shop) -> None:
    song = shop.sound(AssetKind.MUSIC)
    shop.force(shop.build(music=(AssetPlacement(song.id, source_in_ms=600_000),)))

    shop.refuse(Code.MIX_INVALID)


# Refusals about the voice


def test_a_missing_voice_file_is_unavailable(shop: Shop) -> None:
    shop.storage.delete(shop.voice_artifact.uri)

    shop.refuse(Code.ASSET_UNAVAILABLE)


def test_a_changed_voice_file_is_unavailable(shop: Shop) -> None:
    other = encode_wav(tone(DURATION_MS, 0.2, rate=VOICE_RATE))
    shop.storage.write(
        shop.world.item.id, ArtifactKind.AUDIO, 1, other, media_type="audio/wav"
    )

    shop.refuse(Code.ASSET_UNAVAILABLE)


def test_a_silent_voice_fails_the_self_check(
    database: Database, tmp_path: Path
) -> None:
    quiet = Shop(database, tmp_path, voice=dc(DURATION_MS, 0.0, rate=VOICE_RATE))

    error = quiet.refuse(Code.MIX_INVALID)

    assert "audio.silence" in error.detail


# The default asset source


def generated(shop: Shop, pcm: PcmAudio, *, store: bool = True) -> Asset:
    """A generated music asset whose file is an artifact in the storage."""
    item = shop.world.item
    data = encode_wav(pcm)
    stored = shop.storage.write(
        item.id, ArtifactKind.VIDEO, 1, data, media_type="audio/wav"
    )
    artifact = Artifact.create(
        item.id,
        ArtifactKind.VIDEO,
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type="audio/wav",
        clock=at(T0),
    )
    asset = Asset.create(
        shop.world.channel.id,
        AssetKind.MUSIC,
        AssetCategory.GENERATED,
        title="Generated loop",
        source="music-provider",
        artifact_id=artifact.id,
        clock=at(T0),
    )
    with shop.database.transaction() as connection:
        ArtifactRepository(connection).add(artifact)
        AssetRepository(connection).add(asset, source_key="g1", title_key="g1")
        AssetUsageRepository(connection).add(
            AssetUsage.create(asset.id, item.id, attached_by=USER, clock=at(T0))
        )
    return asset


def test_the_default_source_reads_the_artifact_of_a_generated_asset(
    shop: Shop,
) -> None:
    asset = generated(shop, tone(3000, 0.4, rate=22050))
    shop.project = shop.world.service.save_timeline(
        shop.project.id,
        shop.build(music=(AssetPlacement(asset.id),)),
        expected_version=shop.project.version,
        actor=USER,
    )
    mixer = ShortsAudioMixer(shop.database, shop.storage, shop.audit)

    outcome = shop.mix(mixer)

    assert outcome.asset_ids == (asset.id,)
    assert isinstance(ArtifactAssetSource(shop.database, shop.storage), AssetSource)


def test_the_default_source_refuses_an_asset_without_a_stored_file(shop: Shop) -> None:
    song = shop.sound(AssetKind.MUSIC)  # user owned: no artifact_id
    shop.project = shop.world.service.save_timeline(
        shop.project.id,
        shop.build(music=(AssetPlacement(song.id),)),
        expected_version=shop.project.version,
        actor=USER,
    )

    shop.refuse(
        Code.ASSET_UNAVAILABLE,
        ShortsAudioMixer(shop.database, shop.storage, shop.audit),
    )


def test_the_default_source_checks_the_checksum(shop: Shop) -> None:
    asset = generated(shop, tone(3000, 0.4, rate=22050))
    source = ArtifactAssetSource(shop.database, shop.storage)
    assert source.read_audio(asset)
    shop.storage.write(
        shop.world.item.id,
        ArtifactKind.VIDEO,
        1,
        encode_wav(tone(3000, 0.1, rate=22050)),
        media_type="audio/wav",
    )

    with pytest.raises(ValueError):
        source.read_audio(asset)


def test_the_default_source_refuses_an_unknown_artifact(shop: Shop) -> None:
    asset = generated(shop, tone(3000, 0.4, rate=22050))
    ghost = dataclasses.replace(asset, artifact_id="no-such-artifact")

    with pytest.raises(ValueError):
        ArtifactAssetSource(shop.database, shop.storage).read_audio(ghost)


# Error model


def test_every_code_has_a_message_and_the_conflicts_are_409() -> None:
    assert set(USER_MESSAGES) == set(Code)
    assert HTTP_STATUS[Code.CONFLICT] == 409 and HTTP_STATUS[Code.STALE_INPUT] == 409
    error = ShortsMixError("detail", code=Code.NOT_READY)
    assert error.code == "domain.shorts_mix_not_ready"
    assert error.user_message == USER_MESSAGES[Code.NOT_READY]
    assert ShortsMixError().code == "domain.shorts_mix"
