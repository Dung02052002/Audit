"""H-091 Audio Retry (Prompt Pack v8, prompt #091).

Retryable provider failures are tried again inside the TTS job, up to three
calls per chunk with waits of 1 s and 2 s, without duplicating audio or
checkpoints. Non-retryable errors, validation errors and a spent budget fail
the attempt as before. The waits go through an injected ``sleep``, so no test
really waits.
"""

import pytest

from ai_youtube_agent.content.tts_job import (
    BACKOFF_SECONDS,
    CHUNK_DONE,
    COMPLETED,
    MAX_ATTEMPTS,
    PLANNED,
    TtsErrorCode,
    TtsJobError,
    TtsJobRunner,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechSynthesisError,
    VoiceErrorCode,
)
from test_tts_job import THREE, Flaky, World, chunk_records, steps


class Sleeps:
    """A fake ``sleep`` that records the waits."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.values.append(seconds)


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


def runner(world: World, sleeps: Sleeps, synthesizer=None, **options) -> TtsJobRunner:
    return world.runner(synthesizer, sleep=sleeps, **options)


# Defaults


def test_the_default_policy_is_three_calls_with_one_and_two_second_waits() -> None:
    assert MAX_ATTEMPTS == 3
    assert BACKOFF_SECONDS == (1.0, 2.0)


# Transient failures


def test_a_transient_failure_is_retried_until_it_succeeds(world: World) -> None:
    script = world.script("One.")
    world.mock.fail_next(VoiceErrorCode.TIMEOUT, times=2)
    sleeps = Sleeps()

    result = runner(world, sleeps).run(script, world.voice)

    assert sleeps.values == [1.0, 2.0]
    assert (result.provider_calls, result.retries) == (3, 2)
    assert result.attempts == 1
    assert (result.chunks, result.synthesized_chunks) == (1, 1)
    job = world.job(script)
    assert job.status is AIJobStatus.SUCCEEDED and job.attempts == 1
    assert steps(job) == [PLANNED, CHUNK_DONE, COMPLETED]
    assert len(chunk_records(job)) == 1


@pytest.mark.parametrize(
    "code",
    [
        VoiceErrorCode.UNAVAILABLE,
        VoiceErrorCode.RATE_LIMITED,
        VoiceErrorCode.TIMEOUT,
    ],
    ids=lambda c: c.value,
)
def test_each_retryable_code_is_retried(world: World, code: VoiceErrorCode) -> None:
    script = world.script("One.")
    world.mock.fail_next(code)
    sleeps = Sleeps()

    result = runner(world, sleeps).run(script, world.voice)

    assert sleeps.values == [1.0]
    assert (result.provider_calls, result.retries) == (2, 1)


def test_a_clean_run_makes_one_call_per_chunk_and_no_retry(world: World) -> None:
    sleeps = Sleeps()

    result = runner(world, sleeps).run(world.script(*THREE), world.voice)

    assert (result.provider_calls, result.retries) == (3, 0)
    assert result.provider_calls == result.chunks
    assert sleeps.values == []


# Exhausted and non-retryable failures


def test_an_exhausted_retry_fails_the_attempt_with_the_last_code(
    world: World,
) -> None:
    script = world.script("One.")
    world.mock.fail_next(VoiceErrorCode.TIMEOUT, times=3)
    sleeps = Sleeps()

    with pytest.raises(SpeechSynthesisError) as caught:
        runner(world, sleeps).run(script, world.voice)

    assert caught.value.voice_code is VoiceErrorCode.TIMEOUT
    assert sleeps.values == [1.0, 2.0]
    assert len(world.mock.calls) == 3
    job = world.job(script)
    assert job.status is AIJobStatus.FAILED and job.attempts == 1
    assert job.last_error == "voice.timeout"
    assert chunk_records(job) == []
    assert world.store.get(job.id, 0) is None


def test_a_non_retryable_error_is_not_retried(world: World) -> None:
    script = world.script("One.")
    world.mock.fail_next(VoiceErrorCode.REFUSED)
    sleeps = Sleeps()

    with pytest.raises(SpeechSynthesisError) as caught:
        runner(world, sleeps).run(script, world.voice)

    assert caught.value.voice_code is VoiceErrorCode.REFUSED
    assert len(world.mock.calls) == 1
    assert sleeps.values == []
    job = world.job(script)
    assert job.status is AIJobStatus.FAILED
    assert job.last_error == "voice.refused"


def test_a_validation_error_is_not_retried(world: World) -> None:
    script = world.script("One.")
    world.mock.queue(b"ID3\x03fake-mp3", duration_ms=10, media_type="audio/mpeg")
    sleeps = Sleeps()

    with pytest.raises(TtsJobError) as caught:
        runner(world, sleeps).run(script, world.voice)

    assert caught.value.code == TtsErrorCode.UNSUPPORTED_FORMAT
    assert len(world.mock.calls) == 1
    assert sleeps.values == []
    assert world.job(script).status is AIJobStatus.FAILED


def test_one_attempt_means_no_retry_and_no_wait(world: World) -> None:
    script = world.script("One.")
    world.mock.fail_next(VoiceErrorCode.TIMEOUT)
    sleeps = Sleeps()

    with pytest.raises(SpeechSynthesisError):
        runner(world, sleeps, max_attempts=1).run(script, world.voice)

    assert len(world.mock.calls) == 1
    assert sleeps.values == []
    assert world.job(script).last_error == "voice.timeout"


def test_a_custom_budget_and_backoff_are_used(world: World) -> None:
    script = world.script("One.")
    world.mock.fail_next(VoiceErrorCode.UNAVAILABLE, times=2)
    sleeps = Sleeps()

    with pytest.raises(SpeechSynthesisError):
        runner(world, sleeps, max_attempts=2, backoff=(0.5,)).run(script, world.voice)

    assert sleeps.values == [0.5]
    assert len(world.mock.calls) == 2


# No duplication


def test_a_retry_does_not_duplicate_other_chunks_or_checkpoints(
    world: World,
) -> None:
    script = world.script(*THREE)
    # Call 2 is the first try of the second chunk; the third call retries it.
    flaky = Flaky(world.mock, {2}, VoiceErrorCode.TIMEOUT)
    sleeps = Sleeps()

    result = runner(world, sleeps, flaky).run(script, world.voice)

    assert [call.text for call in world.mock.calls] == list(THREE)
    assert flaky.count == 4
    assert (result.provider_calls, result.retries) == (4, 1)
    assert (result.synthesized_chunks, result.reused_chunks) == (3, 0)
    assert sleeps.values == [1.0]
    job = world.job(script)
    assert steps(job) == [PLANNED] + [CHUNK_DONE] * 3 + [COMPLETED]
    assert [record["index"] for record in chunk_records(job)] == [0, 1, 2]


def test_a_resume_after_exhaustion_calls_only_the_missing_chunk(
    world: World,
) -> None:
    script = world.script(*THREE)
    flaky = Flaky(world.mock, {3, 4, 5}, VoiceErrorCode.TIMEOUT)
    sleeps = Sleeps()
    with pytest.raises(SpeechSynthesisError):
        runner(world, sleeps, flaky).run(script, world.voice)
    assert flaky.count == 5
    assert [record["index"] for record in chunk_records(world.job(script))] == [0, 1]
    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)

    result = runner(world, sleeps, fresh).run(script, world.voice)

    assert [call.text for call in fresh.calls] == [THREE[2]]
    assert (result.attempts, result.reused_chunks, result.synthesized_chunks) == (
        2,
        2,
        1,
    )
    assert (result.provider_calls, result.retries) == (1, 0)
    assert world.job(script).status is AIJobStatus.SUCCEEDED


def test_a_rebuilt_result_reports_no_calls_and_no_retries(world: World) -> None:
    script = world.script(*THREE)
    sleeps = Sleeps()
    runner(world, sleeps).run(script, world.voice)

    again = runner(world, sleeps).run(script, world.voice)

    assert (again.provider_calls, again.retries) == (0, 0)
    assert len(world.mock.calls) == 3


# Constructor


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"max_attempts": True},
        {"max_attempts": 2.5},
        {"max_attempts": 3, "backoff": (1.0,)},
        {"max_attempts": 2, "backoff": ()},
        {"max_attempts": 2, "backoff": (-0.1,)},
        {"max_attempts": 3, "backoff": (1.0, -2.0)},
    ],
    ids=[
        "zero",
        "negative",
        "bool",
        "float",
        "short-backoff",
        "empty-backoff",
        "negative-wait",
        "negative-second-wait",
    ],
)
def test_the_constructor_refuses_a_bad_policy(world: World, options: dict) -> None:
    with pytest.raises(ValueError):
        TtsJobRunner(world.database, world.mock, world.store, **options)


def test_a_single_attempt_needs_no_backoff(world: World) -> None:
    TtsJobRunner(world.database, world.mock, world.store, max_attempts=1, backoff=())
