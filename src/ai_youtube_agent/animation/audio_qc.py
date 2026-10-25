"""Audio quality control of the film: loudness, peaks, clipping, ducking.

Loudness comes from ffmpeg's ``ebur128`` on the final MP4; the sample-level checks
(peak, clipped samples, windowed RMS, digital silence, bed against voice, duck
depth) use the standard ``wave`` module on the decoded audio and the stems. All
measurement functions are pure; only ``measure_loudness`` and ``decode_audio``
start ffmpeg.
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
import wave
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.errors import AnimationError

Window = tuple[float, float]  # (start_ms, end_ms)

TARGET_LUFS: Final[float] = -14.0
LUFS_TOLERANCE: Final[float] = 1.5
MAX_TRUE_PEAK_DBTP: Final[float] = -1.0
MAX_LRA: Final[float] = 11.0
MIN_BED_BELOW_VOICE_DB: Final[float] = 14.0
MIN_DUCK_DB: Final[float] = 6.0
DUCK_MIN_GAP_MS: Final[int] = 300
MAX_DIGITAL_SILENCE_MS: Final[int] = 400
RMS_WINDOW_MS: Final[int] = 50
SILENCE_LEVEL: Final[int] = 1  # |sample| <= 1 LSB counts as digital silence

RunProcess = Callable[..., Any]


@dataclass(frozen=True)
class LoudnessReport:
    integrated_lufs: float
    true_peak_dbtp: float
    lra: float

    def as_dict(self) -> dict[str, float]:
        return {
            "integrated_lufs": self.integrated_lufs,
            "true_peak_dbtp": self.true_peak_dbtp,
            "lra": self.lra,
        }


@dataclass
class AudioQcResult:
    """Every measured number plus the list of problems (empty when all is well)."""

    loudness: LoudnessReport | None = None
    peak_dbfs: float | None = None
    clipped_samples: int | None = None
    longest_silence_ms: float | None = None
    bed_below_voice_db: float | None = None
    duck_depth_db: float | None = None
    duck_gaps_measured: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "loudness": self.loudness.as_dict() if self.loudness else None,
            "peak_dbfs": _round(self.peak_dbfs),
            "clipped_samples": self.clipped_samples,
            "longest_silence_ms": _round(self.longest_silence_ms),
            "bed_below_voice_db": _round(self.bed_below_voice_db),
            "duck_depth_db": _round(self.duck_depth_db),
            "duck_gaps_measured": self.duck_gaps_measured,
            "thresholds": {
                "target_lufs": TARGET_LUFS,
                "lufs_tolerance": LUFS_TOLERANCE,
                "max_true_peak_dbtp": MAX_TRUE_PEAK_DBTP,
                "max_lra": MAX_LRA,
                "min_bed_below_voice_db": MIN_BED_BELOW_VOICE_DB,
                "min_duck_db": MIN_DUCK_DB,
                "duck_min_gap_ms": DUCK_MIN_GAP_MS,
                "max_digital_silence_ms": MAX_DIGITAL_SILENCE_MS,
            },
            "problems": list(self.problems),
        }


def _round(value: float | None) -> float | None:
    if value is None or math.isinf(value) or math.isnan(value):
        return None
    return round(value, 2)


# ---------------------------------------------------------------------------
# ffmpeg ebur128
# ---------------------------------------------------------------------------

_NUMBER = r"(-?\d+(?:\.\d+)?|-?inf)"


def _value(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    if match is None:
        return None
    raw = match.group(1)
    return -math.inf if raw == "-inf" else float(raw)


def parse_ebur128(stderr: str) -> LoudnessReport:
    """Integrated loudness, true peak and LRA from the ``Summary`` of ebur128."""
    marker = stderr.rfind("Summary:")
    summary = stderr[marker:] if marker >= 0 else stderr
    integrated = _value(rf"\bI:\s+{_NUMBER}\s+LUFS", summary)
    lra = _value(rf"\bLRA:\s+{_NUMBER}\s+LU", summary)
    peak = _value(rf"True peak:\s*\n?\s*Peak:\s+{_NUMBER}\s+dBFS", summary)
    if integrated is None or lra is None or peak is None:
        raise AnimationError("ffmpeg ebur128 printed no loudness summary")
    return LoudnessReport(integrated, peak, lra)


def measure_loudness(
    path: Path, ffmpeg_binary: str, *, run: RunProcess = subprocess.run
) -> LoudnessReport:
    """Loudness of the audio of ``path`` (a WAV or the MP4)."""
    proc = run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-vn",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AnimationError(f"ffmpeg ebur128 failed: {str(proc.stderr)[-300:]}")
    return parse_ebur128(str(proc.stderr))


def decode_audio(
    path: Path,
    out_wav: Path,
    ffmpeg_binary: str,
    *,
    rate: int = 48000,
    run: RunProcess = subprocess.run,
) -> Path:
    """Decode the audio stream of ``path`` to a 16-bit stereo WAV for the checks."""
    proc = run(
        [
            ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(rate),
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not out_wav.exists():
        raise AnimationError(
            f"ffmpeg could not decode the audio: {str(proc.stderr)[-300:]}"
        )
    return out_wav


# ---------------------------------------------------------------------------
# Sample level (16-bit PCM)
# ---------------------------------------------------------------------------


def read_pcm16(path: Path) -> tuple[list[array], int]:
    """The channels (``array('h')``) and the sample rate of a 16-bit WAV."""
    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != 2:
            raise AnimationError(f"{path.name} is not 16-bit PCM")
        channels = reader.getnchannels()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    samples: array = array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return [samples[c::channels] for c in range(channels)], rate


def peak_dbfs(samples: Sequence[int]) -> float:
    peak = max((abs(v) for v in samples), default=0)
    return -math.inf if peak == 0 else 20 * math.log10(peak / 32768)


def clipped_samples(samples: Sequence[int]) -> int:
    """Samples at full scale (the rails of 16-bit PCM)."""
    return sum(1 for v in samples if v >= 32767 or v <= -32768)


def windowed_rms_db(
    samples: Sequence[float], rate: int, window_ms: int = RMS_WINDOW_MS
) -> list[float]:
    """RMS in dBFS (full scale 32768) of consecutive windows; silence is ``-inf``."""
    size = max(1, rate * window_ms // 1000)
    levels: list[float] = []
    for begin in range(0, len(samples) - size + 1, size):
        chunk = samples[begin : begin + size]
        power = sum(v * v for v in chunk) / size
        levels.append(-math.inf if power <= 0 else 10 * math.log10(power / 32768**2))
    return levels


def longest_silence_ms(samples: Sequence[int], rate: int) -> float:
    """The longest run of digital silence (all samples within one LSB of zero)."""
    longest = run = 0
    for value in samples:
        if -SILENCE_LEVEL <= value <= SILENCE_LEVEL:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest * 1000 / rate


def mono_mix(channels: Sequence[Sequence[int]]) -> array:
    """Average of the channels as floats (``array('d')``)."""
    if len(channels) == 1:
        return array("d", channels[0])
    count = min(len(c) for c in channels)
    return array(
        "d", [sum(c[i] for c in channels) / len(channels) for i in range(count)]
    )


def _speech_levels(
    levels: Sequence[float], window_ms: int, windows: Sequence[Window]
) -> list[float]:
    selected: list[float] = []
    for start, end in windows:
        first = max(0, math.ceil(start / window_ms))
        last = min(len(levels), int(end / window_ms))
        selected.extend(v for v in levels[first:last] if math.isfinite(v))
    return selected


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return -math.inf
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _power_mean_db(levels: Sequence[float]) -> float:
    if not levels:
        return -math.inf
    return 10 * math.log10(sum(10 ** (v / 10) for v in levels) / len(levels))


def bed_vs_voice_db(
    voice: Sequence[float],
    voice_rate: int,
    bed: Sequence[float],
    bed_rate: int,
    windows: Sequence[Window],
) -> float:
    """How far (dB) the bed is below the voice, averaged over the speech windows."""
    voice_levels = _speech_levels(
        windowed_rms_db(voice, voice_rate), RMS_WINDOW_MS, windows
    )
    bed_levels = _speech_levels(windowed_rms_db(bed, bed_rate), RMS_WINDOW_MS, windows)
    if not voice_levels or not bed_levels:
        raise AnimationError("no speech window to compare the bed with the voice")
    return _power_mean_db(voice_levels) - _power_mean_db(bed_levels)


def speech_gaps(
    windows: Sequence[Window], total_ms: float, min_gap_ms: float = DUCK_MIN_GAP_MS
) -> list[Window]:
    """The silent stretches between speech windows (and the head and tail) that
    last at least ``min_gap_ms``."""
    gaps: list[Window] = []
    cursor = 0.0
    for start, end in sorted(windows):
        if start - cursor >= min_gap_ms:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if total_ms - cursor >= min_gap_ms:
        gaps.append((cursor, total_ms))
    return gaps


def duck_depth_db(
    bed: Sequence[float],
    rate: int,
    windows: Sequence[Window],
    total_ms: float,
    min_gap_ms: float = DUCK_MIN_GAP_MS,
) -> tuple[float, int]:
    """``(depth, gaps measured)``: how much louder the bed is, at its loudest
    point inside a gap of at least ``min_gap_ms``, than during speech (median of
    the gaps; a depth of 9 dB means the music comes back 9 dB in the pauses)."""
    levels = windowed_rms_db(bed, rate)
    speech = _speech_levels(levels, RMS_WINDOW_MS, windows)
    if not speech:
        raise AnimationError("no speech window to measure the duck depth")
    speech_db = _power_mean_db(speech)
    depths: list[float] = []
    for start, end in speech_gaps(windows, total_ms, min_gap_ms):
        # The first window of the gap is still ducked and the last one is already
        # ducking again for the next line, so only the loud middle counts.
        inside = _speech_levels(levels, RMS_WINDOW_MS, [(start, end)])
        if inside:
            depths.append(max(inside) - speech_db)
    if not depths:
        raise AnimationError("no gap long enough to measure the duck depth")
    return _median(depths), len(depths)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def evaluate_loudness(report: LoudnessReport, result: AudioQcResult) -> None:
    result.loudness = report
    if abs(report.integrated_lufs - TARGET_LUFS) > LUFS_TOLERANCE:
        result.problems.append(
            f"integrated loudness {report.integrated_lufs:.1f} LUFS is more than "
            f"{LUFS_TOLERANCE:g} LU from {TARGET_LUFS:g}"
        )
    if report.true_peak_dbtp > MAX_TRUE_PEAK_DBTP:
        result.problems.append(
            f"true peak {report.true_peak_dbtp:.1f} dBTP is above "
            f"{MAX_TRUE_PEAK_DBTP:g}"
        )
    if report.lra > MAX_LRA:
        result.problems.append(
            f"loudness range {report.lra:.1f} LU is above {MAX_LRA:g}"
        )


def evaluate_samples(
    channels: Sequence[Sequence[int]], rate: int, result: AudioQcResult
) -> None:
    """Peak, clipping and digital silence of the decoded final audio."""
    result.peak_dbfs = max(peak_dbfs(c) for c in channels)
    result.clipped_samples = sum(clipped_samples(c) for c in channels)
    result.longest_silence_ms = max(longest_silence_ms(c, rate) for c in channels)
    if result.clipped_samples:
        result.problems.append(f"{result.clipped_samples} clipped samples")
    if result.longest_silence_ms > MAX_DIGITAL_SILENCE_MS:
        result.problems.append(
            f"digital silence of {result.longest_silence_ms:.0f} ms "
            f"(limit {MAX_DIGITAL_SILENCE_MS})"
        )


def evaluate_mix_balance(
    voice: Sequence[float],
    voice_rate: int,
    bed: Sequence[float],
    bed_rate: int,
    windows: Sequence[Window],
    total_ms: float,
    result: AudioQcResult,
) -> None:
    """Bed against voice and duck depth from the stems as they were mixed."""
    result.bed_below_voice_db = bed_vs_voice_db(
        voice, voice_rate, bed, bed_rate, windows
    )
    if result.bed_below_voice_db < MIN_BED_BELOW_VOICE_DB:
        result.problems.append(
            f"the bed is only {result.bed_below_voice_db:.1f} dB below the voice "
            f"during speech (need {MIN_BED_BELOW_VOICE_DB:g})"
        )
    result.duck_depth_db, result.duck_gaps_measured = duck_depth_db(
        bed, bed_rate, windows, total_ms
    )
    if result.duck_depth_db < MIN_DUCK_DB:
        result.problems.append(
            f"the bed comes back only {result.duck_depth_db:.1f} dB in the pauses "
            f"(need {MIN_DUCK_DB:g})"
        )
