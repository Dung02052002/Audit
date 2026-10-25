"""Tests for the real Edge TTS flow of ``EdgeTTSAdapter`` (PROV-FIX-01).

The Edge service is replaced by an injected ``communicate_factory`` whose objects
stream the same chunk dicts as ``edge_tts.Communicate``. ``aiohttp`` and
``edge_tts.exceptions`` are faked in ``sys.modules`` so the error mapping is
tested without the optional ``tts`` extra. No test needs the internet.
"""

from __future__ import annotations

import asyncio
import sys
import types
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.registry import EdgeTTSAdapter
from ai_youtube_agent.providers.registry.tts_adapters import SpeechBoundary, TimedSpeech
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    VoiceErrorCode,
)
from media_fixtures import make_mp3

VOICE = "vi-VN-HoaiMyNeural"
TEXT = "Xin chào, đây là hệ thống sản xuất video tự động."
CODE = VoiceErrorCode


def make_request(text: str = TEXT, voice: str = VOICE, **kwargs: Any) -> SpeechRequest:
    return SpeechRequest(text=text, voice_id=voice, language="vi-VN", **kwargs)


def boundary(offset: int = 0) -> dict[str, Any]:
    return {"type": "SentenceBoundary", "offset": offset, "duration": 1, "text": "x"}


def audio_chunks(data: bytes, pieces: int = 3) -> list[dict[str, Any]]:
    size = -(-len(data) // pieces)
    chunks: list[dict[str, Any]] = []
    for index in range(0, len(data), size):
        chunks.append({"type": "audio", "data": data[index : index + size]})
        chunks.append(boundary(index))
    return chunks


@dataclass
class FakeCommunicate:
    chunks: list[dict[str, Any]]
    error: BaseException | None = None
    hang: bool = False

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        if self.hang:
            await asyncio.sleep(3600)
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


@dataclass
class FakeFactory:
    chunks: list[dict[str, Any]] = field(default_factory=list)
    stream_error: BaseException | None = None
    ctor_error: BaseException | None = None
    hang: bool = False
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, text: str, voice: str, **kwargs: Any) -> FakeCommunicate:
        self.calls.append((text, voice, kwargs))
        if self.ctor_error is not None:
            raise self.ctor_error
        return FakeCommunicate(self.chunks, self.stream_error, self.hang)


def adapter_with(factory: FakeFactory, **kwargs: Any) -> EdgeTTSAdapter:
    return EdgeTTSAdapter(communicate_factory=factory, **kwargs)


def failure(adapter: EdgeTTSAdapter, request: SpeechRequest) -> SpeechSynthesisError:
    with pytest.raises(SpeechSynthesisError) as info:
        adapter.synthesize(request)
    return info.value


@pytest.fixture
def mp3() -> bytes:
    return make_mp3(40)


@pytest.fixture
def fake_aiohttp(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class ClientResponseError(ClientError):
        def __init__(self, status: int) -> None:
            super().__init__(f"response error {status}")
            self.status = status

    module.ClientError = ClientError  # type: ignore[attr-defined]
    module.ClientResponseError = ClientResponseError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    return module


@pytest.fixture
def fake_edge_exceptions(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    exceptions = types.ModuleType("edge_tts.exceptions")

    class EdgeTTSException(Exception):
        pass

    for name in (
        "UnknownResponse",
        "UnexpectedResponse",
        "NoAudioReceived",
        "WebSocketError",
        "SkewAdjustmentError",
    ):
        setattr(exceptions, name, type(name, (EdgeTTSException,), {}))
    package = types.ModuleType("edge_tts")
    package.exceptions = exceptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edge_tts", package)
    monkeypatch.setitem(sys.modules, "edge_tts.exceptions", exceptions)
    return exceptions


# -----------------------------------------------------------------------------
# Success path
# -----------------------------------------------------------------------------


def test_synthesize_returns_the_real_streamed_audio(mp3: bytes) -> None:
    factory = FakeFactory(chunks=audio_chunks(mp3))
    adapter = adapter_with(factory)
    request = make_request()
    speech = adapter.synthesize(request)

    assert speech.provider == "edge_tts"
    assert speech.media_type == "audio/mpeg"
    assert speech.duration_ms == 960
    assert speech.voice_id == VOICE
    assert speech.characters == len(TEXT)
    assert speech.audio == mp3
    assert speech.generated_at.utcoffset().total_seconds() == 0


def test_text_reaches_edge_untouched_with_documented_arguments() -> None:
    mp3 = make_mp3(10)
    text = "Chào buổi sáng!\nTiếng Việt có dấu: ắ ồ ữ & <b> \"quote\" 'x'."
    factory = FakeFactory(chunks=audio_chunks(mp3))
    adapter = adapter_with(factory, timeout_seconds=7.9, overall_timeout_seconds=42.0)
    adapter.synthesize(make_request(text, speaking_style="calm"))

    sent_text, sent_voice, kwargs = factory.calls[0]
    assert sent_text == text
    assert sent_voice == VOICE
    assert kwargs == {
        "rate": "+0%",
        "volume": "+0%",
        "pitch": "+0Hz",
        "connect_timeout": 7,
        "receive_timeout": 42,
    }


def test_the_requested_voice_is_used_exactly() -> None:
    factory = FakeFactory(chunks=audio_chunks(make_mp3(10)))
    speech = adapter_with(factory, default_voice=VOICE).synthesize(
        make_request(voice="en-US-JennyNeural")
    )
    assert factory.calls[0][1] == "en-US-JennyNeural"
    assert speech.voice_id == "en-US-JennyNeural"


def test_synthesize_works_inside_a_running_event_loop() -> None:
    mp3 = make_mp3(10)
    adapter = adapter_with(FakeFactory(chunks=audio_chunks(mp3)))

    async def inside_loop() -> bytes:
        return adapter.synthesize(make_request()).audio

    assert asyncio.run(inside_loop()) == mp3


# -----------------------------------------------------------------------------
# Voice validation
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "voice",
    ["mock-vietnamese-1", "vi-vn-HoaiMyNeural", "vi-VN-HoaiMy", "HoaiMyNeural", "x"],
)
def test_invalid_voice_is_rejected_before_any_io_and_never_replaced(
    voice: str,
) -> None:
    factory = FakeFactory(chunks=audio_chunks(make_mp3(10)))
    adapter = adapter_with(factory, default_voice=VOICE)
    error = failure(adapter, make_request(voice=voice))

    assert error.voice_code is CODE.VOICE_NOT_FOUND
    assert error.retryable is False
    assert factory.calls == []


@pytest.mark.parametrize(
    "voice",
    [
        "vi-VN-HoaiMyNeural",
        "vi-VN-NamMinhNeural",
        "en-US-JennyNeural",
        "zh-CN-liaoning-XiaobeiNeural",
        "fil-PH-BlessicaNeural",
    ],
)
def test_neural_voice_names_are_accepted(voice: str) -> None:
    factory = FakeFactory(chunks=audio_chunks(make_mp3(10)))
    assert adapter_with(factory).synthesize(make_request(voice=voice)).voice_id == voice


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


def test_timeout_maps_to_a_retryable_timeout() -> None:
    error = failure(
        adapter_with(FakeFactory(stream_error=TimeoutError())), make_request()
    )
    assert error.voice_code is CODE.TIMEOUT
    assert error.retryable is True


@pytest.mark.parametrize("raised", [OSError("down"), ConnectionError("reset")])
def test_network_errors_map_to_unavailable_and_say_internet_is_required(
    raised: Exception,
) -> None:
    error = failure(adapter_with(FakeFactory(stream_error=raised)), make_request())
    assert error.voice_code is CODE.UNAVAILABLE
    assert error.retryable is True
    assert "internet" in str(error)


def test_rate_limit_maps_to_rate_limited(fake_aiohttp: types.ModuleType) -> None:
    factory = FakeFactory(stream_error=fake_aiohttp.ClientResponseError(429))
    error = failure(adapter_with(factory), make_request())
    assert error.voice_code is CODE.RATE_LIMITED
    assert error.retryable is True


@pytest.mark.parametrize("status", [403, 500, 503])
def test_other_http_errors_map_to_unavailable(
    fake_aiohttp: types.ModuleType, status: int
) -> None:
    factory = FakeFactory(stream_error=fake_aiohttp.ClientResponseError(status))
    error = failure(adapter_with(factory), make_request())
    assert error.voice_code is CODE.UNAVAILABLE
    assert str(status) in str(error)


def test_aiohttp_client_errors_map_to_unavailable(
    fake_aiohttp: types.ModuleType,
) -> None:
    factory = FakeFactory(stream_error=fake_aiohttp.ClientError("no route"))
    assert failure(adapter_with(factory), make_request()).voice_code is CODE.UNAVAILABLE


def test_no_audio_received_maps_to_refused(
    fake_edge_exceptions: types.ModuleType,
) -> None:
    factory = FakeFactory(stream_error=fake_edge_exceptions.NoAudioReceived("none"))
    error = failure(adapter_with(factory), make_request())
    assert error.voice_code is CODE.REFUSED
    assert error.retryable is False


@pytest.mark.parametrize(
    "name",
    ["WebSocketError", "SkewAdjustmentError", "UnknownResponse", "UnexpectedResponse"],
)
def test_edge_service_errors_map_to_unavailable(
    fake_edge_exceptions: types.ModuleType, name: str
) -> None:
    factory = FakeFactory(stream_error=getattr(fake_edge_exceptions, name)("x"))
    assert failure(adapter_with(factory), make_request()).voice_code is CODE.UNAVAILABLE


@pytest.mark.parametrize("raised", [ValueError("bad rate"), TypeError("bad type")])
def test_constructor_rejection_maps_to_invalid_request(raised: Exception) -> None:
    error = failure(adapter_with(FakeFactory(ctor_error=raised)), make_request())
    assert error.voice_code is CODE.INVALID_REQUEST
    assert error.retryable is False


def test_unexpected_exceptions_do_not_escape_as_raw_errors() -> None:
    error = failure(
        adapter_with(FakeFactory(stream_error=RuntimeError("boom"))), make_request()
    )
    assert error.voice_code is CODE.UNAVAILABLE


def test_error_details_never_contain_the_request_text() -> None:
    text = "Một đoạn văn bí mật không được lộ ra trong lỗi."
    for factory in (
        FakeFactory(stream_error=OSError(f"failed while sending {text}")),
        FakeFactory(ctor_error=ValueError(f"bad text {text}")),
        FakeFactory(chunks=[]),
    ):
        error = failure(adapter_with(factory), make_request(text))
        assert text not in str(error)
        assert text not in repr(error.log_fields())


# -----------------------------------------------------------------------------
# Bad audio
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [boundary(), boundary(1)],
        [{"type": "audio", "data": b""}],
    ],
    ids=["no-chunks", "only-boundaries", "empty-audio"],
)
def test_missing_audio_is_an_error_not_a_silent_mock(
    chunks: list[dict[str, Any]],
) -> None:
    error = failure(adapter_with(FakeFactory(chunks=chunks)), make_request())
    assert error.voice_code is CODE.UNAVAILABLE
    assert "no audio" in str(error)


def test_undecodable_audio_is_an_error(mp3: bytes) -> None:
    for data in (b"this is not an mp3 file at all", mp3[:-10]):
        error = failure(
            adapter_with(FakeFactory(chunks=audio_chunks(data))), make_request()
        )
        assert error.voice_code is CODE.UNAVAILABLE
        assert "not decodable MP3" in str(error)


def test_overall_timeout_stops_a_stream_that_never_ends() -> None:
    adapter = adapter_with(FakeFactory(hang=True), overall_timeout_seconds=0.2)
    error = failure(adapter, make_request())
    assert error.voice_code is CODE.TIMEOUT
    assert error.retryable is True


# -----------------------------------------------------------------------------
# Missing library
# -----------------------------------------------------------------------------


def test_missing_library_gives_a_structured_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    adapter = EdgeTTSAdapter()
    error = failure(adapter, make_request())

    assert error.voice_code is CODE.UNAVAILABLE
    assert "uv sync --extra tts" in str(error)
    assert "internet" in str(error)
    assert error.retryable is False
    assert error.provider == "edge_tts"


def test_check_models_and_metadata_report_the_missing_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    adapter = EdgeTTSAdapter()

    models = adapter.check_models()
    assert models.valid is False
    assert "uv sync --extra tts" in (models.error_message or "")
    assert adapter.capabilities().metadata["library_installed"] is False


def test_capabilities_state_that_the_internet_is_required() -> None:
    caps = EdgeTTSAdapter(default_voice=VOICE).capabilities()
    assert caps.resource_requirements.requires_internet is True
    assert caps.metadata["network"] == "internet_required"
    assert caps.metadata["offline"] is False
    assert caps.metadata["output_format"] == "audio/mpeg"
    assert caps.metadata["install"] == "uv sync --extra tts"
    assert caps.metadata["default_voice"] == VOICE
    assert isinstance(caps.metadata["library_installed"], bool)


def test_check_connection_failure_says_internet_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def offline(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    result = EdgeTTSAdapter().check_connection()
    assert result.connected is False
    assert (result.error_message or "").startswith("Edge TTS requires internet access:")


def test_check_models_is_valid_with_an_injected_factory() -> None:
    assert adapter_with(FakeFactory()).check_models().valid is True


# -----------------------------------------------------------------------------
# Explicit fallback (opt-in only)
# -----------------------------------------------------------------------------


def test_there_is_no_fallback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert EdgeTTSAdapter().fallback_enabled is False
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    assert failure(EdgeTTSAdapter(), make_request()).voice_code is CODE.UNAVAILABLE


@pytest.mark.parametrize(
    "raised",
    [OSError("down"), TimeoutError()],
    ids=["unavailable", "timeout"],
)
def test_explicit_fallback_is_used_for_provider_errors_with_its_own_name(
    raised: Exception,
) -> None:
    fallback = MockSpeechSynthesizer()
    adapter = adapter_with(FakeFactory(stream_error=raised), fallback=fallback)
    assert adapter.fallback_enabled is True
    speech = adapter.synthesize(make_request())

    assert speech.provider == "mock"
    assert speech.provider != "edge_tts"
    assert len(fallback.calls) == 1


def test_explicit_fallback_is_used_for_rate_limits(
    fake_aiohttp: types.ModuleType,
) -> None:
    factory = FakeFactory(stream_error=fake_aiohttp.ClientResponseError(429))
    speech = adapter_with(factory, fallback=MockSpeechSynthesizer()).synthesize(
        make_request()
    )
    assert speech.provider == "mock"


def test_explicit_fallback_is_used_when_the_library_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    speech = EdgeTTSAdapter(fallback=MockSpeechSynthesizer()).synthesize(make_request())
    assert speech.provider == "mock"


def test_fallback_is_not_used_for_voice_not_found() -> None:
    fallback = MockSpeechSynthesizer()
    adapter = adapter_with(FakeFactory(), fallback=fallback)
    error = failure(adapter, make_request(voice="mock-vietnamese-1"))

    assert error.voice_code is CODE.VOICE_NOT_FOUND
    assert fallback.calls == []


def test_fallback_is_not_used_for_invalid_request_or_refusal(
    fake_edge_exceptions: types.ModuleType,
) -> None:
    fallback = MockSpeechSynthesizer()
    rejected = adapter_with(FakeFactory(ctor_error=ValueError("x")), fallback=fallback)
    assert failure(rejected, make_request()).voice_code is CODE.INVALID_REQUEST

    refused_factory = FakeFactory(
        stream_error=fake_edge_exceptions.NoAudioReceived("n")
    )
    refused = adapter_with(refused_factory, fallback=fallback)
    assert failure(refused, make_request()).voice_code is CODE.REFUSED
    assert fallback.calls == []


# -----------------------------------------------------------------------------
# Construction and the real library (skipped without the optional extra)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"overall_timeout_seconds": -1},
        {"output_format": "audio/ogg"},
    ],
)
def test_invalid_constructor_values_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        EdgeTTSAdapter(**kwargs)


def test_synthesize_to_file_writes_and_verifies_audio_on_disk(
    mp3: bytes, tmp_path: Path
) -> None:
    adapter = adapter_with(FakeFactory(chunks=audio_chunks(mp3)))
    out_file = tmp_path / "voice" / "narration.mp3"
    speech, saved_path = adapter.synthesize_to_file(make_request(), out_file)

    assert saved_path == out_file.resolve()
    assert saved_path.exists()
    assert saved_path.stat().st_size == len(mp3)
    assert saved_path.read_bytes() == speech.audio == mp3


def test_with_wav_output_transcodes_via_ffmpeg_or_reports_error(
    mp3: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import subprocess
    import wave

    raw_pcm = b"\x10\x00" * 2400

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, stdout=raw_pcm, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = adapter_with(FakeFactory(chunks=audio_chunks(mp3))).with_wav_output()
    speech = adapter.synthesize(make_request())

    assert speech.media_type == "audio/wav"
    assert speech.duration_ms == 100
    assert speech.provider == "edge_tts"
    with wave.open(io.BytesIO(speech.audio), "rb") as reader:
        assert reader.getframerate() == 24000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.readframes(reader.getnframes()) == raw_pcm


def test_default_factory_builds_a_real_communicate_without_network() -> None:
    edge_tts = pytest.importorskip("edge_tts")
    factory: Callable[..., Any] = EdgeTTSAdapter()._resolve_factory()
    communicate = factory(
        TEXT,
        VOICE,
        rate="+0%",
        volume="+0%",
        pitch="+0Hz",
        connect_timeout=8,
        receive_timeout=60,
    )
    assert isinstance(communicate, edge_tts.Communicate)


# -----------------------------------------------------------------------------
# synthesize_timed: prosody and word boundaries (ANIM-01)
# -----------------------------------------------------------------------------


def word_event(
    text: str, offset: int, duration: int, kind: str = "WordBoundary"
) -> dict[str, Any]:
    return {"type": kind, "offset": offset, "duration": duration, "text": text}


def timed_chunks(mp3: bytes) -> list[dict[str, Any]]:
    """Audio interleaved with word boundaries (100 ns ticks) and a sentence one."""
    size = -(-len(mp3) // 2)
    return [
        word_event("Xin", 1_000_000, 2_500_000),
        {"type": "audio", "data": mp3[:size]},
        word_event("chào", 4_000_000, 3_000_000),
        word_event("x", 0, 1, kind="SentenceBoundary"),
        {"type": "audio", "data": mp3[size:]},
        {"type": "unknown", "data": b"?"},
    ]


def test_synthesize_timed_returns_mp3_with_millisecond_word_boundaries(
    mp3: bytes,
) -> None:
    factory = FakeFactory(chunks=timed_chunks(mp3))
    adapter = adapter_with(factory)

    timed = adapter.synthesize_timed(make_request(), rate="-6%", pitch="-4Hz")

    assert isinstance(timed, TimedSpeech)
    assert timed.audio == mp3
    assert timed.speech.media_type == "audio/mpeg"
    assert timed.duration_ms == 960
    assert timed.boundaries == (
        SpeechBoundary("WordBoundary", 100, 350, "Xin"),
        SpeechBoundary("WordBoundary", 400, 700, "chào"),
    )
    assert (timed.rate, timed.pitch, timed.boundary) == ("-6%", "-4Hz", "WordBoundary")


def test_synthesize_timed_sends_prosody_and_boundary_to_edge(mp3: bytes) -> None:
    factory = FakeFactory(chunks=timed_chunks(mp3))
    adapter = adapter_with(factory, timeout_seconds=7.9, overall_timeout_seconds=42.0)

    adapter.synthesize_timed(make_request(), rate="-10%", pitch="-4Hz")

    text, voice, kwargs = factory.calls[0]
    assert (text, voice) == (TEXT, VOICE)
    assert kwargs == {
        "rate": "-10%",
        "volume": "+0%",
        "pitch": "-4Hz",
        "boundary": "WordBoundary",
        "connect_timeout": 7,
        "receive_timeout": 42,
    }


def test_synthesize_timed_defaults_are_neutral_word_boundaries(mp3: bytes) -> None:
    factory = FakeFactory(chunks=timed_chunks(mp3))

    adapter_with(factory).synthesize_timed(make_request())

    kwargs = factory.calls[0][2]
    assert (kwargs["rate"], kwargs["pitch"], kwargs["boundary"]) == (
        "+0%",
        "+0Hz",
        "WordBoundary",
    )


def test_synthesize_timed_can_ask_for_sentence_boundaries(mp3: bytes) -> None:
    factory = FakeFactory(chunks=timed_chunks(mp3))

    timed = adapter_with(factory).synthesize_timed(
        make_request(), boundary="SentenceBoundary"
    )

    assert factory.calls[0][2]["boundary"] == "SentenceBoundary"
    assert timed.boundaries == (SpeechBoundary("SentenceBoundary", 0, 0, "x"),)


def test_synthesize_timed_keeps_malformed_boundaries_out(mp3: bytes) -> None:
    chunks = [
        word_event("ok", 10_000, 20_000),
        {"type": "WordBoundary", "offset": "x", "duration": 1, "text": "bad"},
        {"type": "WordBoundary", "offset": -5, "duration": 1, "text": "neg"},
        {"type": "WordBoundary", "offset": 1, "duration": 1},
        {"type": "WordBoundary", "offset": True, "duration": 1, "text": "bool"},
        *audio_chunks(mp3),
    ]

    timed = adapter_with(FakeFactory(chunks=chunks)).synthesize_timed(make_request())

    assert timed.boundaries == (SpeechBoundary("WordBoundary", 1, 3, "ok"),)


def test_synthesize_still_passes_neutral_prosody_and_no_boundary(mp3: bytes) -> None:
    factory = FakeFactory(chunks=audio_chunks(mp3))

    adapter_with(factory).synthesize(make_request())

    kwargs = factory.calls[0][2]
    assert kwargs["rate"] == "+0%" and kwargs["pitch"] == "+0Hz"
    assert "boundary" not in kwargs


@pytest.mark.parametrize(
    ("kwargs", "voice_code"),
    [
        ({"rate": "slow"}, CODE.INVALID_REQUEST),
        ({"rate": "-6"}, CODE.INVALID_REQUEST),
        ({"pitch": "low"}, CODE.INVALID_REQUEST),
        ({"pitch": "-4"}, CODE.INVALID_REQUEST),
        ({"boundary": "PhonemeBoundary"}, CODE.INVALID_REQUEST),
    ],
)
def test_synthesize_timed_rejects_bad_parameters_before_any_io(
    kwargs: dict[str, str], voice_code: VoiceErrorCode
) -> None:
    factory = FakeFactory()

    with pytest.raises(SpeechSynthesisError) as info:
        adapter_with(factory).synthesize_timed(make_request(), **kwargs)

    assert info.value.voice_code is voice_code
    assert factory.calls == []


def test_synthesize_timed_rejects_a_bad_voice_name() -> None:
    factory = FakeFactory()

    with pytest.raises(SpeechSynthesisError) as info:
        adapter_with(factory).synthesize_timed(make_request(voice="mai"))

    assert info.value.voice_code is CODE.VOICE_NOT_FOUND
    assert factory.calls == []


def test_synthesize_timed_maps_failures_like_synthesize(mp3: bytes) -> None:
    no_audio = FakeFactory(chunks=[word_event("a", 1, 1)])
    ctor = FakeFactory(ctor_error=ValueError("bad rate"))
    stream = FakeFactory(chunks=timed_chunks(mp3), stream_error=TimeoutError())

    codes = []
    for factory in (no_audio, ctor, stream):
        with pytest.raises(SpeechSynthesisError) as info:
            adapter_with(factory).synthesize_timed(make_request())
        codes.append(info.value.voice_code)

    assert codes == [CODE.UNAVAILABLE, CODE.INVALID_REQUEST, CODE.TIMEOUT]


def test_synthesize_timed_never_uses_the_fallback(mp3: bytes) -> None:
    adapter = adapter_with(
        FakeFactory(stream_error=TimeoutError()),
        fallback=MockSpeechSynthesizer(),
    )

    with pytest.raises(SpeechSynthesisError):
        adapter.synthesize_timed(make_request())


def test_synthesize_timed_works_inside_a_running_event_loop(mp3: bytes) -> None:
    adapter = adapter_with(FakeFactory(chunks=timed_chunks(mp3)))

    async def inside_loop() -> int:
        return len(adapter.synthesize_timed(make_request()).boundaries)

    assert asyncio.run(inside_loop()) == 2


def test_synthesize_timed_stops_a_stream_that_never_ends() -> None:
    adapter = adapter_with(FakeFactory(hang=True), overall_timeout_seconds=0.05)

    with pytest.raises(SpeechSynthesisError) as info:
        adapter.synthesize_timed(make_request())

    assert info.value.voice_code is CODE.TIMEOUT


def test_the_real_communicate_accepts_the_timed_arguments_without_network() -> None:
    pytest.importorskip("edge_tts")
    factory: Callable[..., Any] = EdgeTTSAdapter()._resolve_factory()

    communicate = factory(
        TEXT,
        VOICE,
        rate="-6%",
        volume="+0%",
        pitch="-4Hz",
        boundary="WordBoundary",
        connect_timeout=8,
        receive_timeout=60,
    )

    assert communicate is not None
