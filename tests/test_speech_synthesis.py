"""H-086 Voice Provider (Prompt Pack v8, prompt #086).

The TTS provider interface (``SpeechSynthesizer``) and its deterministic mock:

- ``SpeechRequest`` and ``SynthesizedSpeech`` refuse invalid values, and an
  error never names the request text;
- ``VoiceErrorCode`` says which failures are retryable;
- the mock answers a mono, 16-bit, 16 kHz WAV tone chosen by the request
  (60 ms per character, at least 500 ms), can be scripted (queued answers,
  failures, health) and records its calls;
- ``Settings.voice_provider = mock`` and the ``voice_provider`` health check.
"""

import hashlib
import io
import logging
import math
import sys
import wave
from array import array
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.core.config import Environment, Settings, VoiceProviderKind
from ai_youtube_agent.core.errors import ProviderError, to_public
from ai_youtube_agent.main import create_app
from ai_youtube_agent.providers.mock_speech_synthesis import (
    MEDIA_TYPE,
    MIN_MS,
    MS_PER_CHAR,
    SAMPLE_RATE,
    MockSpeechSynthesizer,
)
from ai_youtube_agent.providers.speech_synthesis import (
    MAX_STYLE,
    MAX_TEXT,
    MAX_VOICE_ID,
    RETRYABLE_CODES,
    USER_MESSAGES,
    SpeechRequest,
    SpeechSynthesisError,
    SpeechSynthesizer,
    SynthesizedSpeech,
    VoiceErrorCode,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SECRET_TEXT = "Planted narration sentence 7f3a9c that must stay private."


def request(**changes) -> SpeechRequest:
    values = {"text": "Hello there.", "voice_id": "alloy-1", "language": "en"}
    return SpeechRequest(**{**values, **changes})


def speech(**changes) -> SynthesizedSpeech:
    values = {
        "audio": b"RIFF",
        "media_type": "audio/wav",
        "duration_ms": 1_000,
        "provider": "mock",
        "voice_id": "alloy-1",
        "characters": 12,
        "generated_at": T0,
    }
    return SynthesizedSpeech(**{**values, **changes})


def read_wav(audio: bytes) -> tuple:
    with wave.open(io.BytesIO(audio), "rb") as reader:
        params = reader.getparams()
        samples = array("h", reader.readframes(params.nframes))
    if sys.byteorder == "big":
        samples.byteswap()
    return params, samples


# The request


def test_a_valid_request_keeps_its_values() -> None:
    value = request(speaking_style="calm", language="en-US")

    assert (value.text, value.voice_id, value.language, value.speaking_style) == (
        "Hello there.",
        "alloy-1",
        "en-US",
        "calm",
    )
    assert request().speaking_style is None


def test_the_text_may_have_exactly_the_maximum_length() -> None:
    assert len(request(text="x" * MAX_TEXT).text) == MAX_TEXT == 5_000
    assert len(request(voice_id="v" * MAX_VOICE_ID).voice_id) == 200
    assert len(request(speaking_style="s" * MAX_STYLE).speaking_style) == 200


@pytest.mark.parametrize(
    ("bad", "field"),
    [
        (dict(text=""), "text"),
        (dict(text=" \n\t"), "text"),
        (dict(text="x" * 5_001), "text"),
        (dict(text=None), "text"),
        (dict(text=b"bytes"), "text"),
        (dict(text=42), "text"),
        (dict(voice_id=""), "voice_id"),
        (dict(voice_id="two words"), "voice_id"),
        (dict(voice_id="tab\tid"), "voice_id"),
        (dict(voice_id="v" * 201), "voice_id"),
        (dict(voice_id=None), "voice_id"),
        (dict(language=""), "language"),
        (dict(language="English"), "language"),
        (dict(language="not a tag!"), "language"),
        (dict(language="en\n"), "language"),
        (dict(language=None), "language"),
        (dict(speaking_style=""), "speaking_style"),
        (dict(speaking_style="  "), "speaking_style"),
        (dict(speaking_style="s" * 201), "speaking_style"),
        (dict(speaking_style=3), "speaking_style"),
    ],
)
def test_requests_reject_invalid_values(bad, field) -> None:
    with pytest.raises(ValueError, match=field):
        request(**bad)


def test_a_request_error_never_contains_the_text() -> None:
    for bad in (
        dict(voice_id="bad id"),
        dict(language="xx_YY"),
        dict(speaking_style=" "),
    ):
        with pytest.raises(ValueError) as caught:
            request(text=SECRET_TEXT, **bad)
        assert SECRET_TEXT not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        request(text=SECRET_TEXT * 100)
    assert "Planted" not in str(caught.value)


def test_a_request_is_frozen() -> None:
    with pytest.raises(AttributeError):
        request().text = "changed"


# The result


def test_a_valid_result_keeps_its_values() -> None:
    value = speech(characters=0, duration_ms=86_400_000)

    assert value.audio == b"RIFF" and value.media_type == "audio/wav"
    assert (value.duration_ms, value.characters) == (86_400_000, 0)
    assert speech(media_type="audio/mpeg").media_type == "audio/mpeg"
    assert speech(duration_ms=1).duration_ms == 1


@pytest.mark.parametrize(
    "bad",
    [
        dict(audio=b""),
        dict(audio="RIFF"),
        dict(audio=bytearray(b"RIFF")),
        dict(audio=None),
        dict(media_type="video/mp4"),
        dict(media_type="audio"),
        dict(media_type="audio/"),
        dict(media_type="Audio/wav"),
        dict(media_type="audio/wav\n"),
        dict(media_type="text/plain"),
        dict(duration_ms=0),
        dict(duration_ms=-1),
        dict(duration_ms=True),
        dict(duration_ms=86_400_001),
        dict(duration_ms=1.5),
        dict(duration_ms="100"),
        dict(characters=-1),
        dict(characters=True),
        dict(characters=False),
        dict(characters=2.0),
        dict(provider=""),
        dict(provider=None),
        dict(voice_id=""),
        dict(generated_at=datetime(2026, 10, 4, 12, 0)),
        dict(
            generated_at=datetime(
                2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=7))
            )
        ),
        dict(generated_at="2026-10-04T12:00:00Z"),
    ],
)
def test_results_reject_invalid_values(bad) -> None:
    with pytest.raises(ValueError):
        speech(**bad)


# Error codes


def test_only_unavailable_rate_limited_and_timeout_are_retryable() -> None:
    assert {code.value for code in RETRYABLE_CODES} == {
        "voice.unavailable",
        "voice.rate_limited",
        "voice.timeout",
    }
    assert {code.value for code in VoiceErrorCode if code.retryable} == {
        code.value for code in RETRYABLE_CODES
    }
    assert {code.value for code in VoiceErrorCode if not code.retryable} == {
        "voice.invalid_request",
        "voice.voice_not_found",
        "voice.refused",
    }


def test_every_code_has_a_safe_user_message() -> None:
    assert set(USER_MESSAGES) == set(VoiceErrorCode)
    assert all(message.strip() for message in USER_MESSAGES.values())
    assert USER_MESSAGES[VoiceErrorCode.UNAVAILABLE] == (
        "The voice service is not available. Please try again later."
    )
    assert len(set(USER_MESSAGES.values())) == len(USER_MESSAGES)


@pytest.mark.parametrize("code", list(VoiceErrorCode))
def test_errors_are_provider_errors_that_follow_their_code(code) -> None:
    error = SpeechSynthesisError(code, "internal detail", provider="mock")

    assert isinstance(error, ProviderError)
    assert (error.code, error.voice_code, error.provider) == (code.value, code, "mock")
    assert error.user_message == USER_MESSAGES[code]
    assert error.retryable is code.retryable
    public = error.to_public()
    assert (public.code, public.message, public.retryable) == (
        code.value,
        USER_MESSAGES[code],
        code.retryable,
    )
    assert public.category.value == "provider"
    assert "internal detail" not in str(public.as_dict())
    assert to_public(error) == public


def test_an_error_without_detail_uses_its_code() -> None:
    error = SpeechSynthesisError(VoiceErrorCode.TIMEOUT, provider="mock")

    assert str(error) == "voice.timeout"
    assert SpeechSynthesisError.default_code == "voice.unavailable"


# The mock


def test_the_mock_implements_the_protocol() -> None:
    mock = MockSpeechSynthesizer()

    assert isinstance(mock, SpeechSynthesizer)
    assert mock.name == "mock"
    assert MockSpeechSynthesizer(name="other").name == "other"
    assert not isinstance(object(), SpeechSynthesizer)


def test_the_mock_answers_a_wav_file_described_by_the_result() -> None:
    value = request(text="x" * 20)
    result = MockSpeechSynthesizer(clock=lambda: T0).synthesize(value)

    assert result.media_type == MEDIA_TYPE == "audio/wav"
    assert result.duration_ms == MS_PER_CHAR * 20 == 1_200
    assert (result.provider, result.voice_id) == ("mock", "alloy-1")
    assert result.characters == 20
    assert result.generated_at == T0
    params, _ = read_wav(result.audio)
    assert (params.nchannels, params.sampwidth, params.framerate) == (1, 2, 16_000)
    assert params.nframes * 1000 // params.framerate == result.duration_ms
    assert params.nframes == result.duration_ms * SAMPLE_RATE // 1000


def test_the_duration_has_a_minimum() -> None:
    mock = MockSpeechSynthesizer()

    for text in ("Hi", "x" * 8):
        result = mock.synthesize(request(text=text))
        assert result.duration_ms == MIN_MS == 500
        params, _ = read_wav(result.audio)
        assert params.nframes * 1000 // params.framerate == 500
    assert mock.synthesize(request(text="x" * 9)).duration_ms == 540


def test_the_longest_text_gives_five_minutes() -> None:
    result = MockSpeechSynthesizer().synthesize(request(text="x" * MAX_TEXT))

    assert result.duration_ms == 300_000
    params, _ = read_wav(result.audio)
    assert params.nframes * 1000 // params.framerate == 300_000


def test_the_tone_is_not_silent() -> None:
    _, samples = read_wav(MockSpeechSynthesizer().synthesize(request()).audio)

    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    assert 7_900 <= peak <= 8_000
    assert 5_000 <= rms <= 6_000


def test_characters_are_the_text_length_as_sent() -> None:
    text = "  Café  ngày mới.  "
    result = MockSpeechSynthesizer().synthesize(request(text=text))

    assert result.characters == len(text)


def test_the_same_request_gives_the_same_bytes() -> None:
    first = MockSpeechSynthesizer().synthesize(request())
    second = MockSpeechSynthesizer().synthesize(request())

    assert first.audio == second.audio
    assert first.duration_ms == second.duration_ms


def test_equal_requests_built_separately_give_the_same_bytes() -> None:
    values = {"text": "Chào buổi sáng, Café.", "speaking_style": "calm"}
    first = SpeechRequest(voice_id="alloy-1", language="vi", **values)
    second = SpeechRequest(**values, language="vi", voice_id="alloy-1")

    assert first == second and first is not second
    assert (
        MockSpeechSynthesizer().synthesize(first).audio
        == MockSpeechSynthesizer().synthesize(second).audio
    )


# Different requests usually, but not always, get different tones (there are
# only 601); these fixed cases are known to differ.
@pytest.mark.parametrize(
    "change",
    [
        dict(text="Hello there!"),
        dict(voice_id="alloy-2"),
        dict(language="en-GB"),
        dict(speaking_style="calm"),
    ],
)
def test_these_changed_requests_give_different_bytes(change) -> None:
    mock = MockSpeechSynthesizer()

    assert mock.synthesize(request(**change)).audio != mock.synthesize(request()).audio


def downward_crossings(samples) -> int:
    pairs = zip(samples, samples[1:], strict=False)
    return sum(1 for a, b in pairs if a > 0 and b <= 0)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        (
            request(text="A fixed narration line."),
            "A fixed narration line.\x00alloy-1\x00en\x000\x00",
        ),
        (
            request(text="Chào buổi sáng, Café.", language="vi", speaking_style="calm"),
            "Chào buổi sáng, Café.\x00alloy-1\x00vi\x001\x00calm",
        ),
    ],
)
def test_the_frequency_comes_from_the_canonical_request_digest(
    value, canonical
) -> None:
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    expected = 200 + int.from_bytes(digest[:4], "big") % 601

    result = MockSpeechSynthesizer().synthesize(value)

    assert result.duration_ms >= 1_000
    _, samples = read_wav(result.audio)
    # A whole number of hertz crosses zero downwards that many times a second.
    assert downward_crossings(samples[:SAMPLE_RATE]) == expected


def test_the_clock_defaults_to_now_in_utc() -> None:
    before = datetime.now(UTC)
    result = MockSpeechSynthesizer().synthesize(request())

    assert before <= result.generated_at <= datetime.now(UTC)
    assert result.generated_at.utcoffset() == timedelta(0)


def test_queued_answers_are_used_in_order() -> None:
    mock = MockSpeechSynthesizer(clock=lambda: T0)
    mock.queue(b"first", duration_ms=10)
    mock.queue(b"second", duration_ms=20, media_type="audio/mpeg")

    first = mock.synthesize(request(text=SECRET_TEXT))
    second = mock.synthesize(request(voice_id="v2"))
    third = mock.synthesize(request())

    assert (first.audio, first.duration_ms, first.media_type) == (
        b"first",
        10,
        "audio/wav",
    )
    assert (first.characters, first.voice_id) == (len(SECRET_TEXT), "alloy-1")
    assert (second.audio, second.duration_ms, second.media_type) == (
        b"second",
        20,
        "audio/mpeg",
    )
    assert second.voice_id == "v2"
    assert third.audio == MockSpeechSynthesizer().synthesize(request()).audio


def test_garbage_bytes_are_returned_as_they_are() -> None:
    mock = MockSpeechSynthesizer()
    mock.queue(b"\x00not a wav\xff", duration_ms=500)

    result = mock.synthesize(request())

    assert result.audio == b"\x00not a wav\xff"
    with pytest.raises(wave.Error):
        read_wav(result.audio)


@pytest.mark.parametrize(
    "bad",
    [
        dict(audio=b"", duration_ms=10),
        dict(audio="text", duration_ms=10),
        dict(audio=b"a", duration_ms=0),
        dict(audio=b"a", duration_ms=True),
        dict(audio=b"a", duration_ms=86_400_001),
        dict(audio=b"a", duration_ms=10, media_type="video/mp4"),
        dict(audio=b"a", duration_ms=10, media_type="wav"),
    ],
)
def test_queue_checks_its_values_at_once(bad) -> None:
    mock = MockSpeechSynthesizer()
    audio = bad.pop("audio")

    with pytest.raises(ValueError):
        mock.queue(audio, **bad)
    assert mock.synthesize(request()).media_type == MEDIA_TYPE


def test_scripted_failures_come_first_then_normal_answers() -> None:
    mock = MockSpeechSynthesizer()
    mock.queue(b"kept", duration_ms=10)
    mock.fail_next(VoiceErrorCode.RATE_LIMITED, times=2)
    mock.fail_next(VoiceErrorCode.VOICE_NOT_FOUND)

    for code in (
        VoiceErrorCode.RATE_LIMITED,
        VoiceErrorCode.RATE_LIMITED,
        VoiceErrorCode.VOICE_NOT_FOUND,
    ):
        with pytest.raises(SpeechSynthesisError) as caught:
            mock.synthesize(request())
        assert caught.value.voice_code is code
        assert caught.value.retryable is code.retryable
        assert caught.value.detail == "scripted synthesize failure"
        assert caught.value.provider == "mock"
    assert mock.synthesize(request()).audio == b"kept"
    assert len(mock.calls) == 4


def test_fail_next_needs_at_least_one_time() -> None:
    with pytest.raises(ValueError):
        MockSpeechSynthesizer().fail_next(VoiceErrorCode.TIMEOUT, times=0)


def test_check_follows_the_health_switch() -> None:
    mock = MockSpeechSynthesizer()
    mock.check()

    mock.set_healthy(False)
    with pytest.raises(SpeechSynthesisError) as caught:
        mock.check()
    assert caught.value.voice_code is VoiceErrorCode.UNAVAILABLE
    assert caught.value.retryable

    mock.set_healthy(True)
    mock.check()


def test_calls_record_every_request() -> None:
    mock = MockSpeechSynthesizer()
    first, second = request(), request(text="Second.")
    mock.fail_next(VoiceErrorCode.REFUSED)

    with pytest.raises(SpeechSynthesisError):
        mock.synthesize(first)
    mock.synthesize(second)

    assert mock.calls == [first, second]


def test_the_injected_clock_is_used_for_each_answer() -> None:
    times = iter([T0, T0 + timedelta(seconds=1)])
    mock = MockSpeechSynthesizer(clock=lambda: next(times))

    assert mock.synthesize(request()).generated_at == T0
    assert mock.synthesize(request()).generated_at == T0 + timedelta(seconds=1)


def test_the_request_text_never_reaches_an_error_or_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = MockSpeechSynthesizer()
    for code in VoiceErrorCode:
        mock.fail_next(code)
        with pytest.raises(SpeechSynthesisError) as caught:
            mock.synthesize(request(text=SECRET_TEXT))
        error = caught.value
        logging.getLogger("test.voice").error(
            "voice failed", extra={"fields": error.log_fields()}
        )
        for shown in (
            str(error),
            error.detail,
            str(error.log_fields()),
            str(error.to_public().as_dict()),
            repr(error.args),
        ):
            assert SECRET_TEXT not in shown
            assert "7f3a9c" not in shown
    mock.set_healthy(False)
    with pytest.raises(SpeechSynthesisError) as caught:
        mock.check()
    assert "7f3a9c" not in str(caught.value.log_fields())
    assert "7f3a9c" not in caplog.text


# Settings and bootstrap


def test_settings_default_to_the_mock_and_refuse_unknown_kinds() -> None:
    assert Settings().voice_provider is VoiceProviderKind.MOCK
    assert [kind.value for kind in VoiceProviderKind] == ["mock"]
    with pytest.raises(ValueError):
        Settings(voice_provider="elevenlabs")


def test_bootstrap_resolves_the_mock_once() -> None:
    container = build_container(Settings(environment=Environment.TEST))

    synthesizer = container.resolve(SpeechSynthesizer)
    assert isinstance(synthesizer, MockSpeechSynthesizer)
    assert container.resolve(SpeechSynthesizer) is synthesizer


def test_an_unhealthy_mock_degrades_the_health_endpoint(
    tmp_path: Path, database_copy
) -> None:
    container = build_container(
        Settings(
            environment=Environment.TEST,
            database_path=database_copy(tmp_path / "a.db"),
        )
    )
    container.resolve(SpeechSynthesizer).set_healthy(False)

    with TestClient(create_app(container)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    [check] = [c for c in body["checks"] if c["name"] == "voice_provider"]
    assert (check["kind"], check["status"]) == ("provider", "degraded")
    others = [c for c in body["checks"] if c["name"] != "voice_provider"]
    assert all(c["status"] == "ok" for c in others)
    assert "mock set unhealthy" not in response.text
