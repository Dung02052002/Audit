"""Audio Validation (Prompt Pack v8, prompt #090), context C7 Voice & Audio.

``AudioValidator`` checks a stored audio ``Artifact`` (#089) and saves the
outcome as a ``QCResult`` (#020). The rules are pure (``analyze``) and use only
the standard library (``wave``, ``array``, ``math``); no ``audioop``. Decisions
(written for the user, build-first, 2026-10-16):

- Only ``audio/wav`` PCM audio (8, 16, 24 or 32 bit, mono or stereo) is
  validated. Every sample is normalised to the full scale of its width
  (``2 ** (8 * width - 1)``), so all widths give the same dBFS.
- Four ordered check groups plus an integrity gate:

  - ``audio.integrity`` (fail): the file is not over ``max_analyze_bytes``
    (512 MiB), its sha256 and size are those of the artifact, and it parses as
    a WAV with a sample rate of 1 or more, 1 or 2 channels, at least one frame
    and exactly the bytes its header promises (this catches truncation and
    corruption). When it fails it is the only check and there are no stats.
  - ``audio.format`` (warn): the sample rate is under 16000 Hz.
  - ``audio.duration`` (fail): the real duration (frames / rate) is under
    500 ms or over 3 h, or differs from the duration the metadata claims by
    more than max(100 ms, 1 %).
  - ``audio.silence``: audio is cut in 50 ms windows; a window is silent when
    its peak is at or under -50 dBFS. Fail when the whole file peaks at or
    under -60 dBFS (digital silence) or 90 % or more of it is silent. Warn
    for a silent run of 3 s or more, leading or trailing silence over 5 s, or
    50 % or more silence.
  - ``audio.level``: samples at full scale minus one step or more are clipped.
    Fail when 0.1 % or more are clipped; warn on any clipping or a peak above
    -0.1 dBFS. The RMS is measured over the non-silent windows (all of the
    audio when none): fail under -50 dBFS, warn under -35 or over -6 dBFS.

  The most severe status wins inside a check; the detail is a short English
  text with the numbers, and None on a pass. dBFS values are clamped to
  -120.0 for silence.
- ``AudioValidator.validate`` never blocks anything and does not retry: it
  saves a new ``QCResult`` on every run (a rerun is a new result), then audits
  one ``audio.validated`` event after the commit with scalar data only
  (failed and warned check names comma-joined). A file the storage cannot
  read becomes a single ``audio.integrity`` fail check, not an exception.

Out of scope: retries (#091), cost (#092) and gating on the result (#129).
"""

import hashlib
import io
import math
import struct
import sys
import wave
from array import array
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.voice import AudioMetadata
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ArtifactRepository
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.core.errors import DomainError

VALIDATION_METHOD = "audio-rules-v1"
ACTOR = Actor(ActorKind.SYSTEM, "audio_validation")
VALIDATED = "audio.validated"
WAV_MEDIA_TYPE = "audio/wav"
FLOOR_DBFS = -120.0
Clock = Callable[[], datetime]

INTEGRITY = "audio.integrity"
FORMAT = "audio.format"
DURATION = "audio.duration"
SILENCE = "audio.silence"
LEVEL = "audio.level"


@dataclass(frozen=True)
class AudioLimits:
    min_duration_ms: int = 500
    max_duration_ms: int = 3 * 60 * 60 * 1000
    min_tolerance_ms: int = 100
    tolerance_ratio: float = 0.01
    min_sample_rate: int = 16000
    window_ms: int = 50
    silence_dbfs: float = -50.0
    digital_silence_dbfs: float = -60.0
    silent_ratio_fail: float = 0.9
    silent_ratio_warn: float = 0.5
    long_silence_ms: int = 3000
    edge_silence_ms: int = 5000
    clip_fail_ratio: float = 0.001
    peak_warn_dbfs: float = -0.1
    rms_fail_dbfs: float = -50.0
    rms_warn_low_dbfs: float = -35.0
    rms_warn_high_dbfs: float = -6.0
    max_analyze_bytes: int = 512 * 1024 * 1024


DEFAULT_LIMITS = AudioLimits()


@dataclass(frozen=True)
class AudioStats:
    duration_ms: int
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    peak_dbfs: float
    rms_dbfs: float
    longest_silence_ms: int
    clipped_samples: int


class AudioValidationErrorCode(StrEnum):
    VALIDATE = "domain.audio_validation"
    NOT_AUDIO = "domain.audio_validation_not_audio"
    ARTIFACT_NOT_FOUND = "domain.audio_validation_artifact_not_found"
    METADATA_MISMATCH = "domain.audio_validation_metadata_mismatch"


USER_MESSAGES = {
    AudioValidationErrorCode.VALIDATE: "The audio could not be checked.",
    AudioValidationErrorCode.NOT_AUDIO: "Only WAV audio can be checked.",
    AudioValidationErrorCode.ARTIFACT_NOT_FOUND: "The audio to check was not found.",
    AudioValidationErrorCode.METADATA_MISMATCH: "The audio details belong to "
    "another audio file.",
}


class AudioValidationError(DomainError):
    """Audio could not be checked; ``code`` is an ``AudioValidationErrorCode``."""

    default_code = AudioValidationErrorCode.VALIDATE.value
    default_user_message = USER_MESSAGES[AudioValidationErrorCode.VALIDATE]

    def __init__(
        self,
        detail: str = "",
        *,
        code: AudioValidationErrorCode = AudioValidationErrorCode.VALIDATE,
    ) -> None:
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])


@dataclass(frozen=True)
class AudioValidationResult:
    qc: QCResult
    stats: AudioStats | None


class AudioValidator:
    def __init__(
        self,
        database: Database,
        storage: ArtifactStorage,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
        limits: AudioLimits = DEFAULT_LIMITS,
    ) -> None:
        self._database = database
        self._storage = storage
        self._audit = audit
        self._clock = clock
        self._limits = limits

    def validate(
        self, artifact: Artifact, audio: AudioMetadata
    ) -> AudioValidationResult:
        if artifact.kind is not ArtifactKind.AUDIO or (
            artifact.media_type != WAV_MEDIA_TYPE
        ):
            raise AudioValidationError(
                f"artifact {artifact.id} is {artifact.kind.value} "
                f"{artifact.media_type}, not audio/wav",
                code=AudioValidationErrorCode.NOT_AUDIO,
            )
        if audio.artifact_id != artifact.id:
            raise AudioValidationError(
                f"audio metadata {audio.id} belongs to artifact "
                f"{audio.artifact_id}, not {artifact.id}",
                code=AudioValidationErrorCode.METADATA_MISMATCH,
            )
        try:
            data = self._storage.read(artifact.uri)
        except (OSError, ValueError):
            checks: tuple[QCCheck, ...] = (
                QCCheck(INTEGRITY, QCStatus.FAIL, "file unavailable"),
            )
            stats = None
        else:
            checks, stats = analyze(
                data,
                expected_sha256=artifact.sha256,
                expected_size=artifact.size_bytes,
                expected_duration_ms=audio.duration_ms,
                limits=self._limits,
            )
        with self._database.transaction() as connection:
            if ArtifactRepository(connection).get(artifact.id) is None:
                raise AudioValidationError(
                    f"artifact {artifact.id} is not stored",
                    code=AudioValidationErrorCode.ARTIFACT_NOT_FOUND,
                )
            qc = QCResult.create(
                artifact.content_item_id, [artifact], checks, clock=self._clock
            )
            QCResultRepository(connection).add(qc)
        self._audit.record(
            VALIDATED,
            ACTOR,
            EntityRef("artifact", artifact.id),
            AuditResult.SUCCESS,
            _event_metadata(artifact, qc, stats),
        )
        return AudioValidationResult(qc, stats)


def analyze(
    data: bytes,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_duration_ms: int,
    limits: AudioLimits = DEFAULT_LIMITS,
) -> tuple[tuple[QCCheck, ...], AudioStats | None]:
    """Check WAV ``data`` against the rules; the stats are None on a bad file."""
    problem = _integrity_problem(data, expected_sha256, expected_size, limits)
    parsed = None
    if problem is None:
        parsed, problem = _parse(data)
    if problem is not None or parsed is None:
        return (QCCheck(INTEGRITY, QCStatus.FAIL, problem or "unreadable"),), None
    sample_rate, channels, width, raw = parsed
    samples = _samples(raw, width)
    full_scale = 2 ** (8 * width - 1)
    frames = len(samples) // channels
    windows = _windows(samples, channels, sample_rate, full_scale, limits)
    peak_abs = max(window.peak for window in windows)
    clipped = sum(window.clipped for window in windows)
    silent_frames = sum(window.frames for window in windows if window.silent)
    longest, leading, trailing = _silent_runs(windows)
    rms = _rms_dbfs(windows, samples, full_scale)
    stats = AudioStats(
        duration_ms=_ms(frames, sample_rate),
        sample_rate=sample_rate,
        channels=channels,
        sample_width=width,
        frames=frames,
        peak_dbfs=round(_dbfs(peak_abs, full_scale), 2),
        rms_dbfs=round(rms, 2),
        longest_silence_ms=_ms(longest, sample_rate),
        clipped_samples=clipped,
    )
    checks = (
        QCCheck(INTEGRITY, QCStatus.PASS),
        _format_check(sample_rate, limits),
        _duration_check(stats.duration_ms, expected_duration_ms, limits),
        _silence_check(
            _dbfs(peak_abs, full_scale),
            silent_frames / frames,
            (longest, leading, trailing),
            sample_rate,
            limits,
        ),
        _level_check(_dbfs(peak_abs, full_scale), rms, clipped, len(samples), limits),
    )
    return checks, stats


@dataclass(frozen=True)
class _Window:
    frames: int
    peak: int
    silent: bool
    clipped: int
    square_sum: int
    samples: int


def _integrity_problem(
    data: bytes, expected_sha256: str, expected_size: int, limits: AudioLimits
) -> str | None:
    if len(data) > limits.max_analyze_bytes:
        return f"file is {len(data)} bytes, over the {limits.max_analyze_bytes} limit"
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        return "sha256 differs from the artifact"
    if len(data) != expected_size:
        return f"size is {len(data)} bytes, the artifact says {expected_size}"
    return None


def _parse(data: bytes) -> tuple[tuple[int, int, int, bytes] | None, str | None]:
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            frames = reader.getnframes()
            if sample_rate < 1:
                return None, f"sample rate {sample_rate} is not valid"
            if channels not in (1, 2):
                return None, f"{channels} channels are not supported"
            if width not in (1, 2, 3, 4):
                return None, f"sample width {width} is not supported"
            if frames < 1:
                return None, "the audio has no frames"
            raw = reader.readframes(frames)
    except (wave.Error, EOFError, struct.error, ValueError) as error:
        return None, f"not a readable WAV file: {error}"
    if len(raw) != frames * channels * width:
        return None, (
            f"audio data is {len(raw)} bytes, the header promises "
            f"{frames * channels * width}"
        )
    return (sample_rate, channels, width, raw), None


def _samples(raw: bytes, width: int) -> Sequence[int]:
    if width == 1:
        return [byte - 128 for byte in raw]
    if width == 3:
        return [
            int.from_bytes(raw[start : start + 3], "little", signed=True)
            for start in range(0, len(raw), 3)
        ]
    code = "h" if width == 2 else ("i" if array("i").itemsize == 4 else "l")
    values = array(code)
    values.frombytes(raw)
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _windows(
    samples: Sequence[int],
    channels: int,
    sample_rate: int,
    full_scale: int,
    limits: AudioLimits,
) -> list[_Window]:
    window_frames = max(1, sample_rate * limits.window_ms // 1000)
    step = window_frames * channels
    silence_level = full_scale * 10 ** (limits.silence_dbfs / 20)
    clip_level = full_scale - 1
    windows = []
    for start in range(0, len(samples), step):
        chunk = samples[start : start + step]
        peak = max(max(chunk), -min(chunk))
        silent = peak <= silence_level
        clipped = 0
        if peak >= clip_level:
            clipped = sum(1 for value in chunk if abs(value) >= clip_level)
        square_sum = 0 if silent else sum(value * value for value in chunk)
        windows.append(
            _Window(
                len(chunk) // channels, peak, silent, clipped, square_sum, len(chunk)
            )
        )
    return windows


def _silent_runs(windows: list[_Window]) -> tuple[int, int, int]:
    """Longest, leading and trailing silent run, in frames."""
    longest = run = 0
    for window in windows:
        run = run + window.frames if window.silent else 0
        longest = max(longest, run)
    leading = trailing = 0
    for window in windows:
        if not window.silent:
            break
        leading += window.frames
    for window in reversed(windows):
        if not window.silent:
            break
        trailing += window.frames
    return longest, leading, trailing


def _rms_dbfs(windows: list[_Window], samples: Sequence[int], full_scale: int) -> float:
    """RMS over the non-silent windows, or over all samples when none is."""
    active = [window for window in windows if not window.silent]
    if active:
        total = sum(window.square_sum for window in active)
        count = sum(window.samples for window in active)
    else:
        total = sum(value * value for value in samples)
        count = len(samples)
    return _dbfs(math.sqrt(total / count), full_scale)


def _dbfs(amplitude: float, full_scale: int) -> float:
    if amplitude <= 0 or full_scale <= 0:
        return FLOOR_DBFS
    return max(FLOOR_DBFS, 20 * math.log10(amplitude / full_scale))


def _ms(frames: int, sample_rate: int) -> int:
    return round(frames * 1000 / sample_rate)


def _worst(findings: list[tuple[QCStatus, str]], name: str) -> QCCheck:
    if not findings:
        return QCCheck(name, QCStatus.PASS)
    status = (
        QCStatus.FAIL
        if any(found is QCStatus.FAIL for found, _ in findings)
        else QCStatus.WARN
    )
    details = "; ".join(text for found, text in findings if found is status)
    return QCCheck(name, status, details)


def _format_check(sample_rate: int, limits: AudioLimits) -> QCCheck:
    findings = []
    if sample_rate < limits.min_sample_rate:
        findings.append(
            (
                QCStatus.WARN,
                f"sample rate {sample_rate} Hz is under {limits.min_sample_rate} Hz",
            )
        )
    return _worst(findings, FORMAT)


def _duration_check(actual: int, expected: int, limits: AudioLimits) -> QCCheck:
    findings = []
    if actual < limits.min_duration_ms:
        findings.append(
            (
                QCStatus.FAIL,
                f"duration {actual} ms is under {limits.min_duration_ms} ms",
            )
        )
    if actual > limits.max_duration_ms:
        findings.append(
            (QCStatus.FAIL, f"duration {actual} ms is over {limits.max_duration_ms} ms")
        )
    tolerance = max(limits.min_tolerance_ms, round(expected * limits.tolerance_ratio))
    if abs(actual - expected) > tolerance:
        findings.append(
            (
                QCStatus.FAIL,
                f"duration {actual} ms differs from the expected {expected} ms "
                f"by more than {tolerance} ms",
            )
        )
    return _worst(findings, DURATION)


def _silence_check(
    peak_dbfs: float,
    silent_ratio: float,
    runs: tuple[int, int, int],
    sample_rate: int,
    limits: AudioLimits,
) -> QCCheck:
    longest, leading, trailing = (_ms(run, sample_rate) for run in runs)
    findings = []
    if peak_dbfs <= limits.digital_silence_dbfs:
        findings.append(
            (QCStatus.FAIL, f"the audio is silent, its peak is {peak_dbfs:.1f} dBFS")
        )
    if silent_ratio >= limits.silent_ratio_fail:
        findings.append((QCStatus.FAIL, f"{silent_ratio:.0%} of the audio is silent"))
    elif silent_ratio >= limits.silent_ratio_warn:
        findings.append((QCStatus.WARN, f"{silent_ratio:.0%} of the audio is silent"))
    if longest >= limits.long_silence_ms:
        findings.append((QCStatus.WARN, f"a silence of {longest} ms"))
    if leading > limits.edge_silence_ms:
        findings.append((QCStatus.WARN, f"{leading} ms of silence at the start"))
    if trailing > limits.edge_silence_ms:
        findings.append((QCStatus.WARN, f"{trailing} ms of silence at the end"))
    return _worst(findings, SILENCE)


def _level_check(
    peak_dbfs: float, rms_dbfs: float, clipped: int, total: int, limits: AudioLimits
) -> QCCheck:
    findings = []
    ratio = clipped / total
    if ratio >= limits.clip_fail_ratio:
        findings.append((QCStatus.FAIL, f"{clipped} samples are clipped ({ratio:.2%})"))
    elif clipped:
        findings.append((QCStatus.WARN, f"{clipped} samples are clipped"))
    if peak_dbfs > limits.peak_warn_dbfs:
        findings.append(
            (QCStatus.WARN, f"peak {peak_dbfs:.1f} dBFS is near full scale")
        )
    if rms_dbfs < limits.rms_fail_dbfs:
        findings.append((QCStatus.FAIL, f"RMS {rms_dbfs:.1f} dBFS is too quiet"))
    elif rms_dbfs < limits.rms_warn_low_dbfs:
        findings.append((QCStatus.WARN, f"RMS {rms_dbfs:.1f} dBFS is quiet"))
    elif rms_dbfs > limits.rms_warn_high_dbfs:
        findings.append((QCStatus.WARN, f"RMS {rms_dbfs:.1f} dBFS is loud"))
    return _worst(findings, LEVEL)


def _event_metadata(
    artifact: Artifact, qc: QCResult, stats: AudioStats | None
) -> Mapping[str, MetadataValue]:
    return {
        "content_item_id": artifact.content_item_id,
        "qc_result_id": qc.id,
        "status": qc.status.value,
        "method": VALIDATION_METHOD,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "duration_ms": stats.duration_ms if stats else None,
        "failed": _names(qc, QCStatus.FAIL),
        "warned": _names(qc, QCStatus.WARN),
        "peak_dbfs": stats.peak_dbfs if stats else None,
        "rms_dbfs": stats.rms_dbfs if stats else None,
    }


def _names(qc: QCResult, status: QCStatus) -> str:
    return ",".join(check.name for check in qc.checks if check.status is status)
