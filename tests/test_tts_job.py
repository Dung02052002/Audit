"""H-088 TTS Job (Prompt Pack v8, prompt #088: resumable audio generation).

Rules the user approved on 2026-10-05:

- one chunk per script section; a section over 5,000 characters is split at
  sentence ends, then at whitespace, then cut; chunks numbered from 0;
- chunk audio goes to a ``ChunkStore``; the result is one merged PCM WAV;
- the job is an ``AIJob`` of kind ``audio.generate`` keyed by
  ``audio_key(script, voice, voice version)``; checkpoints hold scalars only;
- a failed job resumes and reuses finished chunks; a succeeded job is rebuilt
  without a provider call; a running job needs ``take_over``;
- the provider, channel and content item are checked before anything is
  stored; no audit events.
"""

import dataclasses
import io
import json
import wave
from datetime import timedelta

import pytest

from ai_youtube_agent.content.script import Script, ScriptSection, SectionKind
from ai_youtube_agent.content.tts_job import (
    CHUNK_DONE,
    COMPLETED,
    JOB_KIND,
    MAX_CHUNK,
    PLANNED,
    ChunkStore,
    InMemoryChunkStore,
    TextChunk,
    TtsErrorCode,
    TtsJobError,
    TtsJobRunner,
    plan_chunks,
    split_text,
)
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.pipeline.idempotency import AUDIO_PREFIX, audio_key
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.speech_synthesis import (
    MAX_TEXT,
    SpeechRequest,
    SpeechSynthesisError,
    SynthesizedSpeech,
    VoiceErrorCode,
)
from factories import (
    T0,
    make_channel,
    make_content_item,
    make_session,
    make_strategy_profile,
    make_voice_profile,
)

SECRET = "Zebra-quartz-7731"


class Ticker:
    """A clock that moves 1 ms forward on every call."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self):
        self.now += timedelta(milliseconds=1)
        return self.now


class Flaky:
    """Wraps a synthesizer and fails the given call numbers (1-based)."""

    def __init__(self, inner, fail_on, code=VoiceErrorCode.REFUSED) -> None:
        self.name = inner.name
        self.inner = inner
        self.fail_on = set(fail_on)
        self.code = code
        self.count = 0

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        self.count += 1
        if self.count in self.fail_on:
            raise SpeechSynthesisError(self.code, "scripted", provider=self.name)
        return self.inner.synthesize(request)

    def check(self) -> None:
        self.inner.check()


def wav(frames: int = 8, *, rate: int = 8000, channels: int = 1, width: int = 2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        size = frames * channels * width
        writer.writeframes((bytes(range(1, 9)) * (size // 8 + 1))[:size])
    return buffer.getvalue()


def read(audio: bytes) -> tuple[tuple[int, int, int], int, bytes]:
    with wave.open(io.BytesIO(audio), "rb") as reader:
        fmt = (reader.getframerate(), reader.getnchannels(), reader.getsampwidth())
        count = reader.getnframes()
        return fmt, count, reader.readframes(count)


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        self.item = make_content_item(self.channel, self.strategy)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            ContentItemRepository(connection).add(self.item)
        self.voice = make_voice_profile(self.channel)
        self.clock = Ticker()
        self.mock = MockSpeechSynthesizer(name="mock_tts", clock=self.clock)
        self.store = InMemoryChunkStore()

    def script(self, *texts: str) -> Script:
        sections = tuple(ScriptSection(SectionKind.BODY, text) for text in texts)
        return Script.create(self.item.id, sections=sections, clock=self.clock)

    def runner(self, synthesizer=None, store=None, **options) -> TtsJobRunner:
        options.setdefault("sleep", lambda seconds: None)  # never really wait
        return TtsJobRunner(
            self.database,
            synthesizer or self.mock,
            store or self.store,
            clock=self.clock,
            **options,
        )

    def job(self, script: Script) -> AIJob | None:
        with self.database.transaction() as connection:
            return AIJobRepository(connection).get_by_idempotency_key(
                audio_key(script.id, self.voice.id, self.voice.version)
            )

    def job_count(self) -> int:
        with self.database.transaction() as connection:
            (count,) = connection.execute("SELECT count(*) FROM ai_jobs").fetchone()
        return count

    def add_job(self, job: AIJob) -> None:
        with self.database.transaction() as connection:
            AIJobRepository(connection).add(job)


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


THREE = ("First part.", "Second part here.", "Third and last part.")


def steps(job: AIJob) -> list[str]:
    return [saved.step for saved in job.checkpoints]


def chunk_records(job: AIJob) -> list[dict]:
    return [dict(saved.data) for saved in job.checkpoints if saved.step == CHUNK_DONE]


def fail_at_third(world: World, script: Script) -> SpeechSynthesisError:
    flaky = Flaky(world.mock, {3})
    with pytest.raises(SpeechSynthesisError) as caught:
        world.runner(flaky).run(script, world.voice)
    return caught.value


# Chunker


def test_a_short_text_is_one_stripped_chunk() -> None:
    assert split_text("  Hello there.  ") == ("Hello there.",)


def test_a_blank_text_has_no_chunks() -> None:
    assert split_text(" \n\t ") == ()


def test_whole_sentences_are_packed_up_to_the_limit() -> None:
    text = "One two. Three four. Five six seven."

    assert split_text(text, 20) == ("One two. Three four.", "Five six seven.")


def test_sentences_end_with_unicode_punctuation() -> None:
    text = "Xin chào… Tạm biệt! Hẹn gặp lại? Vâng."

    assert split_text(text, 20) == ("Xin chào… Tạm biệt!", "Hẹn gặp lại? Vâng.")


def test_packed_sentences_keep_the_whitespace_between_them() -> None:
    assert split_text("Aa bb.\nCc dd.\n\nEe.", 14) == ("Aa bb.\nCc dd.", "Ee.")


def test_a_long_sentence_is_split_at_whitespace() -> None:
    assert split_text("word " * 10, 14) == (
        "word word word",
        "word word word",
        "word word word",
        "word",
    )


def test_a_token_without_whitespace_is_cut_at_the_limit() -> None:
    assert split_text("a" * 25, 10) == ("a" * 10, "a" * 10, "a" * 5)


def test_a_cut_tail_is_packed_with_the_next_word() -> None:
    assert split_text("a" * 13 + " bb", 10) == ("a" * 10, "aaa bb")


def test_every_chunk_fits_and_no_character_is_lost() -> None:
    sentence = "This sentence is about saving money every month. "
    text = sentence * 300 + "x" * 12_000 + " " + "long words " * 700
    chunks = split_text(text)

    assert len(chunks) > 5
    assert all(1 <= len(chunk) <= MAX_CHUNK == MAX_TEXT for chunk in chunks)
    assert all(chunk == chunk.strip() and chunk for chunk in chunks)
    assert "".join("".join(chunks).split()) == "".join(text.split())


def test_the_split_is_deterministic() -> None:
    text = "Alpha beta. " * 900 + "gamma" * 1200

    assert split_text(text) == split_text(text)


def test_the_limit_must_be_a_positive_whole_number() -> None:
    for limit in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            split_text("text", limit)


def test_plan_has_one_chunk_per_short_section_numbered_from_zero() -> None:
    script = Script.create(
        "item-1",
        sections=(
            ScriptSection(SectionKind.HOOK, "Hook."),
            ScriptSection(SectionKind.BODY, "Body."),
            ScriptSection(SectionKind.CTA, "Subscribe."),
        ),
    )

    assert plan_chunks(script) == (
        TextChunk(0, 0, "Hook."),
        TextChunk(1, 1, "Body."),
        TextChunk(2, 2, "Subscribe."),
    )


def test_a_long_section_gives_several_chunks_with_global_indexes() -> None:
    long_text = "Saving is a habit you can learn. " * 200
    script = Script.create(
        "item-1",
        sections=(
            ScriptSection(SectionKind.HOOK, "Hook."),
            ScriptSection(SectionKind.BODY, long_text.strip()),
            ScriptSection(SectionKind.CTA, "Bye."),
        ),
    )
    plan = plan_chunks(script)

    assert [chunk.index for chunk in plan] == list(range(len(plan)))
    assert [chunk.section_index for chunk in plan] == [0, 1, 1, 2]
    assert all(len(chunk.text) <= MAX_CHUNK for chunk in plan)


# Idempotency key


def test_the_audio_key_is_deterministic_and_prefixed() -> None:
    key = audio_key("script-1", "voice-1", 1)

    assert key == audio_key("script-1", "voice-1", 1)
    assert key.startswith(AUDIO_PREFIX) and AUDIO_PREFIX == "tts:"
    assert len(key) == len(AUDIO_PREFIX) + 64


def test_the_audio_key_changes_with_each_input() -> None:
    key = audio_key("script-1", "voice-1", 1)

    assert audio_key("script-2", "voice-1", 1) != key
    assert audio_key("script-1", "voice-2", 1) != key
    assert audio_key("script-1", "voice-1", 2) != key


def test_the_audio_key_refuses_empty_parts() -> None:
    with pytest.raises(ValueError):
        audio_key(" ", "voice-1", 1)


# Chunk store


def test_the_in_memory_store_puts_gets_and_overwrites() -> None:
    store = InMemoryChunkStore()

    assert isinstance(store, ChunkStore)
    assert store.get("job-1", 0) is None
    store.put("job-1", 0, b"one")
    store.put("job-1", 0, b"two")
    assert store.get("job-1", 0) == b"two"


def test_clearing_a_job_keeps_the_other_jobs() -> None:
    store = InMemoryChunkStore()
    store.put("job-1", 0, b"a")
    store.put("job-1", 1, b"b")
    store.put("job-2", 0, b"c")

    store.clear("job-1")

    assert store.get("job-1", 0) is None and store.get("job-1", 1) is None
    assert store.get("job-2", 0) == b"c"


# Happy path


def test_a_single_section_script_is_voiced(world: World) -> None:
    script = world.script("Hello there.")

    result = world.runner().run(script, world.voice)

    job = world.job(script)
    assert job.kind == JOB_KIND == "audio.generate"
    assert job.status is AIJobStatus.SUCCEEDED and job.attempts == 1
    assert job.content_item_id == world.item.id and job.session_id is None
    assert steps(job) == [PLANNED, CHUNK_DONE, COMPLETED]
    assert result.job_id == job.id and result.media_type == "audio/wav"
    assert result.duration_ms == 720  # the mock: 60 ms per character
    assert (result.chunks, result.characters) == (1, 12)
    assert (result.sample_rate, result.channels, result.sample_width) == (16000, 1, 2)
    assert (result.attempts, result.reused_chunks, result.synthesized_chunks) == (
        1,
        0,
        1,
    )
    assert len(world.mock.calls) == 1
    assert world.mock.calls[0] == world.voice.speech_request("Hello there.")


def test_a_multi_section_script_is_merged_in_order(world: World) -> None:
    script = world.script(*THREE)
    for frames in (10, 20, 30):
        world.mock.queue(wav(frames), duration_ms=frames * 100)

    result = world.runner().run(script, world.voice)

    fmt, count, frames = read(result.audio)
    assert fmt == (8000, 1, 2) and count == 60
    expected = b"".join(read(wav(n))[2] for n in (10, 20, 30))
    assert frames == expected
    assert result.duration_ms == 6000
    assert result.characters == sum(len(text) for text in THREE)
    assert result.chunks == 3


def test_checkpoints_record_scalars_per_step(world: World) -> None:
    script = world.script(*THREE)

    result = world.runner().run(script, world.voice)

    job = world.job(script)
    assert steps(job) == [PLANNED] + [CHUNK_DONE] * 3 + [COMPLETED]
    assert dict(job.checkpoints[0].data) == {
        "script_id": script.id,
        "voice_profile_id": world.voice.id,
        "voice_profile_version": world.voice.version,
        "chunks": 3,
        "characters": result.characters,
    }
    records = chunk_records(job)
    assert [record["index"] for record in records] == [0, 1, 2]
    assert [record["section_index"] for record in records] == [0, 1, 2]
    assert set(records[0]) == {
        "index",
        "section_index",
        "characters",
        "text_sha256",
        "checksum",
        "duration_ms",
        "media_type",
        "sample_rate",
        "channels",
        "sample_width",
    }
    completed = dict(job.checkpoints[-1].data)
    assert completed["chunks"] == 3
    assert completed["duration_ms"] == result.duration_ms
    assert completed["sample_rate"] == 16000
    assert len(completed["checksum"]) == 64


def test_the_session_is_recorded_on_the_job(world: World) -> None:
    session = make_session()
    with world.database.transaction() as connection:
        SessionRepository(connection).add(session)
    script = world.script("Hello.")

    world.runner().run(script, world.voice, session_id=session.id)

    assert world.job(script).session_id == session.id


# Provider errors and resume


@pytest.mark.parametrize(
    "code", [VoiceErrorCode.TIMEOUT, VoiceErrorCode.REFUSED], ids=lambda c: c.value
)
def test_a_provider_error_fails_the_attempt_and_is_raised(
    world: World, code: VoiceErrorCode
) -> None:
    script = world.script(*THREE)
    flaky = Flaky(world.mock, {2}, code)

    with pytest.raises(SpeechSynthesisError) as caught:
        world.runner(flaky, max_attempts=1).run(script, world.voice)

    assert caught.value.voice_code is code
    assert caught.value.retryable is code.retryable
    job = world.job(script)
    assert job.status is AIJobStatus.FAILED and job.attempts == 1
    assert job.last_error == code.value
    assert [record["index"] for record in chunk_records(job)] == [0]
    assert world.store.get(job.id, 0) is not None
    assert world.store.get(job.id, 1) is None


def test_a_failed_job_resumes_with_only_the_missing_chunks(world: World) -> None:
    script = world.script(*THREE)
    fail_at_third(world, script)
    first = world.job(script)
    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)

    result = world.runner(fresh).run(script, world.voice)

    assert [call.text for call in fresh.calls] == [THREE[2]]
    assert result.job_id == first.id
    assert (result.attempts, result.reused_chunks, result.synthesized_chunks) == (
        2,
        2,
        1,
    )
    job = world.job(script)
    assert job.status is AIJobStatus.SUCCEEDED and job.last_error is None
    assert steps(job).count(PLANNED) == 1
    assert steps(job).count(CHUNK_DONE) == 3


def test_a_resumed_result_equals_a_clean_run(world: World) -> None:
    script = world.script(*THREE)
    fail_at_third(world, script)
    resumed = world.runner().run(script, world.voice)
    other = World(world.database)
    clean = other.runner().run(other.script(*THREE), other.voice)

    assert resumed.audio == clean.audio
    assert resumed.duration_ms == clean.duration_ms


def test_a_wiped_store_makes_every_chunk_again(world: World) -> None:
    script = world.script(*THREE)
    fail_at_third(world, script)
    world.store.clear(world.job(script).id)
    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)

    result = world.runner(fresh).run(script, world.voice)

    assert [call.text for call in fresh.calls] == list(THREE)
    assert (result.reused_chunks, result.synthesized_chunks) == (0, 3)


def test_tampered_bytes_are_made_again(world: World) -> None:
    script = world.script(*THREE)
    fail_at_third(world, script)
    job_id = world.job(script).id
    world.store.put(job_id, 0, wav(5))
    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)

    world.runner(fresh).run(script, world.voice)

    assert [call.text for call in fresh.calls] == [THREE[0], THREE[2]]
    assert world.store.get(job_id, 0) != wav(5)


def test_bytes_without_a_checkpoint_are_never_adopted(world: World) -> None:
    script = world.script(*THREE)
    fail_at_third(world, script)
    job_id = world.job(script).id
    planted = wav(7)
    world.store.put(job_id, 2, planted)
    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)

    world.runner(fresh).run(script, world.voice)

    assert [call.text for call in fresh.calls] == [THREE[2]]
    assert world.store.get(job_id, 2) != planted


def test_a_checkpoint_without_bytes_is_made_again_and_the_newest_wins(
    world: World,
) -> None:
    script = world.script(*THREE)
    fail_at_third(world, script)
    job_id = world.job(script).id
    store = InMemoryChunkStore()
    store.put(job_id, 1, world.store.get(job_id, 1))  # chunk 0 is lost
    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)
    replacement = read(world.store.get(job_id, 0))[2][:3200]
    fresh.queue(_wav16(replacement), duration_ms=100)

    result = world.runner(fresh, store).run(script, world.voice)

    assert [call.text for call in fresh.calls] == [THREE[0], THREE[2]]
    records = [r for r in chunk_records(world.job(script)) if r["index"] == 0]
    assert len(records) == 2 and records[0]["checksum"] != records[1]["checksum"]
    rebuilt = world.runner(fresh, store).run(script, world.voice)
    assert rebuilt.audio == result.audio
    assert len(fresh.calls) == 2


def _wav16(frames: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(frames)
    return buffer.getvalue()


def test_the_duration_is_the_sum_of_the_newest_chunk_durations(
    world: World,
) -> None:
    script = world.script("One.", "Two.")
    world.mock.queue(wav(8), duration_ms=111)
    with pytest.raises(SpeechSynthesisError):
        world.runner(Flaky(world.mock, {2})).run(script, world.voice)
    world.mock.queue(wav(8), duration_ms=222)

    result = world.runner().run(script, world.voice)

    assert result.duration_ms == 333


# Invalid audio


def _assert_failed_without_chunk(world: World, script: Script, code: str, index: int):
    job = world.job(script)
    assert job.status is AIJobStatus.FAILED and job.last_error == code
    assert index not in [record["index"] for record in chunk_records(job)]
    assert world.store.get(job.id, index) is None


def test_chunks_in_different_formats_are_refused(world: World) -> None:
    script = world.script("One.", "Two.")
    world.mock.queue(wav(8, rate=8000), duration_ms=10)
    world.mock.queue(wav(8, rate=16000), duration_ms=10)

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == TtsErrorCode.FORMAT_MISMATCH
    _assert_failed_without_chunk(world, script, "domain.tts_format_mismatch", 1)
    assert [record["index"] for record in chunk_records(world.job(script))] == [0]


def test_a_non_wav_media_type_is_refused(world: World) -> None:
    script = world.script("One.")
    world.mock.queue(b"ID3\x03fake-mp3", duration_ms=10, media_type="audio/mpeg")

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_unsupported_format"
    _assert_failed_without_chunk(world, script, "domain.tts_unsupported_format", 0)


@pytest.mark.parametrize(
    "audio",
    [
        b"not a wav file at all",
        wav(8)[:30],
        wav(0),
        wav(8)[:20] + b"\x03\x00" + wav(8)[22:],  # IEEE float, not PCM
        wav(100)[:-50],  # truncated frames
    ],
    ids=["garbage", "header-only", "no-frames", "non-pcm", "truncated"],
)
def test_corrupt_audio_is_refused_and_not_checkpointed(
    world: World, audio: bytes
) -> None:
    script = world.script("One.")
    world.mock.queue(audio, duration_ms=10)

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_invalid_audio"
    _assert_failed_without_chunk(world, script, "domain.tts_invalid_audio", 0)


class _OtherVoice(MockSpeechSynthesizer):
    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        speech = super().synthesize(request)
        return dataclasses.replace(speech, voice_id="someone-else")


def test_an_answer_for_another_voice_is_refused(world: World) -> None:
    script = world.script("One.")
    liar = _OtherVoice(name="mock_tts", clock=world.clock)

    with pytest.raises(TtsJobError) as caught:
        world.runner(liar).run(script, world.voice)

    assert caught.value.code == "domain.tts_provider_response"
    _assert_failed_without_chunk(world, script, "domain.tts_provider_response", 0)


# Refused before anything is stored


def test_a_voice_of_another_provider_is_refused(world: World) -> None:
    voice = dataclasses.replace(world.voice, provider="other_tts")

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(world.script("One."), voice)

    assert caught.value.code == "domain.tts_provider_mismatch"
    assert world.job_count() == 0 and world.mock.calls == []


def test_a_voice_of_another_channel_is_refused(world: World) -> None:
    voice = make_voice_profile(make_channel(), provider="mock_tts")

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(world.script("One."), voice)

    assert caught.value.code == "domain.tts_channel_mismatch"
    assert world.job_count() == 0 and world.mock.calls == []


def test_a_missing_content_item_is_refused(world: World) -> None:
    script = Script.create("missing-item", "One.", clock=world.clock)

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_item_not_found"
    assert world.job_count() == 0 and world.mock.calls == []


# Succeeded, cancelled and running jobs


def test_a_succeeded_job_is_rebuilt_without_a_call_or_a_write(world: World) -> None:
    script = world.script(*THREE)
    first = world.runner().run(script, world.voice)
    before = world.job(script)
    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)

    again = world.runner(fresh).run(script, world.voice)

    assert fresh.calls == []
    assert world.job(script) == before
    assert again.audio == first.audio
    assert again.duration_ms == first.duration_ms
    assert (again.reused_chunks, again.synthesized_chunks) == (3, 0)
    assert again.attempts == 1


def test_a_succeeded_job_without_its_chunks_is_unavailable(world: World) -> None:
    script = world.script(*THREE)
    result = world.runner().run(script, world.voice)
    world.store.clear(result.job_id)

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_result_unavailable"
    assert world.job(script).status is AIJobStatus.SUCCEEDED
    assert len(world.mock.calls) == 3


def test_a_succeeded_job_with_changed_bytes_is_unavailable(world: World) -> None:
    script = world.script(*THREE)
    result = world.runner().run(script, world.voice)
    world.store.put(result.job_id, 1, wav(4, rate=16000))

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_result_unavailable"


def _stored_job(world: World, script: Script) -> AIJob:
    return AIJob.create(
        JOB_KIND,
        audio_key(script.id, world.voice.id, world.voice.version),
        content_item_id=world.item.id,
        clock=world.clock,
    )


def test_a_cancelled_job_is_refused(world: World) -> None:
    script = world.script("One.")
    world.add_job(_stored_job(world, script).cancel(clock=world.clock))

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_job_cancelled"
    assert world.mock.calls == []


def test_a_running_job_is_refused_without_take_over(world: World) -> None:
    script = world.script("One.")
    running = _stored_job(world, script).start(clock=world.clock)
    world.add_job(running)

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_job_in_progress"
    assert world.job(script) == running
    assert world.mock.calls == []


class _Crash(Exception):
    """A process dying mid-run: not a provider or job error."""


class _CrashingAt(MockSpeechSynthesizer):
    def __init__(self, call: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.call = call

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        if len(self.calls) + 1 == self.call:
            raise _Crash
        return super().synthesize(request)


def test_an_unexpected_error_leaves_the_job_running(world: World) -> None:
    script = world.script(*THREE)
    crashing = _CrashingAt(3, name="mock_tts", clock=world.clock)

    with pytest.raises(_Crash):
        world.runner(crashing).run(script, world.voice)

    job = world.job(script)
    assert job.status is AIJobStatus.RUNNING and job.last_error is None
    assert len(chunk_records(job)) == 2


def test_take_over_recovers_a_stuck_running_job(world: World) -> None:
    script = world.script(*THREE)
    crashing = _CrashingAt(3, name="mock_tts", clock=world.clock)
    with pytest.raises(_Crash):
        world.runner(crashing).run(script, world.voice)

    fresh = MockSpeechSynthesizer(name="mock_tts", clock=world.clock)
    result = world.runner(fresh).run(script, world.voice, take_over=True)

    assert [call.text for call in fresh.calls] == [THREE[2]]
    assert result.attempts == 2 and result.reused_chunks == 2
    assert world.job(script).status is AIJobStatus.SUCCEEDED


def test_a_waiting_job_is_refused_even_with_take_over(world: World) -> None:
    script = world.script("One.")
    running = _stored_job(world, script).start(clock=world.clock)
    world.add_job(dataclasses.replace(running, status=AIJobStatus.WAITING))

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice, take_over=True)

    assert caught.value.code == "domain.tts_job_in_progress"
    assert world.mock.calls == []


def test_a_changed_plan_fails_the_job(world: World) -> None:
    script = world.script("One.", "Two.")
    job = _stored_job(world, script).start(clock=world.clock)
    job = job.checkpoint(
        PLANNED,
        {
            "script_id": script.id,
            "voice_profile_id": world.voice.id,
            "voice_profile_version": world.voice.version,
            "chunks": 3,
            "characters": 8,
        },
        clock=world.clock,
    ).fail("voice.timeout", clock=world.clock)
    world.add_job(job)

    with pytest.raises(TtsJobError) as caught:
        world.runner().run(script, world.voice)

    assert caught.value.code == "domain.tts_job"
    stored = world.job(script)
    assert stored.status is AIJobStatus.FAILED and stored.last_error == "domain.tts_job"
    assert world.mock.calls == []


# Concurrency


class _Intruder(MockSpeechSynthesizer):
    """Moves the job row on while the first chunk is being synthesized."""

    def __init__(self, world: World, script: Script) -> None:
        super().__init__(name="mock_tts", clock=world.clock)
        self.world = world
        self.script = script

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        job = self.world.job(self.script)
        later = job.updated_at + timedelta(hours=1)
        with self.world.database.transaction() as connection:
            AIJobRepository(connection).update(
                dataclasses.replace(job, updated_at=later),
                expected_updated_at=job.updated_at,
            )
        return super().synthesize(request)


def test_a_stale_update_raises_and_does_not_fail_the_job(world: World) -> None:
    script = world.script("One.")

    with pytest.raises(ConcurrencyError):
        world.runner(_Intruder(world, script)).run(script, world.voice)

    job = world.job(script)
    assert job.status is AIJobStatus.RUNNING and job.last_error is None
    assert chunk_records(job) == []


# Privacy


def test_no_script_text_is_stored_in_the_job_or_errors(world: World) -> None:
    script = world.script(f"Say {SECRET} once.", f"Then {SECRET} again.")
    world.mock.queue(wav(8), duration_ms=10)
    world.mock.queue(wav(8, rate=16000), duration_ms=10)
    with pytest.raises(TtsJobError) as format_error:
        world.runner().run(script, world.voice)
    world.mock.fail_next(VoiceErrorCode.REFUSED)
    with pytest.raises(SpeechSynthesisError) as provider_error:
        world.runner().run(script, world.voice)
    world.mock.queue(wav(8), duration_ms=10)

    world.runner().run(script, world.voice)

    stored = json.dumps(world.job(script).as_dict())
    assert SECRET not in stored
    for error in (format_error.value, provider_error.value):
        assert SECRET not in str(error)
        assert SECRET not in error.detail and SECRET not in error.user_message
