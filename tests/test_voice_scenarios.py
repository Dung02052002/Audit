"""H-093 Voice Tests (Prompt Pack v8, prompt #093).

Scenario tests over the real voice services: the mock provider, the TTS job,
the audio artifact recorder and the audio validator, on the migrated
``database`` fixture, ``LocalFileStorage(tmp_path)``, a recording ``sleep`` and
pinned clocks. Unit rules of each module live in their own test files; these
tests follow one request through the whole chain.
"""

from pathlib import Path

import pytest

from ai_youtube_agent.content.audio_artifact import AudioArtifactRecord
from ai_youtube_agent.content.audio_validation import (
    AudioValidationResult,
    AudioValidator,
)
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.script import Script
from ai_youtube_agent.content.tts_job import (
    TtsErrorCode,
    TtsJobError,
    TtsJobRunner,
    TtsResult,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.pipeline.idempotency import audio_key
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechSynthesisError,
    VoiceErrorCode,
)
from factories import at, make_voice_profile
from test_audio_artifact import T1, TEXTS, World
from test_audio_validation import make_wav, quiet, sine, square

CHECK_NAMES = [
    "audio.integrity",
    "audio.format",
    "audio.duration",
    "audio.silence",
    "audio.level",
]
RETRYABLE = [
    VoiceErrorCode.UNAVAILABLE,
    VoiceErrorCode.RATE_LIMITED,
    VoiceErrorCode.TIMEOUT,
]
NOT_RETRYABLE = [
    VoiceErrorCode.INVALID_REQUEST,
    VoiceErrorCode.VOICE_NOT_FOUND,
    VoiceErrorCode.REFUSED,
]


class Sleeps:
    """A fake ``sleep`` that records the waits."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.values.append(seconds)


class Outage:
    """The mock behind one text that is down: calls for it fail as unavailable."""

    def __init__(self, inner: MockSpeechSynthesizer, text: str) -> None:
        self.name = inner.name
        self.inner = inner
        self.text = text
        self.down = True
        self.failed_calls = 0

    def synthesize(self, request):
        if self.down and request.text == self.text:
            self.failed_calls += 1
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE, "scripted outage", provider=self.name
            )
        return self.inner.synthesize(request)

    def check(self) -> None:
        self.inner.check()


@pytest.fixture
def world(database: Database, tmp_path: Path) -> World:
    return World(database, tmp_path)


def runner(world: World, sleeps: Sleeps, synthesizer=None) -> TtsJobRunner:
    return TtsJobRunner(
        world.database,
        synthesizer or world.mock,
        world.store,
        clock=world.clock,
        sleep=sleeps,
    )


def validator(world: World) -> AudioValidator:
    return AudioValidator(world.database, world.storage, world.audit, clock=at(T1))


def job_of(world: World, script: Script) -> AIJob:
    key = audio_key(script.id, world.voice.id, world.voice.version)
    with world.database.transaction() as connection:
        job = AIJobRepository(connection).get_by_idempotency_key(key)
    assert job is not None
    return job


def latest_audio(world: World):
    with world.database.transaction() as connection:
        return ArtifactRepository(connection).latest(world.item.id, ArtifactKind.AUDIO)


def voice_it(world: World, script: Script, sleeps: Sleeps | None = None) -> TtsResult:
    return runner(world, sleeps or Sleeps()).run(script, world.voice)


def whole_chain(
    world: World, script: Script
) -> tuple[TtsResult, AudioArtifactRecord, AudioValidationResult]:
    result = voice_it(world, script)
    record = world.recorder().record(result, script, world.voice)
    checked = validator(world).validate(record.artifact, record.audio)
    return result, record, checked


def statuses(checked: AudioValidationResult) -> dict[str, QCStatus]:
    return {check.name: check.status for check in checked.qc.checks}


def queued_chain(
    world: World, data: bytes, *, duration_ms: int, text: str = "One."
) -> AudioValidationResult:
    """One chunk answered by a queued WAV, taken through job, record and QC."""
    script = world.script(text)
    world.mock.queue(data, duration_ms=duration_ms)
    result = voice_it(world, script)
    record = world.recorder().record(result, script, world.voice)
    return validator(world).validate(record.artifact, record.audio)


# 1. Provider mock


def test_identical_requests_give_identical_audio_through_the_whole_chain(
    world: World,
) -> None:
    script = world.script()

    result, record, checked = whole_chain(world, script)

    twin = MockSpeechSynthesizer(name="mock_tts")
    for index, text in enumerate(TEXTS):
        expected = twin.synthesize(world.voice.speech_request(text)).audio
        assert world.store.get(result.job_id, index) == expected
    assert [call.text for call in world.mock.calls] == list(TEXTS)
    assert record.created is True and record.artifact.version == 1
    assert checked.qc.status is QCStatus.PASS
    assert [check.name for check in checked.qc.checks] == CHECK_NAMES
    assert all(status is QCStatus.PASS for status in statuses(checked).values())


def test_the_same_request_gives_the_same_bytes_on_every_call(world: World) -> None:
    request = world.voice.speech_request("Same words every time.")
    other = MockSpeechSynthesizer(name="mock_tts")

    first = world.mock.synthesize(request)
    second = world.mock.synthesize(request)

    assert first.audio == second.audio == other.synthesize(request).audio
    assert first.duration_ms == second.duration_ms


def test_a_different_voice_or_style_gives_different_audio(world: World) -> None:
    text = "Same words, other voice."
    base = world.mock.synthesize(world.voice.speech_request(text)).audio
    other_voice = make_voice_profile(world.channel, voice_id="vi-male-02")
    other_style = make_voice_profile(world.channel, speaking_style="calm")

    assert world.mock.synthesize(other_voice.speech_request(text)).audio != base
    assert world.mock.synthesize(other_style.speech_request(text)).audio != base


def test_an_unhealthy_mock_is_reported_by_check(world: World) -> None:
    world.mock.check()
    world.mock.set_healthy(False)

    with pytest.raises(SpeechSynthesisError) as error:
        world.mock.check()

    assert error.value.code == VoiceErrorCode.UNAVAILABLE.value
    assert error.value.retryable is True
    world.mock.set_healthy(True)
    world.mock.check()


def test_queued_answers_and_scripted_failures_are_consumed_in_order(
    world: World,
) -> None:
    first = make_wav(sine(1.0))
    second = make_wav(sine(1.0, freq=880))
    request = world.voice.speech_request("Hello there.")
    world.mock.fail_next(VoiceErrorCode.RATE_LIMITED)
    world.mock.queue(first.data, duration_ms=first.duration_ms)
    world.mock.queue(second.data, duration_ms=second.duration_ms)

    with pytest.raises(SpeechSynthesisError) as error:
        world.mock.synthesize(request)
    answers = [world.mock.synthesize(request).audio for _ in range(3)]

    assert error.value.code == VoiceErrorCode.RATE_LIMITED.value
    assert answers[0] == first.data
    assert answers[1] == second.data
    assert answers[2] not in (first.data, second.data)  # back to the default tone
    assert len(world.mock.calls) == 4


# 2. Invalid audio


@pytest.mark.parametrize(
    ("media_type", "audio", "code"),
    [
        ("audio/mpeg", b"ID3 not a wav", TtsErrorCode.UNSUPPORTED_FORMAT),
        ("audio/wav", b"this is not a wav file", TtsErrorCode.INVALID_AUDIO),
        ("audio/wav", make_wav([]).data, TtsErrorCode.INVALID_AUDIO),
        ("audio/wav", make_wav(sine(1.0)).data[:-501], TtsErrorCode.INVALID_AUDIO),
    ],
    ids=["media-type", "garbage", "no-frames", "truncated"],
)
def test_an_invalid_provider_answer_fails_the_job_and_stores_nothing(
    world: World, media_type: str, audio: bytes, code: TtsErrorCode
) -> None:
    script = world.script("One.")
    world.mock.queue(audio, duration_ms=1000, media_type=media_type)
    sleeps = Sleeps()

    with pytest.raises(TtsJobError) as error:
        voice_it(world, script, sleeps)

    assert error.value.code == code.value
    job = job_of(world, script)
    assert job.status is AIJobStatus.FAILED
    assert job.last_error == code.value
    assert world.store.get(job.id, 0) is None
    assert latest_audio(world) is None
    assert len(world.mock.calls) == 1  # a bad answer is not retried
    assert sleeps.values == []


def test_a_silent_answer_passes_the_job_but_fails_the_silence_check(
    world: World,
) -> None:
    clip = make_wav(quiet(2.0))

    checked = queued_chain(world, clip.data, duration_ms=clip.duration_ms)

    assert checked.qc.status is QCStatus.FAIL
    assert statuses(checked)["audio.silence"] is QCStatus.FAIL
    assert statuses(checked)["audio.integrity"] is QCStatus.PASS


def test_a_clipped_answer_fails_the_level_check(world: World) -> None:
    clip = make_wav(square(2.0))

    checked = queued_chain(world, clip.data, duration_ms=clip.duration_ms)

    assert checked.qc.status is QCStatus.FAIL
    assert statuses(checked)["audio.level"] is QCStatus.FAIL
    assert statuses(checked)["audio.silence"] is QCStatus.PASS


def test_a_file_changed_on_disk_after_recording_fails_the_integrity(
    world: World,
) -> None:
    script = world.script()
    _, record, _ = whole_chain(world, script)
    path = world.root.resolve() / world.item.id / "audio" / "v1.wav"
    path.write_bytes(make_wav(sine(1.0)).data)

    checked = validator(world).validate(record.artifact, record.audio)

    (check,) = checked.qc.checks
    assert (check.name, check.status) == ("audio.integrity", QCStatus.FAIL)
    assert checked.stats is None


def test_a_provider_duration_that_disagrees_with_the_audio_fails_the_duration(
    world: World,
) -> None:
    clip = make_wav(sine(2.0))

    checked = queued_chain(world, clip.data, duration_ms=5000)

    assert checked.qc.status is QCStatus.FAIL
    assert statuses(checked)["audio.duration"] is QCStatus.FAIL
    assert statuses(checked)["audio.integrity"] is QCStatus.PASS


# 3. Retry


@pytest.mark.parametrize("code", RETRYABLE, ids=lambda code: code.value)
def test_a_transient_error_then_success_makes_exactly_one_artifact(
    world: World, code: VoiceErrorCode
) -> None:
    script = world.script("One.")
    world.mock.fail_next(code, times=2)
    sleeps = Sleeps()

    result = voice_it(world, script, sleeps)
    first = world.recorder().record(result, script, world.voice)
    again = world.recorder().record(result, script, world.voice)

    assert sleeps.values == [1.0, 2.0]
    assert (result.provider_calls, result.retries, result.attempts) == (3, 2, 1)
    assert [call.text for call in world.mock.calls] == ["One."] * 3
    assert job_of(world, script).status is AIJobStatus.SUCCEEDED
    assert first.created is True and first.artifact.version == 1
    assert again.created is False and again.artifact.id == first.artifact.id
    assert again.invalidation is None
    assert latest_audio(world).version == 1
    assert len([path for path in world.root.rglob("*") if path.is_file()]) == 1


def test_exhausted_retries_fail_then_a_later_run_resumes_without_duplicates(
    world: World,
) -> None:
    script = world.script()
    outage = Outage(world.mock, TEXTS[1])
    sleeps = Sleeps()

    with pytest.raises(SpeechSynthesisError) as error:
        runner(world, sleeps, outage).run(script, world.voice)

    assert error.value.code == VoiceErrorCode.UNAVAILABLE.value
    assert outage.failed_calls == 3
    assert sleeps.values == [1.0, 2.0]
    job = job_of(world, script)
    assert job.status is AIJobStatus.FAILED and job.attempts == 1
    assert latest_audio(world) is None

    outage.down = False
    later = Sleeps()
    result = runner(world, later, outage).run(script, world.voice)
    record = world.recorder().record(result, script, world.voice)
    replay = world.recorder().record(result, script, world.voice)

    assert later.values == []
    assert (result.attempts, result.reused_chunks, result.synthesized_chunks) == (
        2,
        1,
        2,
    )
    assert (result.provider_calls, result.retries) == (2, 0)
    # The mock was reached once per chunk: no chunk was voiced twice.
    assert [call.text for call in world.mock.calls] == list(TEXTS)
    assert record.created is True and replay.created is False
    assert replay.artifact.id == record.artifact.id
    assert latest_audio(world).version == 1


@pytest.mark.parametrize("code", NOT_RETRYABLE, ids=lambda code: code.value)
def test_a_non_retryable_error_is_not_retried_and_a_later_run_recovers(
    world: World, code: VoiceErrorCode
) -> None:
    script = world.script("One.")
    world.mock.fail_next(code)
    sleeps = Sleeps()

    with pytest.raises(SpeechSynthesisError) as error:
        voice_it(world, script, sleeps)

    assert error.value.code == code.value
    assert error.value.retryable is False
    assert sleeps.values == []
    assert len(world.mock.calls) == 1
    assert job_of(world, script).status is AIJobStatus.FAILED
    assert latest_audio(world) is None

    result = voice_it(world, script, sleeps)

    assert (result.attempts, result.provider_calls, result.retries) == (2, 1, 0)
    assert sleeps.values == []
    assert world.recorder().record(result, script, world.voice).created is True
