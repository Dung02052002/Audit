"""Animation voice: Edge synthesis per line, cache, retries, trimming (fake adapter)."""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.storyboard import text_tokens
from ai_youtube_agent.animation.tts import (
    SpokenLine,
    cache_key,
    process_audio,
    read_wav_samples,
    speech_rate,
    speech_rate_problem,
    synthesize_lines,
    trim_bounds,
)
from ai_youtube_agent.providers.registry.tts_adapters import (
    SpeechBoundary,
    TimedSpeech,
)
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    SynthesizedSpeech,
    VoiceErrorCode,
)
from animation_fixtures import shipped_storyboard
from media_fixtures import make_mp3

RATE = 24000
SILENCE_BEFORE_MS, VOICED_MS, SILENCE_AFTER_MS = 200, 1000, 300
WORD_STEP_MS = 100


def write_tone_wav(
    path: Path,
    *,
    before_ms: int = SILENCE_BEFORE_MS,
    voiced_ms: int = VOICED_MS,
    after_ms: int = SILENCE_AFTER_MS,
) -> None:
    def samples(ms: int, amplitude: float):
        for i in range(RATE * ms // 1000):
            yield round(amplitude * math.sin(2 * math.pi * 220 * i / RATE) * 32000)

    frames = [
        *samples(before_ms, 0.0),
        *samples(voiced_ms, 0.5),
        *samples(after_ms, 0.0),
    ]
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(struct.pack(f"<{len(frames)}h", *frames))


class FakeFfmpeg:
    """Stands in for ``subprocess.run``: writes a tone WAV where ffmpeg would."""

    def __init__(self, returncode: int = 0, **tone: int) -> None:
        self.returncode = returncode
        self.tone = tone
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: Any) -> Any:
        self.commands.append(command)
        if self.returncode == 0:
            write_tone_wav(Path(command[-1]), **self.tone)
        return SimpleNamespace(returncode=self.returncode, stderr="boom", stdout="")


@dataclass
class FakeEdge:
    """Records ``synthesize_timed`` calls; word boundaries 100 ms apart."""

    boundaries: str = "words"  # words | sentence | none
    failures: list[BaseException] = field(default_factory=list)
    calls: list[tuple[SpeechRequest, dict[str, str]]] = field(default_factory=list)

    def synthesize_timed(self, request: SpeechRequest, **kwargs: str) -> TimedSpeech:
        self.calls.append((request, kwargs))
        if self.failures:
            raise self.failures.pop(0)
        audio = make_mp3(40)
        if self.boundaries == "words":
            events = tuple(
                SpeechBoundary(
                    "WordBoundary",
                    SILENCE_BEFORE_MS + 50 + i * WORD_STEP_MS,
                    SILENCE_BEFORE_MS + 50 + i * WORD_STEP_MS + WORD_STEP_MS - 20,
                    "".join(c for c in token if c.isalnum()),
                )
                for i, token in enumerate(text_tokens(request.text))
            )
        elif self.boundaries == "sentence":
            events = (
                SpeechBoundary(
                    "SentenceBoundary",
                    SILENCE_BEFORE_MS,
                    SILENCE_BEFORE_MS + VOICED_MS,
                    request.text,
                ),
            )
        else:
            events = ()
        speech = SynthesizedSpeech(
            audio=audio,
            media_type="audio/mpeg",
            duration_ms=960,
            provider="edge_tts",
            voice_id=request.voice_id,
            characters=len(request.text),
            generated_at=datetime.now(UTC),
        )
        return TimedSpeech(
            speech, events, "WordBoundary", kwargs["rate"], kwargs["pitch"]
        )


def error(code: VoiceErrorCode) -> SpeechSynthesisError:
    return SpeechSynthesisError(code, "boom", provider="edge_tts")


def run(edge: FakeEdge, tmp_path: Path, **kwargs: Any) -> dict[str, SpokenLine]:
    kwargs.setdefault("run", FakeFfmpeg())
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("log", lambda _m: None)
    return synthesize_lines(
        edge,
        shipped_storyboard(),
        "ffmpeg",
        tmp_path / "out",
        tmp_path / "cache",
        **kwargs,
    )


# --- one request per line with the decided voices ------------------------------


def test_every_line_gets_one_request_with_its_voice_rate_and_pitch(tmp_path) -> None:
    edge = FakeEdge()

    spoken = run(edge, tmp_path)

    assert list(spoken) == ["n1", "n2", "n3", "n4", "c1", "n5", "n6", "c2", "n7"]
    assert len(edge.calls) == 9
    by_text = {request.text: (request, kw) for request, kw in edge.calls}
    narrator, narrator_kw = by_text["Sáng nay, quán của Mai vắng khách."]
    customer, customer_kw = by_text["Ngon như ngày xưa đấy!"]
    assert (narrator.voice_id, narrator.language) == ("vi-VN-HoaiMyNeural", "vi-VN")
    assert narrator_kw == {"rate": "-6%", "pitch": "+0Hz", "boundary": "WordBoundary"}
    assert customer.voice_id == "vi-VN-NamMinhNeural"
    assert customer_kw == {"rate": "-10%", "pitch": "-4Hz", "boundary": "WordBoundary"}
    assert spoken["c1"].rate == "-10%" and spoken["c1"].pitch == "-4Hz"


def test_words_come_from_the_word_boundaries_and_follow_the_trim(tmp_path) -> None:
    spoken = run(FakeEdge(), tmp_path)

    n1 = spoken["n1"]
    # 200 ms of silence, safety 30 ms: 170 ms are removed from the start.
    assert n1.lead_trim_ms == 170
    assert n1.timing_source == "word_boundary"
    assert [w.text for w in n1.words] == text_tokens(n1.text)
    assert n1.words[0].start_ms == 200 + 50 - 170
    assert n1.words[1].start_ms - n1.words[0].start_ms == WORD_STEP_MS
    assert n1.duration_ms == VOICED_MS + 2 * 30
    assert n1.words[-1].end_ms <= n1.duration_ms
    assert n1.syllables_per_second == pytest.approx(speech_rate(n1.words))


def test_trimmed_wav_is_written_and_has_the_reported_duration(tmp_path) -> None:
    spoken = run(FakeEdge(), tmp_path)

    path = spoken["n3"].wav_path
    samples, rate = read_wav_samples(path)
    assert path == tmp_path / "out" / "n3.wav"
    assert rate == RATE
    assert round(len(samples) * 1000 / rate) == spoken["n3"].duration_ms
    assert max(abs(v) for v in samples[:10]) < 200  # starts in near-silence
    assert not list((tmp_path / "out").glob("*.decoded.wav"))


def test_the_voice_chain_is_applied_to_24k_mono(tmp_path) -> None:
    ffmpeg = FakeFfmpeg()

    run(FakeEdge(), tmp_path, run=ffmpeg)

    command = ffmpeg.commands[0]
    chain = command[command.index("-af") + 1]
    assert "highpass=f=70" in chain
    assert "acompressor=ratio=2.5" in chain
    assert "equalizer=f=3000" in chain
    assert command[command.index("-ar") + 1] == "24000"
    assert command[command.index("-ac") + 1] == "1"


# --- cache ---------------------------------------------------------------------


def test_a_second_run_reads_the_cache_without_calling_edge(tmp_path) -> None:
    first = FakeEdge()
    run(first, tmp_path)
    second = FakeEdge()

    spoken = run(second, tmp_path)

    assert len(first.calls) == 9 and second.calls == []
    assert all(line.cache_hit for line in spoken.values())
    assert len(list((tmp_path / "cache").glob("*.mp3"))) == 9
    assert len(list((tmp_path / "cache").glob("*.json"))) == 9


def test_cache_key_depends_on_text_voice_rate_and_pitch() -> None:
    base = cache_key("Xin chào", "vi-VN-HoaiMyNeural", "-6%", "+0Hz")

    assert base == cache_key("Xin chào", "vi-VN-HoaiMyNeural", "-6%", "+0Hz")
    assert (
        len(
            {
                base,
                cache_key("Xin chao", "vi-VN-HoaiMyNeural", "-6%", "+0Hz"),
                cache_key("Xin chào", "vi-VN-NamMinhNeural", "-6%", "+0Hz"),
                cache_key("Xin chào", "vi-VN-HoaiMyNeural", "-10%", "+0Hz"),
                cache_key("Xin chào", "vi-VN-HoaiMyNeural", "-6%", "-4Hz"),
            }
        )
        == 5
    )


def test_a_corrupt_cache_entry_is_synthesized_again(tmp_path) -> None:
    run(FakeEdge(), tmp_path)
    mp3 = next((tmp_path / "cache").glob("*.mp3"))
    mp3.write_bytes(b"not an mp3")
    edge = FakeEdge()

    run(edge, tmp_path)

    assert len(edge.calls) == 1


# --- retries -------------------------------------------------------------------


def test_a_transient_failure_is_retried_including_no_audio(tmp_path) -> None:
    edge = FakeEdge(
        failures=[error(VoiceErrorCode.REFUSED), error(VoiceErrorCode.UNAVAILABLE)]
    )
    sleeps: list[float] = []

    spoken = run(edge, tmp_path, sleep=sleeps.append)

    assert len(edge.calls) == 9 + 2
    assert sleeps == [1.5, 3.0]
    assert spoken["n1"].duration_ms > 0


def test_three_failures_raise_an_animation_error(tmp_path) -> None:
    edge = FakeEdge(failures=[error(VoiceErrorCode.TIMEOUT)] * 3)

    with pytest.raises(AnimationError, match="line 'n1' after 3 attempt"):
        run(edge, tmp_path)

    assert len(edge.calls) == 3


def test_a_bad_voice_is_not_retried(tmp_path) -> None:
    edge = FakeEdge(failures=[error(VoiceErrorCode.VOICE_NOT_FOUND)])

    with pytest.raises(AnimationError, match="after 1 attempt"):
        run(edge, tmp_path)

    assert len(edge.calls) == 1


# --- fallback timing -----------------------------------------------------------


def test_sentence_boundaries_give_a_flagged_estimate(tmp_path) -> None:
    spoken = run(FakeEdge(boundaries="sentence"), tmp_path)

    n1 = spoken["n1"]
    assert n1.timing_source == "sentence_estimate"
    assert len(n1.words) == 7
    assert n1.words[0].start_ms == 30  # the voiced span starts 30 ms into the WAV
    assert n1.words[-1].end_ms == 1030  # and ends where the sentence event ends


def test_no_boundaries_at_all_spread_the_words_over_the_voice(tmp_path) -> None:
    spoken = run(FakeEdge(boundaries="none"), tmp_path)

    n2 = spoken["n2"]
    assert n2.timing_source == "sentence_estimate"
    assert n2.words[0].start_ms == 0
    assert n2.words[-1].end_ms == n2.duration_ms


# --- audio processing ----------------------------------------------------------


def test_trim_bounds_keep_a_safety_margin() -> None:
    write = [0] * 2400 + [10000] * 4800 + [0] * 3600  # 100 / 200 / 150 ms at 24 kHz

    start, end = trim_bounds(write, RATE)

    assert start == 2400 - 720
    assert end == 2400 + 4800 + 720


def test_trim_ignores_a_noise_floor_below_minus_45_dbfs() -> None:
    floor = [50, -50] * 1200  # about -56 dBFS
    voiced = [12000] * 2400

    start, end = trim_bounds(floor + voiced + floor, RATE)

    assert start == len(floor) - 720
    assert end == len(floor) + len(voiced) + 720


def test_silent_audio_is_an_error() -> None:
    with pytest.raises(AnimationError, match="silent"):
        trim_bounds([0] * 4800, RATE)


def test_ffmpeg_failure_is_an_error(tmp_path) -> None:
    mp3 = tmp_path / "x.mp3"
    mp3.write_bytes(make_mp3(10))

    with pytest.raises(AnimationError, match="ffmpeg could not decode"):
        process_audio("ffmpeg", mp3, tmp_path / "x.wav", run=FakeFfmpeg(returncode=1))


# --- speech rate ---------------------------------------------------------------


def test_speech_rate_window(tmp_path) -> None:
    spoken = run(FakeEdge(), tmp_path)
    line = spoken["n1"]  # 7 words 100 ms apart: about 10.9 syllables per second

    assert speech_rate_problem(line) is not None
    assert speech_rate_problem(line, (2.0, 12.0)) is None
    assert "line 'n1' is read at" in speech_rate_problem(line)
    assert speech_rate(()) == 0.0
