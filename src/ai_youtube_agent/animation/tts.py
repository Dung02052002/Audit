"""Voice: one Edge TTS request per storyboard line, timed and cleaned.

For each line the MP3 and its word boundaries come from Edge (cached on disk by
text, voice, rate and pitch), the MP3 becomes a 24 kHz mono WAV through the voice
chain (high-pass, light compression, presence), leading and trailing silence is
trimmed and the word times shift with it. If Edge returns no usable word
boundaries the words are timed in proportion to their letters and the line is
marked ``sentence_estimate`` instead of ``word_boundary``. Nothing is ever faked.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import time
import wave
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.storyboard import NarrationLine, Storyboard
from ai_youtube_agent.animation.subtitles import (
    TIMING_SENTENCE_ESTIMATE,
    TimingEvent,
    WordTiming,
    align_boundaries,
)
from ai_youtube_agent.providers.media_probe import MediaProbeError, probe_mp3_bytes
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    VoiceErrorCode,
)

SAMPLE_RATE: Final[int] = 24000
EDGE_ATTEMPTS: Final[int] = 3
SILENCE_DB: Final[float] = -45.0
SAFETY_MS: Final[int] = 30
WINDOW_MS: Final[int] = 5
# Measured live with vi-VN HoaiMy -6% and NamMinh -10%: 3.0 to 4.1 syllables per
# second between the first and the last word of a line.
SYLLABLES_PER_SECOND_WINDOW: Final[tuple[float, float]] = (2.8, 5.0)
VOICE_CHAIN: Final[str] = (
    "highpass=f=70,acompressor=ratio=2.5,equalizer=f=3000:t=q:w=1:g=2"
)
CACHE_VERSION: Final[str] = "tts-v1"
# Edge sometimes answers "no audio" to a good request (seen live); a retry works.
_NO_RETRY: Final[frozenset[VoiceErrorCode]] = frozenset(
    {VoiceErrorCode.VOICE_NOT_FOUND, VoiceErrorCode.INVALID_REQUEST}
)

RunProcess = Callable[..., Any]


@dataclass(frozen=True)
class SpokenLine:
    """A synthesized line: a trimmed WAV and its words, times relative to the WAV."""

    line_id: str
    text: str
    voice_id: str
    wav_path: Path
    duration_ms: int
    words: tuple[WordTiming, ...]
    timing_source: str
    lead_trim_ms: int = 0
    rate: str = "+0%"
    pitch: str = "+0Hz"
    cache_key: str = ""
    cache_hit: bool = False
    syllables_per_second: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "voice_id": self.voice_id,
            "rate": self.rate,
            "pitch": self.pitch,
            "duration_ms": self.duration_ms,
            "timing_source": self.timing_source,
            "lead_trim_ms": self.lead_trim_ms,
            "syllables_per_second": round(self.syllables_per_second, 2),
            "cache_hit": self.cache_hit,
            "wav": str(self.wav_path),
        }


def cache_key(text: str, voice_id: str, rate: str, pitch: str) -> str:
    """Stable key of one Edge request (text, voice, rate, pitch)."""
    material = "\x1f".join((CACHE_VERSION, text, voice_id, rate, pitch))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def speech_rate(words: Sequence[WordTiming]) -> float:
    """Syllables per second between the first and the last word."""
    if not words:
        return 0.0
    span = words[-1].end_ms - words[0].start_ms
    return len(words) / (span / 1000) if span > 0 else 0.0


def speech_rate_problem(
    line: SpokenLine, window: tuple[float, float] = SYLLABLES_PER_SECOND_WINDOW
) -> str | None:
    """A message when the line is read much too fast or too slow, else ``None``."""
    low, high = window
    if low <= line.syllables_per_second <= high:
        return None
    return (
        f"line '{line.line_id}' is read at {line.syllables_per_second:.2f} "
        f"syllables/s, outside {low:g}-{high:g}"
    )


# ---------------------------------------------------------------------------
# Silence trimming (pure Python, 16-bit mono)
# ---------------------------------------------------------------------------


def read_wav_samples(path: Path) -> tuple[array[int], int]:
    """The samples (first channel) and sample rate of a 16-bit PCM WAV."""
    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != 2:
            raise AnimationError(f"{path.name} is not 16-bit PCM")
        channels = reader.getnchannels()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    samples: array[int] = array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = samples[::channels]
    return samples, rate


def write_wav_samples(path: Path, samples: array[int], rate: int) -> Path:
    data = array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(data.tobytes())
    return path


def trim_bounds(
    samples: Sequence[int],
    rate: int,
    *,
    threshold_db: float = SILENCE_DB,
    safety_ms: int = SAFETY_MS,
    window_ms: int = WINDOW_MS,
) -> tuple[int, int]:
    """``(start, end)`` sample indexes that drop the silence around the voice.

    A window is sound when its peak is above ``threshold_db`` (dBFS); ``safety_ms``
    of audio is kept before the first and after the last sound window.
    """
    window = max(1, rate * window_ms // 1000)
    threshold = 32768 * 10 ** (threshold_db / 20)
    first = last = -1
    for begin in range(0, len(samples), window):
        peak = max((abs(v) for v in samples[begin : begin + window]), default=0)
        if peak > threshold:
            if first < 0:
                first = begin
            last = min(begin + window, len(samples))
    if first < 0:
        raise AnimationError("the synthesized audio is silent")
    safety = rate * safety_ms // 1000
    return max(0, first - safety), min(len(samples), last + safety)


def peak_dbfs(samples: Sequence[int]) -> float:
    peak = max((abs(v) for v in samples), default=0)
    return -math.inf if peak == 0 else 20 * math.log10(peak / 32768)


def process_audio(
    ffmpeg_binary: str,
    mp3_path: Path,
    wav_path: Path,
    *,
    run: RunProcess = subprocess.run,
) -> tuple[int, int, int]:
    """MP3 to the cleaned, trimmed WAV; returns ``(duration_ms, lead_ms, raw_ms)``.

    ``lead_ms`` is the audio removed from the start (word times shift by it);
    ``raw_ms`` is the length of the WAV before trimming.
    """
    decoded = wav_path.with_name(wav_path.stem + ".decoded.wav")
    command = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(mp3_path),
        "-af",
        VOICE_CHAIN,
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(decoded),
    ]
    proc = run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not decoded.exists():
        raise AnimationError(
            f"ffmpeg could not decode {mp3_path.name}: {str(proc.stderr).strip()}"
        )
    try:
        samples, rate = read_wav_samples(decoded)
        start, end = trim_bounds(samples, rate)
        write_wav_samples(wav_path, samples[start:end], rate)
    finally:
        decoded.unlink(missing_ok=True)
    return (
        round((end - start) * 1000 / rate),
        round(start * 1000 / rate),
        round(len(samples) * 1000 / rate),
    )


# ---------------------------------------------------------------------------
# Edge requests and the cache
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def _load_cached(cache_dir: Path, key: str) -> tuple[bytes, list[TimingEvent]] | None:
    mp3_path, meta_path = cache_dir / f"{key}.mp3", cache_dir / f"{key}.json"
    if not (mp3_path.is_file() and meta_path.is_file()):
        return None
    try:
        audio = mp3_path.read_bytes()
        probe_mp3_bytes(audio)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        events = [
            TimingEvent(str(k), int(s), int(e), str(t))
            for k, s, e, t in meta["boundaries"]
        ]
    except (OSError, ValueError, KeyError, TypeError, MediaProbeError):
        return None
    return audio, events


def _store_cached(
    cache_dir: Path, key: str, audio: bytes, events: Sequence[Any], voice: str
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "voice_id": voice,
        "boundaries": [[e.kind, e.start_ms, e.end_ms, e.text] for e in events],
    }
    _write_atomic(cache_dir / f"{key}.mp3", audio)
    _write_atomic(
        cache_dir / f"{key}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8")
    )


def _request_edge(
    edge: Any,
    line: NarrationLine,
    storyboard: Storyboard,
    *,
    attempts: int,
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> tuple[bytes, list[Any]]:
    voice = storyboard.voice(line.voice)
    request = SpeechRequest(
        text=line.text, voice_id=voice.voice_id, language=storyboard.language
    )
    for attempt in range(1, attempts + 1):
        try:
            timed = edge.synthesize_timed(
                request, rate=voice.rate, pitch=voice.pitch, boundary="WordBoundary"
            )
        except SpeechSynthesisError as exc:
            if exc.voice_code in _NO_RETRY or attempt == attempts:
                raise AnimationError(
                    f"Edge TTS failed for line '{line.id}' after {attempt} "
                    f"attempt(s): {exc}"
                ) from exc
            log(f"         voice: {line.id} attempt {attempt} failed ({exc}); retry")
            sleep(1.5 * attempt)
        else:
            return timed.audio, list(timed.boundaries)
    raise AssertionError("unreachable: the loop returns or raises")


def synthesize_lines(
    edge: Any,
    storyboard: Storyboard,
    ffmpeg_binary: str,
    out_dir: Path,
    cache_dir: Path,
    *,
    attempts: int = EDGE_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    run: RunProcess = subprocess.run,
) -> dict[str, SpokenLine]:
    """Synthesize every line of ``storyboard`` and return them by line id.

    One Edge request per line (3 attempts with a growing pause); a line whose
    text, voice, rate and pitch were synthesized before is read from
    ``cache_dir``. WAVs are written to ``out_dir`` as ``<line id>.wav``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    spoken: dict[str, SpokenLine] = {}
    for line in storyboard.all_lines():
        voice = storyboard.voice(line.voice)
        key = cache_key(line.text, voice.voice_id, voice.rate, voice.pitch)
        cached = _load_cached(cache_dir, key)
        if cached is None:
            audio, events = _request_edge(
                edge, line, storyboard, attempts=attempts, sleep=sleep, log=log
            )
            _store_cached(cache_dir, key, audio, events, voice.voice_id)
            events = [TimingEvent(e.kind, e.start_ms, e.end_ms, e.text) for e in events]
            hit = False
        else:
            audio, events = cached
            hit = True
        mp3_path = cache_dir / f"{key}.mp3"
        wav_path = out_dir / f"{line.id}.wav"
        duration_ms, lead_ms, raw_ms = process_audio(
            ffmpeg_binary, mp3_path, wav_path, run=run
        )
        words, source = _time_words(line, events, raw_ms, lead_ms, duration_ms)
        spoken[line.id] = SpokenLine(
            line_id=line.id,
            text=line.text,
            voice_id=voice.voice_id,
            wav_path=wav_path,
            duration_ms=duration_ms,
            words=words,
            timing_source=source,
            lead_trim_ms=lead_ms,
            rate=voice.rate,
            pitch=voice.pitch,
            cache_key=key,
            cache_hit=hit,
            syllables_per_second=speech_rate(words),
        )
        log(
            f"         voice: {line.id} {duration_ms} ms, {source}, "
            f"{spoken[line.id].syllables_per_second:.2f} syl/s"
            f"{' (cache)' if hit else ''}"
        )
    return spoken


def _time_words(
    line: NarrationLine,
    events: Sequence[TimingEvent],
    raw_ms: int,
    lead_ms: int,
    duration_ms: int,
) -> tuple[tuple[WordTiming, ...], str]:
    """Words aligned to the Edge events, shifted to the trimmed WAV."""
    words, source = align_boundaries(line.text, events, total_ms=raw_ms)
    if source == TIMING_SENTENCE_ESTIMATE and not events:
        # No events at all: spread the words over the voiced part of the audio.
        voiced = [
            TimingEvent("SentenceBoundary", lead_ms, lead_ms + duration_ms, line.text)
        ]
        words, source = align_boundaries(line.text, voiced)
    shifted: list[WordTiming] = []
    for word in words:
        start = min(max(0, word.start_ms - lead_ms), duration_ms - 1)
        end = min(max(start + 1, word.end_ms - lead_ms), duration_ms)
        shifted.append(WordTiming(word.index, word.text, start, end))
    return tuple(shifted), source
