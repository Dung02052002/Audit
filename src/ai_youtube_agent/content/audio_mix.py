"""Audio mix engine (Prompt Pack v8, prompt #099), context C8 Shorts Production.

``mix_audio`` combines the voice with music and sound effects under
``MixConstraints``. It is pure, deterministic (the same inputs give the same
bytes) and uses only the standard library (``wave``, ``array``, ``math``). The
rules were decided with the user on 2026-10-17:

- The voice is the master: the output has the sample rate, channel count and
  exact length of the voice. Every other track is converted to that format
  (mono is duplicated to stereo, stereo is averaged to mono, the rate is
  changed by linear interpolation). Only PCM WAV of 8, 16, 24 or 32 bits with
  1 or 2 channels and 8000 to 192000 Hz is read (float WAV and anything else is
  refused with ``MixInputError``). Nothing is dropped silently: an empty file,
  an empty source window or a track that starts after the voice ends is an
  error; a track that runs past the voice end is clipped with a warning and a
  music source shorter than its span is not looped (warning ``music_short``).
- Tracks are placed by absolute milliseconds (the service turns scene relative
  sound effects into absolute times). The source window ``source_in_ms`` to
  ``source_out_ms`` is cut, gain is the track's ``gain_db`` or the role default
  (music -18 dB, sound effects -6 dB) clamped to -60..+12 dB, and fades are
  raised cosine, each clamped to half of the placed span.
- Ducking: the voice is active in a 10 ms window when its peak is over
  ``voice_threshold_dbfs``. The music (not the effects) is lowered by
  ``duck_db`` while the voice is active, starting one attack time early
  (look-ahead) and following a one-pole attack and release.
- Level: the voice plus the tracks are brought to ``target_rms_dbfs`` measured
  over the active voice windows (RMS based, not BS.1770 LUFS) with a static gain
  limited to ``max_boost_db`` either way; a mix without an active voice window
  keeps its level.
- A look-ahead limiter keeps the peaks at or under ``peak_ceiling_dbfs`` and a
  final hard clamp makes that certain. The output is 16-bit PCM, rounded half to
  even.
"""

import io
import math
import struct
import sys
import wave
from array import array
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
ACTIVITY_WINDOW_MS = 10
FLOOR_DBFS = -120.0
MIN_GAIN_DB = -60.0
MAX_GAIN_DB = 12.0
_DOUBLE = array("d").itemsize


class MixInputError(ValueError):
    """An input cannot be mixed (bad audio, bad parameters, bad placement)."""


def _number(name: str, value: object, low: float, high: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or math.isnan(value)
        or not low <= value <= high
    ):
        raise MixInputError(f"{name} must be a number from {low:g} to {high:g}")


def _whole(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise MixInputError(f"{name} must be a whole number from {low} to {high}")


# Audio


@dataclass(frozen=True)
class PcmAudio:
    """Interleaved samples in -1..1 (``array('d')``)."""

    sample_rate: int
    channels: int
    samples: array

    def __post_init__(self) -> None:
        _whole("sample rate", self.sample_rate, MIN_SAMPLE_RATE, MAX_SAMPLE_RATE)
        _whole("channels", self.channels, 1, 2)
        if not isinstance(self.samples, array) or self.samples.typecode != "d":
            raise MixInputError("samples must be an array of doubles")
        if not self.samples or len(self.samples) % self.channels:
            raise MixInputError("the audio has no whole frames")

    @property
    def frames(self) -> int:
        return len(self.samples) // self.channels

    @property
    def duration_ms(self) -> int:
        return _to_ms(self.frames, self.sample_rate)


def _to_ms(frames: int, rate: int) -> int:
    return (frames * 1000 + rate // 2) // rate


def _frames(ms: int, rate: int) -> int:
    return (ms * rate + 500) // 1000


def _zeros(count: int) -> array:
    return array("d", bytes(_DOUBLE * count))


def decode_wav(data: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> PcmAudio:
    """Read PCM WAV bytes; ``MixInputError`` when they cannot be mixed."""
    if not isinstance(data, bytes | bytearray):
        raise MixInputError("the audio must be bytes")
    if len(data) > max_bytes:
        raise MixInputError(f"the audio is {len(data)} bytes, over {max_bytes}")
    try:
        with wave.open(io.BytesIO(bytes(data)), "rb") as reader:
            rate = reader.getframerate()
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            frames = reader.getnframes()
            if not MIN_SAMPLE_RATE <= rate <= MAX_SAMPLE_RATE:
                raise MixInputError(f"sample rate {rate} Hz is not supported")
            if channels not in (1, 2):
                raise MixInputError(f"{channels} channels are not supported")
            if width not in (1, 2, 3, 4):
                raise MixInputError(f"sample width {width} is not supported")
            if frames < 1:
                raise MixInputError("the audio has no frames")
            raw = reader.readframes(frames)
    except (wave.Error, EOFError, struct.error) as error:
        raise MixInputError(f"not a readable PCM WAV file: {error}") from None
    if len(raw) != frames * channels * width:
        raise MixInputError(
            f"audio data is {len(raw)} bytes, the header promises "
            f"{frames * channels * width}"
        )
    return PcmAudio(rate, channels, _floats(raw, width))


def _floats(raw: bytes, width: int) -> array:
    if width == 1:
        return array("d", [(byte - 128) / 128 for byte in raw])
    if width == 3:
        scale = 1 << 23
        return array(
            "d",
            [
                int.from_bytes(raw[start : start + 3], "little", signed=True) / scale
                for start in range(0, len(raw), 3)
            ],
        )
    code = "h" if width == 2 else ("i" if array("i").itemsize == 4 else "l")
    values = array(code)
    values.frombytes(raw)
    if sys.byteorder == "big":
        values.byteswap()
    scale = 1 << (8 * width - 1)
    return array("d", [value / scale for value in values])


def encode_wav(pcm: PcmAudio) -> bytes:
    """16-bit PCM WAV bytes, samples rounded half to even and clamped."""
    values = array(
        "h", [max(-32768, min(32767, round(v * 32768))) for v in pcm.samples]
    )
    if sys.byteorder == "big":
        values.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(pcm.channels)
        writer.setsampwidth(2)
        writer.setframerate(pcm.sample_rate)
        writer.writeframes(values.tobytes())
    return buffer.getvalue()


# Parameters


@dataclass(frozen=True)
class MixConstraints:
    music_gain_db: float = -18.0
    sfx_gain_db: float = -6.0
    min_gain_db: float = MIN_GAIN_DB
    max_gain_db: float = MAX_GAIN_DB
    duck_db: float = 12.0
    duck_attack_ms: int = 60
    duck_release_ms: int = 400
    voice_threshold_dbfs: float = -45.0
    target_rms_dbfs: float = -18.0
    max_boost_db: float = 12.0
    peak_ceiling_dbfs: float = -1.0
    limiter_lookahead_ms: int = 5
    limiter_release_ms: int = 80
    max_tracks: int = 64
    max_total_seconds: int = 3600

    def __post_init__(self) -> None:
        _number("min_gain_db", self.min_gain_db, MIN_GAIN_DB, 0)
        _number("max_gain_db", self.max_gain_db, 0, MAX_GAIN_DB)
        _number("music_gain_db", self.music_gain_db, MIN_GAIN_DB, MAX_GAIN_DB)
        _number("sfx_gain_db", self.sfx_gain_db, MIN_GAIN_DB, MAX_GAIN_DB)
        _number("duck_db", self.duck_db, 0, 60)
        _whole("duck_attack_ms", self.duck_attack_ms, 0, 5000)
        _whole("duck_release_ms", self.duck_release_ms, 0, 5000)
        _number("voice_threshold_dbfs", self.voice_threshold_dbfs, -120, 0)
        _number("target_rms_dbfs", self.target_rms_dbfs, -60, 0)
        _number("max_boost_db", self.max_boost_db, 0, 24)
        _number("peak_ceiling_dbfs", self.peak_ceiling_dbfs, -20, 0)
        _whole("limiter_lookahead_ms", self.limiter_lookahead_ms, 0, 50)
        _whole("limiter_release_ms", self.limiter_release_ms, 1, 2000)
        _whole("max_tracks", self.max_tracks, 0, 256)
        _whole("max_total_seconds", self.max_total_seconds, 1, 4 * 3600)

    def to_dict(self) -> dict[str, float | int]:
        """Plain numbers, for the audit event."""
        return {
            "music_gain_db": self.music_gain_db,
            "sfx_gain_db": self.sfx_gain_db,
            "duck_db": self.duck_db,
            "duck_attack_ms": self.duck_attack_ms,
            "duck_release_ms": self.duck_release_ms,
            "voice_threshold_dbfs": self.voice_threshold_dbfs,
            "target_rms_dbfs": self.target_rms_dbfs,
            "max_boost_db": self.max_boost_db,
            "peak_ceiling_dbfs": self.peak_ceiling_dbfs,
        }


DEFAULT_CONSTRAINTS = MixConstraints()


class MixRole(StrEnum):
    MUSIC = "music"
    SFX = "sfx"


@dataclass(frozen=True)
class MixTrack:
    """One music or effect track: its audio and where it plays (absolute ms)."""

    role: MixRole
    pcm: PcmAudio
    start_ms: int = 0
    end_ms: int | None = None
    source_in_ms: int | None = None
    source_out_ms: int | None = None
    gain_db: float | None = None
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    label: str = "track"

    def __post_init__(self) -> None:
        if not isinstance(self.role, MixRole):
            raise MixInputError("role must be a MixRole")
        if not isinstance(self.pcm, PcmAudio):
            raise MixInputError("pcm must be a PcmAudio")
        if not isinstance(self.label, str) or not self.label.strip():
            raise MixInputError("label must not be empty")
        _whole("start_ms", self.start_ms, 0, 4 * 3600 * 1000)
        if self.end_ms is not None:
            _whole("end_ms", self.end_ms, 1, 4 * 3600 * 1000)
            if self.end_ms <= self.start_ms:
                raise MixInputError(f"track {self.label} must end after it starts")
        for name in ("source_in_ms", "source_out_ms"):
            if getattr(self, name) is not None:
                _whole(name, getattr(self, name), 0, 4 * 3600 * 1000)
        if (
            self.source_in_ms is not None
            and self.source_out_ms is not None
            and self.source_in_ms >= self.source_out_ms
        ):
            raise MixInputError(f"track {self.label}: source_in_ms is not before out")
        _whole("fade_in_ms", self.fade_in_ms, 0, 4 * 3600 * 1000)
        _whole("fade_out_ms", self.fade_out_ms, 0, 4 * 3600 * 1000)
        if self.gain_db is not None:
            _number("gain_db", self.gain_db, MIN_GAIN_DB, MAX_GAIN_DB)


@dataclass(frozen=True)
class TrackReport:
    label: str
    role: MixRole
    start_ms: int
    end_ms: int
    gain_db: float


@dataclass(frozen=True)
class MixReport:
    duration_ms: int
    sample_rate: int
    channels: int
    peak_dbfs: float
    rms_dbfs: float
    pre_gain_rms_dbfs: float
    normalisation_gain_db: float
    limiter_max_reduction_db: float
    ducked_ratio: float
    tracks: tuple[TrackReport, ...]
    warnings: tuple[str, ...]

    def metadata(self) -> Mapping[str, float | int | str]:
        """Scalar numbers of the report, for the audit event."""
        return {
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "peak_dbfs": self.peak_dbfs,
            "rms_dbfs": self.rms_dbfs,
            "pre_gain_rms_dbfs": self.pre_gain_rms_dbfs,
            "normalisation_gain_db": self.normalisation_gain_db,
            "limiter_max_reduction_db": self.limiter_max_reduction_db,
            "ducked_ratio": self.ducked_ratio,
            "track_count": len(self.tracks),
            "warning_count": len(self.warnings),
            "warnings": ",".join(self.warnings),
        }


@dataclass(frozen=True)
class MixResult:
    wav: bytes
    report: MixReport


# The mix


def mix_audio(
    voice: PcmAudio,
    tracks: Sequence[MixTrack],
    constraints: MixConstraints = DEFAULT_CONSTRAINTS,
) -> MixResult:
    """Mix ``tracks`` under ``voice``; ``MixInputError`` when it cannot be done."""
    _check_inputs(voice, tracks, constraints)
    rate, channels = voice.sample_rate, voice.channels
    music = _zeros(len(voice.samples))
    effects = _zeros(len(voice.samples))
    warnings: list[str] = []
    reports = []
    for track in tracks:
        start, data, report = _place(track, voice, constraints, warnings)
        bus = music if track.role is MixRole.MUSIC else effects
        _add(bus, start * channels, data)
        reports.append(report)
    window, active = _activity(voice, constraints)
    ducked_ratio = 0.0
    if any(track.role is MixRole.MUSIC for track in tracks):
        gains, ducked_ratio = _duck_gains(active, window, voice.frames, constraints)
        for channel in range(channels):
            music[channel::channels] = array(
                "d",
                [v * g for v, g in zip(music[channel::channels], gains, strict=True)],
            )
    mixed = array(
        "d", [v + m + e for v, m, e in zip(voice.samples, music, effects, strict=True)]
    )
    step = window * channels
    measured = _active_rms(mixed, active, step)
    pre_gain = FLOOR_DBFS
    normalisation = 0.0
    if measured is not None and measured > 0:
        pre_gain = max(FLOOR_DBFS, 20 * math.log10(measured))
        normalisation = max(
            -constraints.max_boost_db,
            min(constraints.max_boost_db, constraints.target_rms_dbfs - pre_gain),
        )
        scale = 10 ** (normalisation / 20)
        mixed = array("d", [v * scale for v in mixed])
    mixed, reduction = _limit(mixed, channels, rate, constraints)
    final_rms = _active_rms(mixed, active, step)
    if final_rms is None:
        final_rms = _rms(mixed)
    report = MixReport(
        duration_ms=voice.duration_ms,
        sample_rate=rate,
        channels=channels,
        peak_dbfs=round(_dbfs(max(map(abs, mixed))), 2),
        rms_dbfs=round(_dbfs(final_rms), 2),
        pre_gain_rms_dbfs=round(pre_gain, 2),
        normalisation_gain_db=round(normalisation, 2),
        limiter_max_reduction_db=round(reduction, 2),
        ducked_ratio=round(ducked_ratio, 4),
        tracks=tuple(reports),
        warnings=tuple(warnings),
    )
    return MixResult(encode_wav(PcmAudio(rate, channels, mixed)), report)


def _check_inputs(
    voice: PcmAudio, tracks: Sequence[MixTrack], constraints: MixConstraints
) -> None:
    if not isinstance(voice, PcmAudio):
        raise MixInputError("the voice must be a PcmAudio")
    if not isinstance(constraints, MixConstraints):
        raise MixInputError("constraints must be MixConstraints")
    if len(tracks) > constraints.max_tracks:
        raise MixInputError(f"at most {constraints.max_tracks} tracks can be mixed")
    total_ms = voice.duration_ms
    for track in tracks:
        if not isinstance(track, MixTrack):
            raise MixInputError("tracks must be MixTrack values")
        total_ms += track.pcm.duration_ms
    if total_ms > constraints.max_total_seconds * 1000:
        raise MixInputError(
            f"the audio adds up to {total_ms} ms, over "
            f"{constraints.max_total_seconds} seconds"
        )


def _place(
    track: MixTrack,
    voice: PcmAudio,
    constraints: MixConstraints,
    warnings: list[str],
) -> tuple[int, array, TrackReport]:
    """The start frame, the processed samples and the report of one track."""
    rate, channels, total = voice.sample_rate, voice.channels, voice.frames
    start = _frames(track.start_ms, rate)
    if start >= total:
        raise MixInputError(
            f"track {track.label} starts at {track.start_ms} ms, at or after the "
            "end of the voice"
        )
    end = total
    if track.end_ms is not None:
        end = _frames(track.end_ms, rate)
        if end > total:
            end = total
            warnings.append(f"track_clipped:{track.label}")
    if end <= start:
        raise MixInputError(f"track {track.label} has no length at this sample rate")
    pcm = track.pcm
    first = 0
    if track.source_in_ms is not None:
        first = _frames(track.source_in_ms, pcm.sample_rate)
    last = pcm.frames
    if track.source_out_ms is not None:
        last = min(last, _frames(track.source_out_ms, pcm.sample_rate))
    if first >= last:
        raise MixInputError(f"track {track.label} has an empty source window")
    window = pcm.samples[first * pcm.channels : last * pcm.channels]
    data = _conform(window, pcm.channels, pcm.sample_rate, channels, rate)
    length = end - start
    if len(data) // channels < length:
        length = len(data) // channels
        if track.role is MixRole.MUSIC:
            warnings.append(f"music_short:{track.label}")
    data = data[: length * channels]
    default = (
        constraints.music_gain_db
        if track.role is MixRole.MUSIC
        else constraints.sfx_gain_db
    )
    wanted = default if track.gain_db is None else track.gain_db
    gain_db = max(constraints.min_gain_db, min(constraints.max_gain_db, wanted))
    scale = 10 ** (gain_db / 20)
    data = array("d", [v * scale for v in data])
    _fade(data, channels, min(_frames(track.fade_in_ms, rate), length // 2), False)
    _fade(data, channels, min(_frames(track.fade_out_ms, rate), length // 2), True)
    report = TrackReport(
        track.label,
        track.role,
        track.start_ms,
        _to_ms(start + length, rate),
        round(gain_db, 2),
    )
    return start, data, report


def _conform(
    data: array, source_channels: int, source_rate: int, channels: int, rate: int
) -> array:
    """``data`` in the channel layout and sample rate of the voice."""
    if source_channels != channels:
        if source_channels == 1:
            doubled = _zeros(len(data) * 2)
            doubled[0::2] = data
            doubled[1::2] = data
            data = doubled
        else:
            data = array(
                "d",
                [
                    (left + right) / 2
                    for left, right in zip(data[0::2], data[1::2], strict=True)
                ],
            )
    if source_rate != rate:
        data = _resample(data, channels, source_rate, rate)
    return data


def _resample(data: array, channels: int, source_rate: int, rate: int) -> array:
    """Linear interpolation from ``source_rate`` to ``rate``."""
    frames = len(data) // channels
    count = max(1, (frames * rate + source_rate // 2) // source_rate)
    out = _zeros(count * channels)
    last = frames - 1
    ratio = source_rate / rate
    for channel in range(channels):
        source = data[channel::channels]
        resampled = array("d")
        for index in range(count):
            position = index * ratio
            whole = int(position)
            if whole >= last:
                resampled.append(source[last])
            else:
                low = source[whole]
                resampled.append(low + (source[whole + 1] - low) * (position - whole))
        out[channel::channels] = resampled
    return out


def _fade(data: array, channels: int, frames: int, tail: bool) -> None:
    """Raised-cosine fade over ``frames`` frames at the head or the tail."""
    total = len(data) // channels
    for index in range(frames):
        gain = 0.5 * (1 - math.cos(math.pi * (index + 0.5) / frames))
        frame = total - 1 - index if tail else index
        for channel in range(channels):
            data[frame * channels + channel] *= gain


def _add(bus: array, offset: int, data: array) -> None:
    end = offset + len(data)
    # A track running past the end of the bus is cut at the bus end.
    bus[offset:end] = array(
        "d", [a + b for a, b in zip(bus[offset:end], data, strict=False)]
    )


# Ducking


def _activity(voice: PcmAudio, constraints: MixConstraints) -> tuple[int, list[bool]]:
    """The window size in frames and whether the voice is active in each window."""
    window = max(1, voice.sample_rate * ACTIVITY_WINDOW_MS // 1000)
    step = window * voice.channels
    threshold = 10 ** (constraints.voice_threshold_dbfs / 20)
    samples = voice.samples
    active = [
        max(map(abs, samples[start : start + step])) > threshold
        for start in range(0, len(samples), step)
    ]
    return window, active


def _coefficient(milliseconds: float) -> float:
    if milliseconds <= 0:
        return 1.0
    return 1 - math.exp(-ACTIVITY_WINDOW_MS / milliseconds)


def _duck_gains(
    active: list[bool], window: int, frames: int, constraints: MixConstraints
) -> tuple[array, float]:
    """The linear music gain of every frame and the share of ducked windows."""
    count = len(active)
    ahead = -(-constraints.duck_attack_ms // ACTIVITY_WINDOW_MS)
    targets = [
        -constraints.duck_db if any(active[index : index + ahead + 1]) else 0.0
        for index in range(count)
    ]
    attack = _coefficient(constraints.duck_attack_ms)
    release = _coefficient(constraints.duck_release_ms)
    current = 0.0
    levels = []
    for target in targets:
        current += (target - current) * (attack if target < current else release)
        levels.append(10 ** (current / 20))
    levels.append(levels[-1])
    gains = array("d")
    for index in range(count):
        first = index * window
        size = min(frames, first + window) - first
        low = levels[index]
        slope = (levels[index + 1] - low) / window
        gains.extend(low + slope * offset for offset in range(size))
    ducked = sum(target < 0 for target in targets) / count
    return gains, ducked


# Level


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return FLOOR_DBFS
    return max(FLOOR_DBFS, 20 * math.log10(amplitude))


def _rms(samples: array) -> float:
    return math.sqrt(sum(v * v for v in samples) / len(samples))


def _active_rms(samples: array, active: list[bool], step: int) -> float | None:
    """RMS over the samples of the active windows, None when there is none."""
    total = 0.0
    count = 0
    for index, on in enumerate(active):
        if on:
            chunk = samples[index * step : (index + 1) * step]
            total += sum(v * v for v in chunk)
            count += len(chunk)
    return math.sqrt(total / count) if count else None


def _limit(
    samples: array, channels: int, rate: int, constraints: MixConstraints
) -> tuple[array, float]:
    """A look-ahead limiter to the ceiling; returns the samples and the reduction."""
    ceiling = 10 ** (constraints.peak_ceiling_dbfs / 20)
    if channels == 1:
        peaks = [abs(v) for v in samples]
    else:
        peaks = [
            max(abs(left), abs(right))
            for left, right in zip(samples[0::2], samples[1::2], strict=True)
        ]
    reduction = 0.0
    if max(peaks) > ceiling:
        required = [ceiling / p if p > ceiling else 1.0 for p in peaks]
        window = max(1, rate * constraints.limiter_lookahead_ms // 1000)
        floor = _forward_min(required, window)
        release_frames = max(1.0, constraints.limiter_release_ms * rate / 1000)
        recovery = 1 - math.exp(-1 / release_frames)
        gains = array("d")
        running = 0.0
        previous = 1.0
        for index, value in enumerate(floor):
            running += value
            if index >= window:
                running -= floor[index - window]
            smooth = running / min(window, index + 1)
            if smooth > previous:
                smooth = previous + (smooth - previous) * recovery
            gains.append(smooth)
            previous = smooth
        for channel in range(channels):
            samples[channel::channels] = array(
                "d",
                [v * g for v, g in zip(samples[channel::channels], gains, strict=True)],
            )
        reduction = -20 * math.log10(min(gains))
    samples = array("d", [max(-ceiling, min(ceiling, v)) for v in samples])
    return samples, reduction


def _forward_min(values: list[float], window: int) -> list[float]:
    """``out[i]`` is the minimum of ``values[i : i + window]``."""
    out = [0.0] * len(values)
    kept: deque[int] = deque()
    for index in range(len(values) - 1, -1, -1):
        while kept and values[kept[0]] >= values[index]:
            kept.popleft()
        kept.appendleft(index)
        while kept[-1] > index + window - 1:
            kept.pop()
        out[index] = values[kept[-1]]
    return out
