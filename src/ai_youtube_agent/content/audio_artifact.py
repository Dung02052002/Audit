"""Audio Artifact (Prompt Pack v8, prompt #089), context C7 Voice & Audio.

``AudioArtifactRecorder`` stores the audio of a finished TTS job (#088) as a
new version of the item's audio ``Artifact`` with its ``AudioMetadata``. The
rules were approved by the user on 2026-10-16:

- The file goes to the ``ArtifactStorage`` (``core/artifact_storage.py``,
  local files under ``Settings.artifact_dir``); the database keeps the
  artifact (uri, sha256, size, media type) and the audio metadata (script
  version, voice profile id and version, provider, duration). No migration.
- Everything is checked in one transaction before the file is written: the
  media type is an audio type (``AUDIO_MEDIA_TYPES``), the content item exists
  and belongs to the voice profile's channel, the script is stored, the voice
  profile is stored exactly as given (same version and provider), and the job
  is the succeeded ``audio.generate`` job of this script and voice version
  whose ``audio.completed`` checkpoint has the checksum and duration of the
  result.
- The same audio twice is not a new version: when the latest audio version
  has the same sha256 and its metadata names the same script, voice profile
  version and provider, it is returned with ``created=False`` and nothing is
  written or audited; with other metadata it is refused
  (``domain.audio_artifact_conflict``).
- A new version is the latest + 1. The artifact, the invalidation of stale
  approvals (#035) and the audio metadata are stored in the same transaction.
  When anything fails after the file was written, the file is deleted again
  (any failing delete is logged with the uri only) and the first error is
  raised.
- After the commit, each invalidated approval is audited as
  ``approval.invalidated`` and the new version as ``audio.recorded`` by the
  system, with scalar data only, never the script text.

Cost is not recorded here: #092 records the TTS cost against the artifact id.
Out of scope: audio validation (#090) and retries (#091).
"""

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.tts_job import COMPLETED, JOB_KIND, TtsResult
from ai_youtube_agent.content.voice import AudioMetadata, VoiceProfile
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage, StoredFile
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import VoiceProfileRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    AudioMetadataRepository,
    ContentItemRepository,
    ScriptRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.core.version_invalidation import (
    InvalidationResult,
    invalidate_stale_approvals,
    record_invalidations,
)
from ai_youtube_agent.pipeline.idempotency import audio_key
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus

logger = get_logger(__name__)

ACTOR = Actor(ActorKind.SYSTEM, "audio_artifact")
RECORDED = "audio.recorded"
# Audio only: a media type the storage gains later (video) is not audio.
AUDIO_MEDIA_TYPES = frozenset({"audio/wav"})
Clock = Callable[[], datetime]


class AudioArtifactErrorCode(StrEnum):
    RECORD = "domain.audio_artifact"
    ITEM_NOT_FOUND = "domain.audio_item_not_found"
    CHANNEL_MISMATCH = "domain.audio_channel_mismatch"
    SCRIPT_NOT_FOUND = "domain.audio_script_not_found"
    VOICE_NOT_FOUND = "domain.audio_voice_not_found"
    JOB_MISMATCH = "domain.audio_job_mismatch"
    UNSUPPORTED_MEDIA_TYPE = "domain.audio_unsupported_media_type"
    CONFLICT = "domain.audio_artifact_conflict"


USER_MESSAGES = {
    AudioArtifactErrorCode.RECORD: "The audio could not be saved.",
    AudioArtifactErrorCode.ITEM_NOT_FOUND: "This content item does not exist.",
    AudioArtifactErrorCode.CHANNEL_MISMATCH: "The voice profile belongs to another "
    "channel.",
    AudioArtifactErrorCode.SCRIPT_NOT_FOUND: "The script of this audio was not found.",
    AudioArtifactErrorCode.VOICE_NOT_FOUND: "The voice profile of this audio was "
    "not found.",
    AudioArtifactErrorCode.JOB_MISMATCH: "The audio does not match its finished "
    "audio job.",
    AudioArtifactErrorCode.UNSUPPORTED_MEDIA_TYPE: "This audio format cannot be saved.",
    AudioArtifactErrorCode.CONFLICT: "The same audio is already saved for another "
    "script or voice.",
}


class AudioArtifactError(DomainError):
    """Audio was refused; ``code`` is an ``AudioArtifactErrorCode``."""

    default_code = AudioArtifactErrorCode.RECORD.value
    default_user_message = USER_MESSAGES[AudioArtifactErrorCode.RECORD]

    def __init__(
        self,
        detail: str = "",
        *,
        code: AudioArtifactErrorCode = AudioArtifactErrorCode.RECORD,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])


@dataclass(frozen=True)
class AudioArtifactRecord:
    artifact: Artifact
    audio: AudioMetadata
    created: bool
    invalidation: InvalidationResult | None


class AudioArtifactRecorder:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._clock = clock

    def record(
        self, result: TtsResult, script: Script, voice: VoiceProfile
    ) -> AudioArtifactRecord:
        if result.media_type not in AUDIO_MEDIA_TYPES:
            raise AudioArtifactError(
                f"job {result.job_id}: media type {result.media_type} cannot be stored",
                code=AudioArtifactErrorCode.UNSUPPORTED_MEDIA_TYPE,
            )
        checksum = _sha256(result.audio)
        item_id = script.content_item_id
        written: StoredFile | None = None
        try:
            with self._database.transaction() as connection:
                self._check(connection, result, script, voice, checksum)
                artifacts = ArtifactRepository(connection)
                audios = AudioMetadataRepository(connection)
                latest = artifacts.latest(item_id, ArtifactKind.AUDIO)
                if latest is not None and latest.sha256 == checksum:
                    audio = audios.get_by_artifact(latest.id)
                    return _replay(latest, audio, script, voice)
                version = latest.version + 1 if latest else 1
                written = self._storage.write(
                    item_id,
                    ArtifactKind.AUDIO,
                    version,
                    result.audio,
                    media_type=result.media_type,
                )
                artifact = _artifact(latest, item_id, written, result, self._clock)
                audio = AudioMetadata.create(
                    artifact,
                    script,
                    voice,
                    duration_ms=result.duration_ms,
                    clock=self._clock,
                )
                artifacts.add(artifact)
                invalidation = invalidate_stale_approvals(
                    connection, item_id, clock=self._clock
                )
                audios.add(audio)
        except BaseException:
            if written is not None:
                self._discard(written.uri)
            raise
        record_invalidations(self._audit, invalidation)
        self._audit.record(
            RECORDED,
            ACTOR,
            EntityRef("artifact", artifact.id),
            AuditResult.SUCCESS,
            _event_metadata(artifact, audio),
        )
        return AudioArtifactRecord(artifact, audio, True, invalidation)

    def _check(
        self,
        connection: sqlite3.Connection,
        result: TtsResult,
        script: Script,
        voice: VoiceProfile,
        checksum: str,
    ) -> None:
        item = ContentItemRepository(connection).get(script.content_item_id)
        if item is None:
            raise AudioArtifactError(
                f"content item {script.content_item_id} does not exist",
                code=AudioArtifactErrorCode.ITEM_NOT_FOUND,
            )
        if item.channel_id != voice.channel_id:
            raise AudioArtifactError(
                f"voice profile {voice.id} belongs to another channel than "
                f"content item {item.id}",
                code=AudioArtifactErrorCode.CHANNEL_MISMATCH,
            )
        stored_script = ScriptRepository(connection).get(script.id)
        if stored_script is None or stored_script.content_item_id != item.id:
            raise AudioArtifactError(
                f"script {script.id} is not stored for content item {item.id}",
                code=AudioArtifactErrorCode.SCRIPT_NOT_FOUND,
            )
        stored_voice = VoiceProfileRepository(connection).get(voice.id)
        if stored_voice != voice:
            raise AudioArtifactError(
                f"voice profile {voice.id} version {voice.version} is not stored "
                f"as given for channel {voice.channel_id}",
                code=AudioArtifactErrorCode.VOICE_NOT_FOUND,
            )
        job = AIJobRepository(connection).get(result.job_id)
        problem = _job_problem(job, result, script, voice, item.id, checksum)
        if problem:
            raise AudioArtifactError(
                f"job {result.job_id}: {problem}",
                code=AudioArtifactErrorCode.JOB_MISMATCH,
            )

    def _discard(self, uri: str) -> None:
        try:
            self._storage.delete(uri)
        except Exception:
            # The first error must win; the cleanup failure is only logged.
            logger.warning(
                "audio file could not be removed after a failed record",
                extra={"fields": {"uri": uri}},
            )


def _job_problem(
    job: AIJob | None,
    result: TtsResult,
    script: Script,
    voice: VoiceProfile,
    item_id: str,
    checksum: str,
) -> str | None:
    """Why ``job`` did not make ``result`` for this script and voice, or None."""
    if job is None:
        return "the job does not exist"
    if job.kind != JOB_KIND:
        return f"the job is a {job.kind} job"
    if job.idempotency_key != audio_key(script.id, voice.id, voice.version):
        return "the job voiced another script or voice profile version"
    if job.status is not AIJobStatus.SUCCEEDED:
        return f"the job is {job.status.value}"
    if job.content_item_id != item_id:
        return "the job belongs to another content item"
    completed = None
    for saved in reversed(job.checkpoints):
        if saved.step == COMPLETED:
            completed = saved.data
            break
    if completed is None or completed.get("checksum") != checksum:
        return "the audio differs from the completed job"
    if completed.get("duration_ms") != result.duration_ms:
        return "the duration differs from the completed job"
    return None


def _replay(
    latest: Artifact,
    audio: AudioMetadata | None,
    script: Script,
    voice: VoiceProfile,
) -> AudioArtifactRecord:
    if (
        audio is None
        or audio.script_id != script.id
        or audio.voice_profile_id != voice.id
        or audio.voice_profile_version != voice.version
        or audio.provider != voice.provider
    ):
        raise AudioArtifactError(
            f"audio version {latest.version} of content item "
            f"{latest.content_item_id} has the same sha256 but other metadata",
            code=AudioArtifactErrorCode.CONFLICT,
        )
    return AudioArtifactRecord(latest, audio, False, None)


def _artifact(
    latest: Artifact | None,
    item_id: str,
    stored: StoredFile,
    result: TtsResult,
    clock: Clock | None,
) -> Artifact:
    if latest is None:
        return Artifact.create(
            item_id,
            ArtifactKind.AUDIO,
            uri=stored.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type=result.media_type,
            clock=clock,
        )
    return latest.next_version(
        uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        media_type=result.media_type,
        clock=clock,
    )


def _event_metadata(
    artifact: Artifact, audio: AudioMetadata
) -> Mapping[str, MetadataValue]:
    return {
        "content_item_id": artifact.content_item_id,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "duration_ms": audio.duration_ms,
        "provider": audio.provider,
        "script_id": audio.script_id,
        "voice_profile_id": audio.voice_profile_id,
        "voice_profile_version": audio.voice_profile_version,
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
