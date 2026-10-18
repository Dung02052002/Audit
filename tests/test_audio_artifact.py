"""H-089 Audio Artifact (Prompt Pack v8, prompt #089).

Rules the user approved on 2026-10-16:

- the WAV of a succeeded TTS job (#088) is written to the ``ArtifactStorage``
  and stored as the next audio ``Artifact`` version with its ``AudioMetadata``;
- the item, channel, script, voice profile and job are checked in one
  transaction before the file is written;
- the same audio again returns the latest version (``created=False``) and
  writes nothing; with other metadata it is a conflict;
- stale approvals are invalidated in the same transaction (#035); a failure
  after the write rolls the rows back and deletes the file;
- ``approval.invalidated`` and one ``audio.recorded`` event (scalars only, no
  script text) are audited after the commit; no cost is recorded.
"""

import dataclasses
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.content import audio_artifact
from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.audio_artifact import (
    ACTOR,
    AUDIO_MEDIA_TYPES,
    RECORDED,
    AudioArtifactError,
    AudioArtifactErrorCode,
    AudioArtifactRecord,
    AudioArtifactRecorder,
)
from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.tts_job import (
    JOB_KIND,
    InMemoryChunkStore,
    TtsJobRunner,
    TtsResult,
)
from ai_youtube_agent.content.voice import AudioMetadata, VoiceProfile
from ai_youtube_agent.core import artifact_storage
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import (
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
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
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.idempotency import audio_key
from ai_youtube_agent.pipeline.job import AIJob
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from factories import (
    T0,
    at,
    make_ai_job,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
    make_voice_profile,
)

SECRET = "Zebra-quartz-7731"
T1 = T0 + timedelta(hours=1)
TEXTS = (f"Opening line with {SECRET}.", "A second part.", "The last part.")


class Ticker:
    """A clock that moves 1 ms forward on every call."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self):
        self.now += timedelta(milliseconds=1)
        return self.now


class World:
    def __init__(
        self, database: Database, tmp_path: Path, status: ContentStatus | None = None
    ) -> None:
        self.database = database
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        self.item = make_content_item(self.channel, self.strategy)
        if status is not None:
            self.item = dataclasses.replace(self.item, status=status)
        self.voice = make_voice_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            ContentItemRepository(connection).add(self.item)
            VoiceProfileRepository(connection).add(self.voice)
        self.clock = Ticker()
        self.mock = MockSpeechSynthesizer(name="mock_tts", clock=self.clock)
        self.store = InMemoryChunkStore()
        self.root = tmp_path / "artifacts"
        self.storage = LocalFileStorage(self.root)
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))

    def recorder(self, storage=None, sink=None) -> AudioArtifactRecorder:
        audit = AuditLog(sink, clock=at(T1)) if sink is not None else self.audit
        return AudioArtifactRecorder(
            self.database, storage or self.storage, audit, clock=at(T1)
        )

    def script(self, *texts: str, item_id: str | None = None) -> Script:
        sections = tuple(
            ScriptSection(SectionKind.BODY, text) for text in texts or TEXTS
        )
        script = Script.create(
            item_id or self.item.id, sections=sections, clock=self.clock
        )
        self.save_script(script)
        return script

    def next_script(self, script: Script, *sections: ScriptSection) -> Script:
        changed = script.next_version(sections=sections, clock=self.clock)
        self.save_script(changed)
        return changed

    def save_script(self, script: Script) -> None:
        with self.database.transaction() as connection:
            ScriptRepository(connection).add(script)

    def run(self, script: Script, voice: VoiceProfile | None = None) -> TtsResult:
        runner = TtsJobRunner(self.database, self.mock, self.store, clock=self.clock)
        return runner.run(script, voice or self.voice)

    def other_item(self):
        other = make_content_item(self.channel, self.strategy)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(other)
        return other

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            (count,) = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        return count

    def artifacts(self) -> list[Artifact]:
        with self.database.transaction() as connection:
            return ArtifactRepository(connection).list_by_content_item(self.item.id)

    def audio_of(self, artifact: Artifact) -> AudioMetadata | None:
        with self.database.transaction() as connection:
            return AudioMetadataRepository(connection).get_by_artifact(artifact.id)

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.rglob("*") if p.is_file())

    def path(self, version: int) -> Path:
        return self.root.resolve() / self.item.id / "audio" / f"v{version}.wav"


@pytest.fixture
def world(database: Database, tmp_path: Path) -> World:
    return World(database, tmp_path)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expect_nothing_stored(world: World) -> None:
    assert world.count("artifacts") == 0
    assert world.count("audio_metadata") == 0
    assert world.files() == []
    assert world.sink.events() == ()


# A new version


def test_the_first_audio_is_version_one_and_readable(world: World) -> None:
    script = world.script()
    result = world.run(script)

    record = world.recorder().record(result, script, world.voice)

    assert isinstance(record, AudioArtifactRecord)
    assert record.created is True
    artifact = record.artifact
    assert artifact.content_item_id == world.item.id
    assert artifact.kind is ArtifactKind.AUDIO
    assert artifact.version == 1
    assert artifact.uri == world.path(1).as_uri()
    assert artifact.sha256 == sha256(result.audio)
    assert artifact.size_bytes == len(result.audio)
    assert artifact.media_type == "audio/wav"
    assert artifact.created_at == T1

    assert world.path(1).read_bytes() == result.audio
    assert world.storage.read(artifact.uri) == result.audio
    assert world.files() == [world.path(1)]

    audio = record.audio
    assert audio.artifact_id == artifact.id
    assert audio.script_id == script.id
    assert audio.voice_profile_id == world.voice.id
    assert audio.voice_profile_version == world.voice.version
    assert audio.provider == "mock_tts"
    assert audio.duration_ms == result.duration_ms
    assert audio.created_at == T1

    assert world.artifacts() == [artifact]
    assert world.audio_of(artifact) == audio
    assert record.invalidation is not None
    assert record.invalidation.invalidated == ()
    assert record.invalidation.item_reset is False


def test_a_new_script_version_gives_version_two(world: World) -> None:
    first_script = world.script()
    first = world.recorder().record(world.run(first_script), first_script, world.voice)
    second_script = world.next_script(
        first_script, ScriptSection(SectionKind.BODY, "Completely new words.")
    )
    result = world.run(second_script)

    second = world.recorder().record(result, second_script, world.voice)

    assert second.created is True
    assert second.artifact.version == 2
    assert second.artifact.id != first.artifact.id
    assert second.artifact.uri == world.path(2).as_uri()
    assert second.audio.script_id == second_script.id
    assert world.path(2).read_bytes() == result.audio
    assert world.path(1).read_bytes() == world.storage.read(first.artifact.uri)
    assert sha256(world.path(1).read_bytes()) == first.artifact.sha256
    assert world.artifacts() == [first.artifact, second.artifact]
    with world.database.transaction() as connection:
        latest = ArtifactRepository(connection).latest(
            world.item.id, ArtifactKind.AUDIO
        )
    assert latest == second.artifact


def test_older_audio_content_again_is_a_new_version(world: World) -> None:
    # A -> B -> A: only the latest version decides whether it is a replay.
    first_script = world.script("Alpha words.")
    first = world.recorder().record(world.run(first_script), first_script, world.voice)
    second_script = world.next_script(
        first_script, ScriptSection(SectionKind.BODY, "Beta words.")
    )
    world.recorder().record(world.run(second_script), second_script, world.voice)
    third_script = world.next_script(
        second_script, ScriptSection(SectionKind.BODY, "Alpha words.")
    )

    third = world.recorder().record(world.run(third_script), third_script, world.voice)

    assert third.created is True
    assert third.artifact.version == 3
    assert third.artifact.sha256 == first.artifact.sha256


# Replays and conflicts


def test_the_same_result_again_is_a_replay(world: World) -> None:
    script = world.script()
    result = world.run(script)
    first = world.recorder().record(result, script, world.voice)

    again = world.recorder().record(result, script, world.voice)

    assert again == AudioArtifactRecord(first.artifact, first.audio, False, None)
    assert world.count("artifacts") == 1
    assert world.count("audio_metadata") == 1
    assert world.files() == [world.path(1)]
    assert [e.action for e in world.sink.events()] == [RECORDED]


def test_a_rebuilt_job_result_is_a_replay(world: World) -> None:
    script = world.script()
    first = world.recorder().record(world.run(script), script, world.voice)

    rebuilt = world.run(script)
    again = world.recorder().record(rebuilt, script, world.voice)

    assert rebuilt.synthesized_chunks == 0
    assert again.created is False
    assert again.artifact == first.artifact
    assert world.count("artifacts") == 1


def test_the_same_audio_for_another_script_version_is_a_conflict(
    world: World,
) -> None:
    script = world.script("Only one part.")
    world.recorder().record(world.run(script), script, world.voice)
    # Same section text with a different timing: same audio, other script.
    twin = world.next_script(
        script, ScriptSection(SectionKind.BODY, "Only one part.", seconds=30)
    )
    result = world.run(twin)

    with pytest.raises(AudioArtifactError) as caught:
        world.recorder().record(result, twin, world.voice)

    assert caught.value.code == AudioArtifactErrorCode.CONFLICT.value
    assert world.count("artifacts") == 1
    assert world.count("audio_metadata") == 1
    assert world.files() == [world.path(1)]
    assert [e.action for e in world.sink.events()] == [RECORDED]


def stored_twin(world: World, result: TtsResult, script: Script, **changes):
    """An audio version with the result's checksum and changed metadata."""
    artifact = make_artifact(
        world.item,
        ArtifactKind.AUDIO,
        sha256=sha256(result.audio),
        media_type="audio/wav",
    )
    with world.database.transaction() as connection:
        ArtifactRepository(connection).add(artifact)
        if changes.get("audio", True) is not None:
            audio = AudioMetadata.create(
                artifact, script, world.voice, duration_ms=result.duration_ms
            )
            fields = {k: v for k, v in changes.items() if k != "audio"}
            AudioMetadataRepository(connection).add(
                dataclasses.replace(audio, **fields)
            )
    return artifact


@pytest.mark.parametrize(
    "changes",
    [
        {"audio": None},
        {"provider": "other_tts"},
        {"voice_profile_version": 2},
        {"script_id": "other"},
    ],
    ids=["no metadata", "provider", "voice version", "script"],
)
def test_the_same_audio_with_other_metadata_is_a_conflict(
    world: World, changes: dict
) -> None:
    script = world.script()
    result = world.run(script)
    if changes.get("script_id") == "other":
        other = world.next_script(
            script, ScriptSection(SectionKind.BODY, "Another script.")
        )
        changes = {"script_id": other.id}
    stored_twin(world, result, script, **changes)

    with pytest.raises(AudioArtifactError) as caught:
        world.recorder().record(result, script, world.voice)

    assert caught.value.code == AudioArtifactErrorCode.CONFLICT.value
    assert world.count("artifacts") == 1
    assert world.files() == []
    assert world.sink.events() == ()


# Checks before anything is written


def unsupported_media_type(world: World):
    script = world.script()
    result = dataclasses.replace(world.run(script), media_type="audio/mpeg")
    return result, script, world.voice


def item_not_found(world: World):
    script = world.script()
    result = world.run(script)
    return result, dataclasses.replace(script, content_item_id="missing"), world.voice


def channel_mismatch(world: World):
    script = world.script()
    result = world.run(script)
    return result, script, dataclasses.replace(world.voice, channel_id="elsewhere")


def script_not_stored(world: World):
    script = world.script()
    result = world.run(script)
    unstored = Script.create(world.item.id, f"Unsaved {SECRET}.", clock=world.clock)
    return result, unstored, world.voice


def script_of_another_item(world: World):
    script = world.script()
    result = world.run(script)
    foreign = world.script("Elsewhere.", item_id=world.other_item().id)
    moved = dataclasses.replace(foreign, content_item_id=world.item.id)
    return result, moved, world.voice


def voice_not_stored(world: World):
    script = world.script()
    result = world.run(script)
    return result, script, dataclasses.replace(world.voice, id="missing-voice")


def unknown_job(world: World):
    script = world.script()
    return dataclasses.replace(world.run(script), job_id="missing"), script, world.voice


def job_of_another_kind(world: World):
    script = world.script()
    result = world.run(script)
    other = make_ai_job(world.item)
    with world.database.transaction() as connection:
        AIJobRepository(connection).add(other)
    return dataclasses.replace(result, job_id=other.id), script, world.voice


def job_of_another_script(world: World):
    script = world.script()
    result = world.run(script)
    other = world.next_script(
        script, ScriptSection(SectionKind.BODY, "A different script.")
    )
    return result, other, world.voice


def job_of_another_voice_version(world: World):
    script = world.script()
    result = world.run(script)
    changed = world.voice.update(
        actor=world.voice.updated_by, clock=at(T0), voice_id="vi-male-02"
    )
    with world.database.transaction() as connection:
        VoiceProfileRepository(connection).update(
            changed, expected_version=world.voice.version
        )
    return result, script, changed


def voice_version_not_stored(world: World):
    script = world.script()
    result = world.run(script)
    return result, script, dataclasses.replace(world.voice, version=2)


def voice_provider_not_stored(world: World):
    script = world.script()
    result = world.run(script)
    return result, script, dataclasses.replace(world.voice, provider="other_tts")


def job_not_finished(world: World):
    script = world.script()
    result = world.run(script)
    fresh = world.next_script(
        script, ScriptSection(SectionKind.BODY, "Not voiced yet.")
    )
    job = AIJob.create(
        JOB_KIND,
        audio_key(fresh.id, world.voice.id, world.voice.version),
        content_item_id=world.item.id,
        clock=world.clock,
    )
    with world.database.transaction() as connection:
        AIJobRepository(connection).add(job)
    return dataclasses.replace(result, job_id=job.id), fresh, world.voice


def job_of_another_item(world: World):
    script = world.script()
    result = world.run(script)
    other = world.other_item()
    with world.database.transaction() as connection:
        connection.execute(
            "UPDATE ai_jobs SET content_item_id = ? WHERE id = ?",
            (other.id, result.job_id),
        )
    return result, script, world.voice


def audio_changed(world: World):
    script = world.script()
    result = world.run(script)
    changed = dataclasses.replace(result, audio=result.audio + b"\0\0")
    return changed, script, world.voice


def duration_changed(world: World):
    script = world.script()
    result = world.run(script)
    return (
        dataclasses.replace(result, duration_ms=result.duration_ms + 1),
        script,
        world.voice,
    )


Code = AudioArtifactErrorCode
PRE_CHECKS = [
    (unsupported_media_type, Code.UNSUPPORTED_MEDIA_TYPE),
    (item_not_found, Code.ITEM_NOT_FOUND),
    (channel_mismatch, Code.CHANNEL_MISMATCH),
    (script_not_stored, Code.SCRIPT_NOT_FOUND),
    (script_of_another_item, Code.SCRIPT_NOT_FOUND),
    (voice_not_stored, Code.VOICE_NOT_FOUND),
    (voice_version_not_stored, Code.VOICE_NOT_FOUND),
    (voice_provider_not_stored, Code.VOICE_NOT_FOUND),
    (unknown_job, Code.JOB_MISMATCH),
    (job_of_another_kind, Code.JOB_MISMATCH),
    (job_of_another_script, Code.JOB_MISMATCH),
    (job_of_another_voice_version, Code.JOB_MISMATCH),
    (job_not_finished, Code.JOB_MISMATCH),
    (job_of_another_item, Code.JOB_MISMATCH),
    (audio_changed, Code.JOB_MISMATCH),
    (duration_changed, Code.JOB_MISMATCH),
]


@pytest.mark.parametrize(
    ("case", "code"), PRE_CHECKS, ids=[case.__name__ for case, _ in PRE_CHECKS]
)
def test_every_check_refuses_before_anything_is_written(
    world: World, case, code: AudioArtifactErrorCode
) -> None:
    result, script, voice = case(world)

    with pytest.raises(AudioArtifactError) as caught:
        world.recorder().record(result, script, voice)

    error = caught.value
    assert isinstance(error, DomainError)
    assert error.code == code.value
    assert error.user_message == audio_artifact.USER_MESSAGES[code]
    assert SECRET not in str(error)
    assert SECRET not in error.detail
    assert SECRET not in error.user_message
    expect_nothing_stored(world)


def test_a_media_type_the_storage_keeps_is_refused_when_it_is_not_audio(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(artifact_storage.MEDIA_EXTENSIONS, "video/mp4", "mp4")
    script = world.script()
    result = dataclasses.replace(world.run(script), media_type="video/mp4")

    with pytest.raises(AudioArtifactError) as caught:
        world.recorder().record(result, script, world.voice)

    assert caught.value.code == Code.UNSUPPORTED_MEDIA_TYPE.value
    expect_nothing_stored(world)


def test_only_wav_is_audio_and_the_storage_can_keep_it() -> None:
    assert AUDIO_MEDIA_TYPES == frozenset({"audio/wav"})
    assert AUDIO_MEDIA_TYPES <= set(artifact_storage.MEDIA_EXTENSIONS)


def test_every_error_code_has_a_user_message() -> None:
    assert set(audio_artifact.USER_MESSAGES) == set(AudioArtifactErrorCode)
    assert all(code.value.startswith("domain.") for code in AudioArtifactErrorCode)


# Failures after the file was written


def test_a_failing_metadata_insert_rolls_back_and_deletes_the_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = world.script()
    result = world.run(script)

    def broken(self, audio):
        raise RuntimeError("metadata insert failed")

    monkeypatch.setattr(AudioMetadataRepository, "add", broken)

    with pytest.raises(RuntimeError, match="metadata insert failed"):
        world.recorder().record(result, script, world.voice)

    expect_nothing_stored(world)


def test_a_failing_invalidation_rolls_back_and_deletes_the_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = world.script()
    result = world.run(script)

    def broken(connection, item_id, *, clock=None):
        raise RuntimeError("invalidation failed")

    monkeypatch.setattr(audio_artifact, "invalidate_stale_approvals", broken)

    with pytest.raises(RuntimeError, match="invalidation failed"):
        world.recorder().record(result, script, world.voice)

    expect_nothing_stored(world)


def test_a_failing_cleanup_still_raises_the_first_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StuckStorage(LocalFileStorage):
        def delete(self, uri: str) -> None:
            raise OSError("file is locked")

    script = world.script()
    result = world.run(script)

    def broken(self, audio):
        raise RuntimeError("metadata insert failed")

    monkeypatch.setattr(AudioMetadataRepository, "add", broken)

    with pytest.raises(RuntimeError, match="metadata insert failed"):
        world.recorder(StuckStorage(world.root)).record(result, script, world.voice)

    assert world.count("artifacts") == 0
    assert world.count("audio_metadata") == 0
    assert world.files() == [world.path(1)]  # the orphan the next write replaces
    assert world.sink.events() == ()


def test_any_failing_cleanup_still_raises_the_first_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenStorage(LocalFileStorage):
        def delete(self, uri: str) -> None:
            raise ValueError("cleanup refused the uri")

    script = world.script()
    result = world.run(script)

    def broken(self, audio):
        raise RuntimeError("metadata insert failed")

    monkeypatch.setattr(AudioMetadataRepository, "add", broken)

    with pytest.raises(RuntimeError, match="metadata insert failed"):
        world.recorder(BrokenStorage(world.root)).record(result, script, world.voice)

    assert world.count("artifacts") == 0
    assert world.count("audio_metadata") == 0
    assert world.sink.events() == ()


def test_a_failing_write_stores_no_rows(world: World) -> None:
    class FullStorage(LocalFileStorage):
        def write(self, *args, **kwargs):
            raise OSError("disk full")

    script = world.script()
    result = world.run(script)

    with pytest.raises(OSError, match="disk full"):
        world.recorder(FullStorage(world.root)).record(result, script, world.voice)

    expect_nothing_stored(world)


def test_an_orphan_from_a_failed_run_is_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StuckStorage(LocalFileStorage):
        def delete(self, uri: str) -> None:
            raise OSError("file is locked")

    def broken(self, audio):
        raise RuntimeError("metadata insert failed")

    script = world.script()
    result = world.run(script)
    with monkeypatch.context() as patch:
        patch.setattr(AudioMetadataRepository, "add", broken)
        with pytest.raises(RuntimeError):
            world.recorder(StuckStorage(world.root)).record(result, script, world.voice)
    world.path(1).write_bytes(b"spoiled orphan")

    record = world.recorder().record(result, script, world.voice)

    assert record.created is True
    assert record.artifact.version == 1
    assert world.path(1).read_bytes() == result.audio


# Approvals and audit


def test_a_new_version_invalidates_the_approved_request_after_commit(
    database: Database, tmp_path: Path
) -> None:
    world = World(database, tmp_path, ContentStatus.APPROVED)
    first_script = world.script()
    first = world.recorder().record(world.run(first_script), first_script, world.voice)
    approval = make_approval_request(
        world.item, [first.artifact], status=ApprovalStatus.APPROVED
    )
    with database.transaction() as connection:
        ApprovalRequestRepository(connection).add(approval)
    second_script = world.next_script(
        first_script, ScriptSection(SectionKind.BODY, "A changed script.")
    )
    result = world.run(second_script)
    seen: list[tuple[str, list[ApprovalStatus]]] = []

    def load_statuses() -> list[ApprovalStatus]:
        with database.transaction() as connection:
            requests = ApprovalRequestRepository(connection).list_by_content_item(
                world.item.id
            )
        return [request.status for request in requests]

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            # A fresh connection only sees committed data.
            seen.append((event.action, load_statuses()))
            super().append(event)

    sink = CheckingSink()

    second = world.recorder(sink=sink).record(result, second_script, world.voice)

    assert second.artifact.version == 2
    invalidation = second.invalidation
    assert invalidation is not None
    assert invalidation.item_reset is True
    (entry,) = invalidation.invalidated
    assert entry.approval.id == approval.id
    assert entry.previous_status is ApprovalStatus.APPROVED
    assert entry.stale_kinds == (ArtifactKind.AUDIO,)
    assert load_statuses() == [ApprovalStatus.INVALIDATED]
    with database.transaction() as connection:
        item = ContentItemRepository(connection).get(world.item.id)
    assert item.status is ContentStatus.GENERATING
    assert seen == [
        ("approval.invalidated", [ApprovalStatus.INVALIDATED]),
        (RECORDED, [ApprovalStatus.INVALIDATED]),
    ]
    invalidated, recorded = sink.events()
    assert (invalidated.entity.type, invalidated.entity.id) == (
        "approval_request",
        approval.id,
    )
    assert dict(invalidated.metadata) == {
        "content_item_id": world.item.id,
        "previous_status": "approved",
        "stale_kinds": "audio",
        "item_reset": True,
    }
    assert recorded.entity.id == second.artifact.id


def test_one_audio_recorded_event_with_scalar_metadata(world: World) -> None:
    script = world.script()
    result = world.run(script)

    record = world.recorder().record(result, script, world.voice)

    (event,) = world.sink.events()
    assert event.action == RECORDED == "audio.recorded"
    assert event.actor == ACTOR
    assert event.actor.kind is ActorKind.SYSTEM
    assert event.actor.id == "audio_artifact"
    assert (event.entity.type, event.entity.id) == ("artifact", record.artifact.id)
    assert event.result is AuditResult.SUCCESS
    assert dict(event.metadata) == {
        "content_item_id": world.item.id,
        "version": 1,
        "sha256": sha256(result.audio),
        "size_bytes": len(result.audio),
        "duration_ms": result.duration_ms,
        "provider": "mock_tts",
        "script_id": script.id,
        "voice_profile_id": world.voice.id,
        "voice_profile_version": world.voice.version,
    }
    assert all(SECRET not in str(value) for value in event.metadata.values())


def test_the_audio_event_is_recorded_after_the_commit(
    world: World,
) -> None:
    script = world.script()
    result = world.run(script)
    seen: list[int] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            seen.append(world.count("audio_metadata"))
            super().append(event)

    world.recorder(sink=CheckingSink()).record(result, script, world.voice)

    assert seen == [1]


def test_no_cost_is_recorded(world: World) -> None:
    script = world.script()

    world.recorder().record(world.run(script), script, world.voice)

    assert world.count("cost_records") == 0


def test_another_items_audio_does_not_count_as_the_latest(world: World) -> None:
    other = world.other_item()
    with world.database.transaction() as connection:
        ArtifactRepository(connection).add(make_artifact(other, ArtifactKind.AUDIO))
    script = world.script()

    record = world.recorder().record(world.run(script), script, world.voice)

    assert record.artifact.version == 1


def test_a_changed_voice_profile_gives_a_new_version(world: World) -> None:
    script = world.script()
    first = world.recorder().record(world.run(script), script, world.voice)
    changed = world.voice.update(
        actor=world.voice.updated_by, clock=at(T0), voice_id="vi-male-02"
    )
    with world.database.transaction() as connection:
        VoiceProfileRepository(connection).update(
            changed, expected_version=world.voice.version
        )

    second = world.recorder().record(world.run(script, changed), script, changed)

    assert second.artifact.version == 2
    assert second.artifact.sha256 != first.artifact.sha256
    assert second.audio.voice_profile_version == 2
