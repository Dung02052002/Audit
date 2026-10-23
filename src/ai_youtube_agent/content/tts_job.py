"""TTS Job (Prompt Pack v8, prompt #088), context C7 Voice & Audio.

``TtsJobRunner`` voices one ``Script`` version with one ``VoiceProfile``
through the ``SpeechSynthesizer`` provider (#086) as a resumable ``AIJob``
(#027). The rules were approved by the user on 2026-10-05:

- Chunking: one chunk per ``ScriptSection``. A section over 5,000 characters
  (``MAX_TEXT``) is split at sentence ends, whole sentences packed greedily up
  to the limit; a sentence that is still too long is split at whitespace, and
  a token without whitespace over the limit is cut every 5,000 characters.
  Chunks are stripped, blank ones dropped, and numbered from 0 across the
  sections. The split is deterministic and keeps the text between the cut
  points as written.
- The job is an ``AIJob`` of kind ``audio.generate`` with the idempotency key
  ``audio_key(script id, voice profile id, voice profile version)``, stored by
  ``AIJobRepository`` (no migration). A later run with the same script and
  voice resumes the same job: a failed attempt is started again (attempts + 1)
  and every chunk already finished is reused.
- Chunk audio goes to a ``ChunkStore`` (``put`` may overwrite, ``get`` gives
  None when missing); ``InMemoryChunkStore`` is the only one for now. A chunk
  is stored first, then its ``audio.chunk_done`` checkpoint is saved. A chunk
  is reused only when its newest checkpoint, its stored bytes (same sha256)
  and its text (same sha256) all agree; bytes without a checkpoint are never
  adopted, they are synthesized again and overwritten.
- Checkpoints hold scalar data only, never script text or audio:
  ``audio.planned`` (once: script and voice ids, chunk and character counts;
  a resumed job must have the same plan), ``audio.chunk_done`` per chunk
  (index, section index, characters, text sha256, audio sha256, provider
  duration, media type and WAV format; the newest per index wins) and
  ``audio.completed`` (chunks, characters, duration, merged sha256, sample
  rate), saved in the same update as ``succeed``. ``last_error`` holds an
  error code only.
- The result is one WAV file: the PCM frames of every chunk in index order.
  The provider must answer ``audio/wav`` PCM, and every chunk must share the
  first chunk's sample rate, channels and sample width. The duration is the
  sum of the provider-reported chunk durations.
- A succeeded job is rebuilt from its checkpoints and the store, with no
  provider call and no write; missing or changed chunk bytes raise
  ``domain.tts_result_unavailable``. A cancelled job is refused. A running
  job is refused unless ``take_over=True``, which saves the stale attempt as
  failed (``interrupted``) and starts a new one. A waiting job is always
  refused: an ``AIJob`` cannot fail from waiting.
- The voice profile's provider must be the synthesizer's name, the content
  item must exist and belong to the voice profile's channel; these are checked
  before anything is stored.
- Retries (#091): a ``SpeechSynthesisError`` with ``retryable`` true is tried
  again, up to ``MAX_ATTEMPTS`` (3) calls per chunk, waiting 1 s and then 2 s
  (``BACKOFF_SECONDS``) through an injectable ``sleep``. A non-retryable
  provider error, a ``TtsJobError`` (the answer was validated after the call)
  and a ``ConcurrencyError`` are never retried. When the last try still fails
  with a retryable error, that error fails the attempt with its code, as
  below. A retry never changes ``AIJob.attempts``.
- No duplication: one provider call is in flight per chunk at a time; a retry
  starts only after the call failed, so no audio exists to duplicate; a chunk
  is stored and checkpointed only after a validated success; finished chunks
  are skipped on resume. The extra calls are bounded by ``max_attempts - 1``
  per failing chunk and are visible in ``TtsResult.provider_calls`` and
  ``TtsResult.retries`` (both 0 for a rebuilt result).
- A provider error, or a ``TtsJobError`` once the attempt has started, saves
  the attempt as failed with the error code and is raised again. Any other
  error (such as a ``ConcurrencyError``) leaves the job as it is. No
  transaction is held open across a provider call. No audit events are
  written.

Out of scope: audio storage, the audio ``Artifact`` and ``AudioMetadata``
(#089, ``content/audio_artifact.py``), audio validation (#090) and TTS cost
(#092).
"""

import dataclasses
import hashlib
import io
import re
import struct
import threading
import time
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple, Protocol, runtime_checkable

from ai_youtube_agent.content.paid_api_budget import PaidRequestDecisionError
from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.voice import VoiceProfile
from ai_youtube_agent.core.audit import MetadataValue
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.idempotency import AUDIO_KIND, audio_key
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus, JobCheckpoint
from ai_youtube_agent.providers.speech_synthesis import (
    MAX_TEXT,
    SpeechRequest,
    SpeechSynthesisError,
    SpeechSynthesizer,
    SynthesizedSpeech,
)

MAX_CHUNK = MAX_TEXT
JOB_KIND = AUDIO_KIND
MEDIA_TYPE = "audio/wav"
PLANNED = "audio.planned"
CHUNK_DONE = "audio.chunk_done"
COMPLETED = "audio.completed"
INTERRUPTED = "interrupted"
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0)
Clock = Callable[[], datetime]

# Sentence ends first, then any whitespace; a token without whitespace is cut.
_SEPARATORS = (re.compile(r"(?<=[.!?…。！？])\s+"), re.compile(r"\s+"))


class TtsErrorCode(StrEnum):
    JOB = "domain.tts_job"
    PROVIDER_MISMATCH = "domain.tts_provider_mismatch"
    CHANNEL_MISMATCH = "domain.tts_channel_mismatch"
    ITEM_NOT_FOUND = "domain.tts_item_not_found"
    JOB_IN_PROGRESS = "domain.tts_job_in_progress"
    JOB_CANCELLED = "domain.tts_job_cancelled"
    UNSUPPORTED_FORMAT = "domain.tts_unsupported_format"
    INVALID_AUDIO = "domain.tts_invalid_audio"
    FORMAT_MISMATCH = "domain.tts_format_mismatch"
    RESULT_UNAVAILABLE = "domain.tts_result_unavailable"
    PROVIDER_RESPONSE = "domain.tts_provider_response"


USER_MESSAGES = {
    TtsErrorCode.JOB: "The audio could not be generated.",
    TtsErrorCode.PROVIDER_MISMATCH: "The voice profile uses another voice service.",
    TtsErrorCode.CHANNEL_MISMATCH: "The voice profile belongs to another channel.",
    TtsErrorCode.ITEM_NOT_FOUND: "This content item does not exist.",
    TtsErrorCode.JOB_IN_PROGRESS: "The audio for this script is already being made.",
    TtsErrorCode.JOB_CANCELLED: "The audio job for this script was cancelled.",
    TtsErrorCode.UNSUPPORTED_FORMAT: "The voice service answered in an "
    "unsupported audio format.",
    TtsErrorCode.INVALID_AUDIO: "The voice service answered with unreadable audio.",
    TtsErrorCode.FORMAT_MISMATCH: "The voice service answered in different "
    "audio formats.",
    TtsErrorCode.RESULT_UNAVAILABLE: "The generated audio is no longer available.",
    TtsErrorCode.PROVIDER_RESPONSE: "The voice service answered for another voice.",
}


class TtsJobError(DomainError):
    """A TTS job was refused or failed; ``code`` is a ``TtsErrorCode``."""

    default_code = TtsErrorCode.JOB.value
    default_user_message = USER_MESSAGES[TtsErrorCode.JOB]

    def __init__(
        self, detail: str = "", *, code: TtsErrorCode = TtsErrorCode.JOB
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])


@dataclass(frozen=True)
class TextChunk:
    index: int
    section_index: int
    text: str


def split_text(text: str, limit: int = MAX_CHUNK) -> tuple[str, ...]:
    """``text`` in stripped, non-blank pieces of 1 to ``limit`` characters."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a whole number of 1 or more")
    spans = _spans(text, 0, len(text), 0, limit)
    pieces = (text[start:end].strip() for start, end in spans)
    return tuple(piece for piece in pieces if piece)


def _spans(
    text: str, start: int, end: int, level: int, limit: int
) -> list[tuple[int, int]]:
    """Spans of ``text[start:end]``, each at most ``limit`` long, packed greedily."""
    if end - start <= limit:
        return [(start, end)]
    if level == len(_SEPARATORS):
        return [(at, min(at + limit, end)) for at in range(start, end, limit)]
    parts = []
    position = start
    for match in _SEPARATORS[level].finditer(text, start, end):
        parts.append((position, match.start()))
        position = match.end()
    parts.append((position, end))
    packed: list[tuple[int, int]] = []
    for part_start, part_end in parts:
        if part_start == part_end:
            continue
        for span_start, span_end in _spans(
            text, part_start, part_end, level + 1, limit
        ):
            if packed and span_end - packed[-1][0] <= limit:
                packed[-1] = (packed[-1][0], span_end)
            else:
                packed.append((span_start, span_end))
    return packed


def plan_chunks(script: Script) -> tuple[TextChunk, ...]:
    """The chunks of ``script``: one or more per section, numbered from 0."""
    chunks: list[TextChunk] = []
    for section_index, section in enumerate(script.sections):
        for text in split_text(section.text):
            chunks.append(TextChunk(len(chunks), section_index, text))
    return tuple(chunks)


@runtime_checkable
class ChunkStore(Protocol):
    """Where chunk audio waits until the job merges it."""

    def put(self, job_id: str, index: int, audio: bytes) -> None: ...

    def get(self, job_id: str, index: int) -> bytes | None: ...


class InMemoryChunkStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: dict[tuple[str, int], bytes] = {}

    def put(self, job_id: str, index: int, audio: bytes) -> None:
        with self._lock:
            self._chunks[(job_id, index)] = bytes(audio)

    def get(self, job_id: str, index: int) -> bytes | None:
        with self._lock:
            return self._chunks.get((job_id, index))

    def clear(self, job_id: str) -> None:
        with self._lock:
            for key in [key for key in self._chunks if key[0] == job_id]:
                del self._chunks[key]


@dataclass(frozen=True)
class TtsResult:
    job_id: str = ""
    audio: bytes = b""
    media_type: str = "audio/wav"
    duration_ms: int = 0
    characters: int = 0
    chunks: int = 0
    sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2
    attempts: int = 1
    reused_chunks: int = 0
    synthesized_chunks: int = 0
    provider_calls: int = 0
    retries: int = 0
    provider: str | None = None
    voice_id: str | None = None
    job: AIJob | None = None

    def __post_init__(self) -> None:
        if self.job is not None and not self.job_id:
            object.__setattr__(self, "job_id", self.job.id)


class WavFormat(NamedTuple):
    sample_rate: int
    channels: int
    sample_width: int


class _Attempt:
    """The job of one run; every save is its own guarded transaction."""

    def __init__(self, database: Database, job: AIJob) -> None:
        self._database = database
        self.job = job
        self.provider_calls = 0
        self.retries = 0

    def save(self, job: AIJob) -> None:
        with self._database.transaction() as connection:
            AIJobRepository(connection).update(
                job, expected_updated_at=self.job.updated_at
            )
        self.job = job


class TtsJobRunner:
    def __init__(
        self,
        database: Database,
        synthesizer: SpeechSynthesizer,
        store: ChunkStore,
        *,
        clock: Clock | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff: Sequence[float] = BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a whole number of 1 or more")
        waits = tuple(backoff)
        if len(waits) < max_attempts - 1:
            raise ValueError("backoff needs a wait for every retry")
        if any(wait < 0 for wait in waits):
            raise ValueError("backoff waits cannot be negative")
        self._database = database
        self._synthesizer = synthesizer
        self._store = store
        self._clock = clock
        self._max_attempts = max_attempts
        self._backoff = waits
        self._sleep = sleep

    def run(
        self,
        script: Script,
        voice: VoiceProfile,
        *,
        session_id: str | None = None,
        take_over: bool = False,
    ) -> TtsResult:
        if voice.provider != self._synthesizer.name:
            raise TtsJobError(
                f"voice profile {voice.id} uses provider {voice.provider}, "
                f"not {self._synthesizer.name}",
                code=TtsErrorCode.PROVIDER_MISMATCH,
            )
        plan = plan_chunks(script)
        job = self._job(script, voice, session_id)
        if job.status is AIJobStatus.SUCCEEDED:
            return self._rebuild(job, plan)
        if job.status is AIJobStatus.CANCELLED:
            raise TtsJobError(
                f"job {job.id} was cancelled", code=TtsErrorCode.JOB_CANCELLED
            )
        if job.status is AIJobStatus.WAITING or (
            job.status is AIJobStatus.RUNNING and not take_over
        ):
            raise TtsJobError(
                f"job {job.id} is {job.status.value}", code=TtsErrorCode.JOB_IN_PROGRESS
            )
        attempt = _Attempt(self._database, job)
        if job.status is AIJobStatus.RUNNING:
            attempt.save(job.fail(INTERRUPTED, clock=self._after(job)))
        attempt.save(attempt.job.start(clock=self._after(attempt.job)))
        try:
            return self._produce(attempt, plan, script, voice)
        except (
            SpeechSynthesisError,
            TtsJobError,
            # A paid voice stopped by the budget policy (BUDGET-01): the attempt
            # fails with the budget code; a re-run needs a new approval.
            PaidRequestDecisionError,
        ) as error:
            attempt.save(attempt.job.fail(error.code, clock=self._after(attempt.job)))
            raise

    def stored_chunks(self, script: Script, voice: VoiceProfile) -> frozenset[int]:
        """The indexes of the chunks of ``script`` a run reuses from the store
        instead of calling the provider again; read only (no job is created)."""
        key = audio_key(script.id, voice.id, voice.version)
        with self._database.transaction() as connection:
            job = AIJobRepository(connection).get_by_idempotency_key(key)
        if job is None:
            return frozenset()
        return frozenset(
            chunk.index
            for chunk in plan_chunks(script)
            if self._stored(job, chunk, None) is not None
        )

    def _job(
        self, script: Script, voice: VoiceProfile, session_id: str | None
    ) -> AIJob:
        key = audio_key(script.id, voice.id, voice.version)
        with self._database.transaction() as connection:
            item = ContentItemRepository(connection).get(script.content_item_id)
            if item is None:
                raise TtsJobError(
                    f"content item {script.content_item_id} does not exist",
                    code=TtsErrorCode.ITEM_NOT_FOUND,
                )
            if item.channel_id != voice.channel_id:
                raise TtsJobError(
                    f"voice profile {voice.id} belongs to another channel than "
                    f"content item {item.id}",
                    code=TtsErrorCode.CHANNEL_MISMATCH,
                )
            jobs = AIJobRepository(connection)
            job = jobs.get_by_idempotency_key(key)
            if job is None:
                job = AIJob.create(
                    JOB_KIND,
                    key,
                    content_item_id=item.id,
                    session_id=session_id,
                    clock=self._clock,
                )
                jobs.add(job)
        return job

    def _produce(
        self,
        attempt: _Attempt,
        plan: tuple[TextChunk, ...],
        script: Script,
        voice: VoiceProfile,
    ) -> TtsResult:
        self._check_plan(attempt, plan, script, voice)
        wav_format: WavFormat | None = None
        reused = synthesized = 0
        for chunk in plan:
            stored = self._stored(attempt.job, chunk, wav_format)
            if stored is None:
                stored = self._synthesize(attempt, chunk, voice, wav_format)
                synthesized += 1
            else:
                reused += 1
            wav_format = wav_format or stored[1]
        parts = []
        for chunk in plan:
            # A chunk lost from the store since the loop above is made again.
            stored = self._stored(attempt.job, chunk, wav_format)
            if stored is None:
                stored = self._synthesize(attempt, chunk, voice, wav_format)
                synthesized += 1
            parts.append(stored[0])
        if wav_format is None:  # a script has at least one non-blank section
            raise AssertionError("no chunk was planned")
        merged = _merge(parts, wav_format)
        result = dataclasses.replace(
            _result(attempt.job, merged, plan, wav_format, reused, synthesized),
            provider_calls=attempt.provider_calls,
            retries=attempt.retries,
        )
        completed = attempt.job.checkpoint(
            COMPLETED,
            {
                "chunks": result.chunks,
                "characters": result.characters,
                "duration_ms": result.duration_ms,
                "checksum": _sha256(merged),
                "sample_rate": wav_format.sample_rate,
            },
            clock=self._after(attempt.job),
        )
        attempt.save(completed.succeed(clock=self._after(completed)))
        return result

    def _check_plan(
        self,
        attempt: _Attempt,
        plan: tuple[TextChunk, ...],
        script: Script,
        voice: VoiceProfile,
    ) -> None:
        expected = {
            "script_id": script.id,
            "voice_profile_id": voice.id,
            "voice_profile_version": voice.version,
            "chunks": len(plan),
            "characters": _characters(plan),
        }
        planned = _latest(attempt.job, PLANNED)
        if planned is None:
            job = attempt.job
            attempt.save(job.checkpoint(PLANNED, expected, clock=self._after(job)))
        elif dict(planned.data) != expected:
            raise TtsJobError(
                f"job {attempt.job.id} was planned differently; the chunking changed"
            )

    def _stored(
        self, job: AIJob, chunk: TextChunk, wav_format: WavFormat | None
    ) -> tuple[bytes, WavFormat] | None:
        """The stored audio of ``chunk`` when its newest checkpoint vouches for it."""
        record = _chunks_done(job).get(chunk.index)
        if record is None or record.get("text_sha256") != _sha256(
            chunk.text.encode("utf-8")
        ):
            return None
        audio = self._store.get(job.id, chunk.index)
        if audio is None or _sha256(audio) != record.get("checksum"):
            return None
        stored_format = WavFormat(
            record["sample_rate"], record["channels"], record["sample_width"]
        )
        if wav_format is not None and stored_format != wav_format:
            return None
        return audio, stored_format

    def _synthesize(
        self,
        attempt: _Attempt,
        chunk: TextChunk,
        voice: VoiceProfile,
        wav_format: WavFormat | None,
    ) -> tuple[bytes, WavFormat]:
        speech = self._call(attempt, voice.speech_request(chunk.text))
        if speech.provider != self._synthesizer.name or speech.voice_id != (
            voice.voice_id
        ):
            raise TtsJobError(
                f"chunk {chunk.index}: the answer names another provider or voice",
                code=TtsErrorCode.PROVIDER_RESPONSE,
            )
        if speech.media_type != MEDIA_TYPE:
            raise TtsJobError(
                f"chunk {chunk.index}: media type {speech.media_type} is not "
                f"{MEDIA_TYPE}",
                code=TtsErrorCode.UNSUPPORTED_FORMAT,
            )
        chunk_format, _ = _read_wav(speech.audio, chunk.index)
        if wav_format is not None and chunk_format != wav_format:
            raise TtsJobError(
                f"chunk {chunk.index}: format {tuple(chunk_format)} differs from "
                f"{tuple(wav_format)}",
                code=TtsErrorCode.FORMAT_MISMATCH,
            )
        self._store.put(attempt.job.id, chunk.index, speech.audio)
        job = attempt.job
        attempt.save(
            job.checkpoint(
                CHUNK_DONE,
                {
                    "index": chunk.index,
                    "section_index": chunk.section_index,
                    "characters": len(chunk.text),
                    "text_sha256": _sha256(chunk.text.encode("utf-8")),
                    "checksum": _sha256(speech.audio),
                    "duration_ms": speech.duration_ms,
                    "media_type": speech.media_type,
                    "sample_rate": chunk_format.sample_rate,
                    "channels": chunk_format.channels,
                    "sample_width": chunk_format.sample_width,
                },
                clock=self._after(job),
            )
        )
        return speech.audio, chunk_format

    def _call(self, attempt: _Attempt, request: SpeechRequest) -> SynthesizedSpeech:
        """One chunk's provider call, tried again after a retryable error."""
        for number in range(1, self._max_attempts + 1):
            attempt.provider_calls += 1
            try:
                return self._synthesizer.synthesize(request)
            except SpeechSynthesisError as error:
                if not error.retryable or number == self._max_attempts:
                    raise
                attempt.retries += 1
                self._sleep(self._backoff[number - 1])
        raise AssertionError("max_attempts is at least 1")

    def _rebuild(self, job: AIJob, plan: tuple[TextChunk, ...]) -> TtsResult:
        completed = _latest(job, COMPLETED)
        wav_format: WavFormat | None = None
        parts = []
        for chunk in plan:
            stored = self._stored(job, chunk, wav_format)
            if stored is None:
                raise TtsJobError(
                    f"chunk {chunk.index} of job {job.id} is no longer stored",
                    code=TtsErrorCode.RESULT_UNAVAILABLE,
                )
            parts.append(stored[0])
            wav_format = wav_format or stored[1]
        if completed is None or wav_format is None:
            raise TtsJobError(
                f"job {job.id} has no completed audio",
                code=TtsErrorCode.RESULT_UNAVAILABLE,
            )
        merged = _merge(parts, wav_format)
        if _sha256(merged) != completed.data.get("checksum"):
            raise TtsJobError(
                f"the merged audio of job {job.id} does not match its checksum",
                code=TtsErrorCode.RESULT_UNAVAILABLE,
            )
        return _result(job, merged, plan, wav_format, len(plan), 0)

    def _after(self, job: AIJob) -> Clock:
        """A clock that never goes back before the job's last change."""
        now = self._clock() if self._clock else datetime.now(UTC)
        moment = max(now, job.updated_at)
        return lambda: moment


def _result(
    job: AIJob,
    merged: bytes,
    plan: tuple[TextChunk, ...],
    wav_format: WavFormat,
    reused: int,
    synthesized: int,
) -> TtsResult:
    done = _chunks_done(job)
    return TtsResult(
        job_id=job.id,
        audio=merged,
        media_type=MEDIA_TYPE,
        duration_ms=sum(int(done[chunk.index]["duration_ms"]) for chunk in plan),
        characters=_characters(plan),
        chunks=len(plan),
        sample_rate=wav_format.sample_rate,
        channels=wav_format.channels,
        sample_width=wav_format.sample_width,
        attempts=job.attempts,
        reused_chunks=reused,
        synthesized_chunks=synthesized,
    )


def _read_wav(audio: bytes, index: int) -> tuple[WavFormat, bytes]:
    """The format and PCM frames of a WAV file, or ``domain.tts_invalid_audio``."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            wav_format = WavFormat(
                reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
            )
            count = reader.getnframes()
            frames = reader.readframes(count)
    except (wave.Error, EOFError, struct.error, ValueError) as error:
        raise TtsJobError(
            f"chunk {index}: the audio is not a PCM WAV file",
            code=TtsErrorCode.INVALID_AUDIO,
        ) from error
    if (
        wav_format.sample_rate < 1
        or count < 1
        or len(frames) != count * wav_format.channels * wav_format.sample_width
    ):
        raise TtsJobError(
            f"chunk {index}: the WAV file holds no complete audio",
            code=TtsErrorCode.INVALID_AUDIO,
        )
    return wav_format, frames


def _merge(parts: list[bytes], wav_format: WavFormat) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(wav_format.channels)
        writer.setsampwidth(wav_format.sample_width)
        writer.setframerate(wav_format.sample_rate)
        for index, audio in enumerate(parts):
            writer.writeframesraw(_read_wav(audio, index)[1])
    return buffer.getvalue()


def _chunks_done(job: AIJob) -> dict[int, Mapping[str, MetadataValue]]:
    """The newest ``audio.chunk_done`` data of each chunk index."""
    done: dict[int, Mapping[str, MetadataValue]] = {}
    for saved in job.checkpoints:
        if saved.step == CHUNK_DONE:
            done[int(saved.data["index"])] = saved.data
    return done


def chunk_durations(job: AIJob) -> tuple[int, ...] | None:
    """The ``duration_ms`` of each chunk of a succeeded job, in chunk order.

    None when the job has not succeeded or its checkpoints do not cover the
    chunks 0..n-1 (the shorts planner then estimates the timing instead).
    """
    if job.status is not AIJobStatus.SUCCEEDED:
        return None
    done = _chunks_done(job)
    if not done or sorted(done) != list(range(len(done))):
        return None
    return tuple(int(done[index]["duration_ms"]) for index in range(len(done)))


def _latest(job: AIJob, step: str) -> JobCheckpoint | None:
    for saved in reversed(job.checkpoints):
        if saved.step == step:
            return saved
    return None


def _characters(plan: tuple[TextChunk, ...]) -> int:
    return sum(len(chunk.text) for chunk in plan)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
