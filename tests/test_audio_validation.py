"""H-090 Audio Validation (Prompt Pack v8, prompt #090).

Rules (see ``content/audio_validation.py``): a stored WAV artifact is checked
for integrity, format, duration, silence and level; the outcome is a new
``QCResult`` per run, and one ``audio.validated`` event (scalars only) is
audited after the commit. Nothing is blocked or retried here.
"""

import dataclasses
import hashlib
import io
import math
import struct
import uuid
import wave
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from ai_youtube_agent.content import audio_validation
from ai_youtube_agent.content.audio_validation import (
    ACTOR,
    DEFAULT_LIMITS,
    VALIDATED,
    VALIDATION_METHOD,
    AudioLimits,
    AudioStats,
    AudioValidationError,
    AudioValidationErrorCode,
    AudioValidationResult,
    AudioValidator,
    analyze,
)
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.voice import AudioMetadata
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import (
    ActorKind,
    AuditLog,
    AuditResult,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.core.errors import DomainError
from factories import (
    T0,
    at,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

RATE = 16000
T1 = T0 + timedelta(hours=1)
NAMES = [
    "audio.integrity",
    "audio.format",
    "audio.duration",
    "audio.silence",
    "audio.level",
]


# WAV builders


@dataclass(frozen=True)
class Clip:
    data: bytes
    duration_ms: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def sine(seconds: float, amp: float = 0.5, rate: int = RATE, freq: int = 440):
    count = round(seconds * rate)
    return [amp * math.sin(2 * math.pi * freq * n / rate) for n in range(count)]


def quiet(seconds: float, rate: int = RATE):
    return [0.0] * round(seconds * rate)


def square(seconds: float, amp: float = 1.0, rate: int = RATE):
    count = round(seconds * rate)
    return [amp if (n // 20) % 2 == 0 else -amp for n in range(count)]


def stereo(left, right):
    values = []
    for a, b in zip(left, right, strict=True):
        values.extend((a, b))
    return values


def pack(values, width: int) -> bytes:
    scale = 2 ** (8 * width - 1) - 1
    ints = [max(-scale - 1, min(scale, round(v * scale))) for v in values]
    if width == 1:
        return bytes(i + 128 for i in ints)
    if width == 2:
        return struct.pack(f"<{len(ints)}h", *ints)
    if width == 3:
        return b"".join(i.to_bytes(3, "little", signed=True) for i in ints)
    return struct.pack(f"<{len(ints)}i", *ints)


def make_wav(
    values, *, rate: int = RATE, width: int = 2, channels: int = 1
) -> Clip:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(pack(values, width))
    frames = len(values) // channels
    return Clip(buffer.getvalue(), round(frames * 1000 / rate))


def run(
    clip: Clip,
    *,
    expected_ms: int | None = None,
    limits: AudioLimits = DEFAULT_LIMITS,
):
    return analyze(
        clip.data,
        expected_sha256=clip.sha256,
        expected_size=len(clip.data),
        expected_duration_ms=clip.duration_ms if expected_ms is None else expected_ms,
        limits=limits,
    )


def by_name(checks):
    return {check.name: check for check in checks}


def raw_run(data: bytes, **overrides):
    arguments = {
        "expected_sha256": hashlib.sha256(data).hexdigest(),
        "expected_size": len(data),
        "expected_duration_ms": 1000,
        "limits": DEFAULT_LIMITS,
    }
    arguments.update(overrides)
    return analyze(data, **arguments)


def expect_single_integrity_failure(checks, stats) -> str:
    assert stats is None
    (check,) = checks
    assert check.name == "audio.integrity"
    assert check.status is QCStatus.FAIL
    assert check.detail and check.detail.strip()
    return check.detail


# A good file


def test_a_good_sine_passes_every_check_with_stats() -> None:
    checks, stats = run(make_wav(sine(2.0)))

    assert [check.name for check in checks] == NAMES
    assert all(check.status is QCStatus.PASS for check in checks)
    assert all(check.detail is None for check in checks)
    assert isinstance(stats, AudioStats)
    assert stats.duration_ms == 2000
    assert stats.sample_rate == RATE
    assert stats.channels == 1
    assert stats.sample_width == 2
    assert stats.frames == 2 * RATE
    assert stats.peak_dbfs == pytest.approx(-6.02, abs=0.1)
    assert stats.rms_dbfs == pytest.approx(-9.03, abs=0.1)
    assert stats.longest_silence_ms == 0
    assert stats.clipped_samples == 0


def test_the_default_limits_are_the_approved_ones() -> None:
    limits = DEFAULT_LIMITS
    assert limits.min_duration_ms == 500
    assert limits.max_duration_ms == 3 * 60 * 60 * 1000
    assert limits.min_tolerance_ms == 100
    assert limits.tolerance_ratio == 0.01
    assert limits.window_ms == 50
    assert limits.silence_dbfs == -50
    assert limits.digital_silence_dbfs == -60
    assert limits.silent_ratio_fail == 0.9
    assert limits.silent_ratio_warn == 0.5
    assert limits.long_silence_ms == 3000
    assert limits.edge_silence_ms == 5000
    assert limits.clip_fail_ratio == 0.001
    assert limits.peak_warn_dbfs == -0.1
    assert limits.rms_fail_dbfs == -50
    assert limits.rms_warn_low_dbfs == -35
    assert limits.rms_warn_high_dbfs == -6
    assert limits.max_analyze_bytes == 512 * 1024 * 1024
    assert VALIDATION_METHOD == "audio-rules-v1"


def test_a_low_sample_rate_warns_on_the_format_only() -> None:
    clip = make_wav(sine(2.0, rate=8000), rate=8000)

    checks, stats = run(clip)

    found = by_name(checks)
    assert found["audio.format"].status is QCStatus.WARN
    assert "8000" in (found["audio.format"].detail or "")
    assert all(
        check.status is QCStatus.PASS for name, check in found.items() if name != "audio.format"
    )
    assert stats is not None and stats.sample_rate == 8000


# Duration


def test_a_duration_that_differs_from_the_metadata_fails() -> None:
    checks, _ = run(make_wav(sine(2.0)), expected_ms=3000)

    check = by_name(checks)["audio.duration"]
    assert check.status is QCStatus.FAIL
    assert "2000" in (check.detail or "") and "3000" in (check.detail or "")


def test_a_small_difference_is_inside_the_tolerance() -> None:
    checks, _ = run(make_wav(sine(2.0)), expected_ms=2100)

    assert by_name(checks)["audio.duration"].status is QCStatus.PASS


def test_a_difference_over_the_minimum_tolerance_fails() -> None:
    checks, _ = run(make_wav(sine(2.0)), expected_ms=2101)

    assert by_name(checks)["audio.duration"].status is QCStatus.FAIL


def test_the_tolerance_grows_to_one_percent_of_the_expected_duration() -> None:
    clip = make_wav(sine(12.0))

    inside, _ = run(clip, expected_ms=12110)  # off by 110 ms, 1 % is 121 ms
    outside, _ = run(clip, expected_ms=12130)  # off by 130 ms, 1 % is 121 ms

    assert by_name(inside)["audio.duration"].status is QCStatus.PASS
    assert by_name(outside)["audio.duration"].status is QCStatus.FAIL


def test_audio_under_the_minimum_duration_fails() -> None:
    checks, stats = run(make_wav(sine(0.3)))

    check = by_name(checks)["audio.duration"]
    assert check.status is QCStatus.FAIL
    assert "300" in (check.detail or "")
    assert stats is not None and stats.duration_ms == 300


def test_audio_over_the_maximum_duration_fails() -> None:
    limits = AudioLimits(max_duration_ms=1000)

    checks, _ = run(make_wav(sine(2.0)), limits=limits)

    assert by_name(checks)["audio.duration"].status is QCStatus.FAIL


# Integrity


def test_a_wrong_sha256_is_a_single_integrity_failure() -> None:
    clip = make_wav(sine(2.0))

    checks, stats = analyze(
        clip.data,
        expected_sha256="0" * 64,
        expected_size=len(clip.data),
        expected_duration_ms=clip.duration_ms,
    )

    assert "sha256" in expect_single_integrity_failure(checks, stats)


def test_a_wrong_size_is_a_single_integrity_failure() -> None:
    clip = make_wav(sine(2.0))

    checks, stats = analyze(
        clip.data,
        expected_sha256=clip.sha256,
        expected_size=len(clip.data) + 1,
        expected_duration_ms=clip.duration_ms,
    )

    assert "size" in expect_single_integrity_failure(checks, stats)


def test_a_truncated_file_is_a_single_integrity_failure() -> None:
    clip = make_wav(sine(2.0))

    checks, stats = raw_run(clip.data[:-1000])

    expect_single_integrity_failure(checks, stats)


def test_garbage_is_a_single_integrity_failure() -> None:
    checks, stats = raw_run(b"this is not a wav file at all")

    expect_single_integrity_failure(checks, stats)


def test_a_riff_header_without_a_body_is_a_single_integrity_failure() -> None:
    checks, stats = raw_run(b"RIFF\x00\x00\x00\x00WAVE")

    expect_single_integrity_failure(checks, stats)


def test_empty_data_is_a_single_integrity_failure() -> None:
    checks, stats = raw_run(b"")

    expect_single_integrity_failure(checks, stats)


def test_a_file_over_the_size_limit_is_a_single_integrity_failure() -> None:
    clip = make_wav(sine(2.0))

    checks, stats = run(clip, limits=AudioLimits(max_analyze_bytes=100))

    assert "limit" in expect_single_integrity_failure(checks, stats)


def test_a_file_without_frames_is_a_single_integrity_failure() -> None:
    checks, stats = run(make_wav([]))

    expect_single_integrity_failure(checks, stats)


def test_more_than_two_channels_is_a_single_integrity_failure() -> None:
    clip = make_wav(sine(1.0) * 3, channels=3)

    checks, stats = run(clip)

    assert "channels" in expect_single_integrity_failure(checks, stats)


# Silence


def test_a_file_of_zeros_fails_the_silence_and_level_checks() -> None:
    checks, stats = run(make_wav(quiet(2.0)))

    found = by_name(checks)
    assert found["audio.silence"].status is QCStatus.FAIL
    assert found["audio.level"].status is QCStatus.FAIL
    assert stats is not None
    assert stats.peak_dbfs == -120.0
    assert stats.rms_dbfs == -120.0
    assert stats.longest_silence_ms == 2000


def test_mostly_silent_audio_fails() -> None:
    clip = make_wav(sine(0.5) + quiet(9.5))  # 95 % silent

    checks, _ = run(clip)

    check = by_name(checks)["audio.silence"]
    assert check.status is QCStatus.FAIL
    assert "95%" in (check.detail or "")


def test_a_long_gap_warns() -> None:
    clip = make_wav(sine(3.0) + quiet(4.0) + sine(3.0))  # 40 % silent

    checks, stats = run(clip)

    check = by_name(checks)["audio.silence"]
    assert check.status is QCStatus.WARN
    assert "4000 ms" in (check.detail or "")
    assert stats is not None and stats.longest_silence_ms == 4000


def test_leading_silence_warns() -> None:
    clip = make_wav(quiet(5.5) + sine(6.0))  # 48 % silent

    checks, _ = run(clip)

    check = by_name(checks)["audio.silence"]
    assert check.status is QCStatus.WARN
    assert "at the start" in (check.detail or "")


def test_trailing_silence_warns() -> None:
    clip = make_wav(sine(6.0) + quiet(5.5))

    checks, _ = run(clip)

    check = by_name(checks)["audio.silence"]
    assert check.status is QCStatus.WARN
    assert "at the end" in (check.detail or "")


def test_speech_like_bursts_with_short_pauses_pass() -> None:
    values = []
    for _ in range(4):
        values += sine(0.5) + quiet(0.5)
    values += sine(0.5)

    checks, stats = run(make_wav(values))

    assert by_name(checks)["audio.silence"].status is QCStatus.PASS
    assert stats is not None and stats.longest_silence_ms == 500


# Level


def test_a_clipped_square_wave_fails_the_level() -> None:
    checks, stats = run(make_wav(square(2.0)))

    check = by_name(checks)["audio.level"]
    assert check.status is QCStatus.FAIL
    assert "clipped" in (check.detail or "")
    assert stats is not None and stats.clipped_samples == 2 * RATE


def test_a_few_clipped_samples_warn() -> None:
    values = sine(2.0)
    for k in range(10):
        values[1000 + k * 100] = 1.0

    checks, stats = run(make_wav(values))

    check = by_name(checks)["audio.level"]
    assert check.status is QCStatus.WARN
    assert "10 samples are clipped" in (check.detail or "")
    assert stats is not None and stats.clipped_samples == 10


def test_a_tone_at_minus_60_dbfs_fails_silence_and_level() -> None:
    checks, _ = run(make_wav(sine(2.0, amp=0.001)))

    found = by_name(checks)
    assert found["audio.silence"].status is QCStatus.FAIL
    assert found["audio.level"].status is QCStatus.FAIL


def test_a_very_quiet_but_audible_tone_fails_only_the_level() -> None:
    checks, _ = run(make_wav(sine(2.0, amp=0.004)))  # peak -48, RMS -51 dBFS

    found = by_name(checks)
    assert found["audio.silence"].status is QCStatus.PASS
    assert found["audio.level"].status is QCStatus.FAIL
    assert "quiet" in (found["audio.level"].detail or "")


def test_a_tone_at_minus_40_dbfs_warns_on_the_level() -> None:
    checks, _ = run(make_wav(sine(2.0, amp=0.01)))

    found = by_name(checks)
    assert found["audio.silence"].status is QCStatus.PASS
    assert found["audio.level"].status is QCStatus.WARN


def test_a_hot_tone_warns_on_the_level() -> None:
    checks, stats = run(make_wav(sine(2.0, amp=0.98)))

    check = by_name(checks)["audio.level"]
    assert check.status is QCStatus.WARN
    assert "RMS" in (check.detail or "")
    assert stats is not None and stats.clipped_samples == 0


# Sample widths and channels


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_every_width_gives_the_same_dbfs(width: int) -> None:
    checks, stats = run(make_wav(sine(2.0), width=width))

    assert all(check.status is QCStatus.PASS for check in checks)
    assert stats is not None
    assert stats.sample_width == width
    tolerance = 0.25 if width == 1 else 0.1  # 8-bit audio has 127 steps only
    assert stats.peak_dbfs == pytest.approx(-6.02, abs=tolerance)
    assert stats.rms_dbfs == pytest.approx(-9.03, abs=tolerance)


def test_stereo_audio_is_analysed_per_frame() -> None:
    left = sine(2.0)
    clip = make_wav(stereo(left, quiet(2.0)), channels=2)

    checks, stats = run(clip)

    assert all(check.status is QCStatus.PASS for check in checks)
    assert stats is not None
    assert stats.channels == 2
    assert stats.frames == 2 * RATE
    assert stats.duration_ms == 2000
    assert stats.peak_dbfs == pytest.approx(-6.02, abs=0.1)
    assert stats.rms_dbfs == pytest.approx(-12.04, abs=0.1)


def test_every_failing_or_warning_check_has_a_detail() -> None:
    clip = make_wav(square(0.3))

    checks, _ = run(clip, expected_ms=5000)

    assert any(check.status is not QCStatus.PASS for check in checks)
    for check in checks:
        if check.status is QCStatus.PASS:
            assert check.detail is None
        else:
            assert check.detail and check.detail.strip()


# The service


class World:
    def __init__(self, database: Database, tmp_path: Path) -> None:
        self.database = database
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        self.item = make_content_item(self.channel, self.strategy)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
            ContentItemRepository(connection).add(self.item)
        self.root = tmp_path / "artifacts"
        self.storage = LocalFileStorage(self.root)
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=at(T1))

    def validator(self, sink=None, **options) -> AudioValidator:
        audit = AuditLog(sink, clock=at(T1)) if sink is not None else self.audit
        return AudioValidator(
            self.database, self.storage, audit, clock=at(T1), **options
        )

    def store(
        self, clip: Clip, *, persist: bool = True, duration_ms: int | None = None
    ) -> tuple[Artifact, AudioMetadata]:
        written = self.storage.write(
            self.item.id, ArtifactKind.AUDIO, 1, clip.data, media_type="audio/wav"
        )
        artifact = Artifact.create(
            self.item.id,
            ArtifactKind.AUDIO,
            uri=written.uri,
            sha256=written.sha256,
            size_bytes=written.size_bytes,
            media_type="audio/wav",
            clock=at(),
        )
        if persist:
            with self.database.transaction() as connection:
                ArtifactRepository(connection).add(artifact)
        return artifact, self.metadata(artifact, duration_ms or clip.duration_ms)

    def metadata(self, artifact: Artifact, duration_ms: int) -> AudioMetadata:
        return AudioMetadata(
            id=uuid.uuid4().hex,
            artifact_id=artifact.id,
            script_id="script-1",
            voice_profile_id="voice-1",
            voice_profile_version=1,
            provider="mock_tts",
            duration_ms=duration_ms,
            created_at=T1,
        )

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            (count,) = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        return count

    def stored_qc(self, qc_id: str):
        with self.database.transaction() as connection:
            return QCResultRepository(connection).get(qc_id)

    def path(self) -> Path:
        return self.root.resolve() / self.item.id / "audio" / "v1.wav"


@pytest.fixture
def world(database: Database, tmp_path: Path) -> World:
    return World(database, tmp_path)


def test_a_good_file_makes_a_stored_passing_qc_result(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))

    result = world.validator().validate(artifact, audio)

    assert isinstance(result, AudioValidationResult)
    assert result.qc.status is QCStatus.PASS
    assert result.qc.content_item_id == world.item.id
    assert result.qc.artifact_ids == (artifact.id,)
    assert [check.name for check in result.qc.checks] == NAMES
    assert result.qc.created_at == T1
    assert result.stats is not None and result.stats.duration_ms == 2000
    assert world.stored_qc(result.qc.id) == result.qc


def test_every_run_makes_a_new_qc_result(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    validator = world.validator()

    first = validator.validate(artifact, audio)
    second = validator.validate(artifact, audio)

    assert first.qc.id != second.qc.id
    assert world.count("qc_results") == 2
    assert len(world.sink.events()) == 2


def test_a_failing_file_is_saved_not_raised(world: World) -> None:
    artifact, audio = world.store(make_wav(quiet(2.0)))

    result = world.validator().validate(artifact, audio)

    assert result.qc.status is QCStatus.FAIL
    assert world.stored_qc(result.qc.id) == result.qc


def test_a_metadata_duration_that_differs_fails_the_duration(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)), duration_ms=4000)

    result = world.validator().validate(artifact, audio)

    checks = by_name(result.qc.checks)
    assert checks["audio.duration"].status is QCStatus.FAIL


def test_the_validator_uses_its_limits(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    validator = world.validator(limits=AudioLimits(max_duration_ms=1000))

    result = validator.validate(artifact, audio)

    assert by_name(result.qc.checks)["audio.duration"].status is QCStatus.FAIL


def test_one_audio_validated_event_with_scalar_metadata(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0, amp=0.98)))

    result = world.validator().validate(artifact, audio)

    (event,) = world.sink.events()
    assert event.action == VALIDATED == "audio.validated"
    assert event.actor == ACTOR
    assert event.actor.kind is ActorKind.SYSTEM
    assert event.actor.id == "audio_validation"
    assert (event.entity.type, event.entity.id) == ("artifact", artifact.id)
    assert event.result is AuditResult.SUCCESS
    metadata = dict(event.metadata)
    assert result.stats is not None
    assert metadata == {
        "content_item_id": world.item.id,
        "qc_result_id": result.qc.id,
        "status": "warn",
        "method": VALIDATION_METHOD,
        "version": 1,
        "sha256": artifact.sha256,
        "duration_ms": 2000,
        "failed": "",
        "warned": "audio.level",
        "peak_dbfs": result.stats.peak_dbfs,
        "rms_dbfs": result.stats.rms_dbfs,
    }
    assert all(
        value is None or isinstance(value, str | int | float | bool)
        for value in metadata.values()
    )


def test_the_event_names_every_failed_check(world: World) -> None:
    artifact, audio = world.store(make_wav(quiet(2.0)))

    world.validator().validate(artifact, audio)

    (event,) = world.sink.events()
    assert event.metadata["status"] == "fail"
    assert event.metadata["failed"] == "audio.silence,audio.level"
    assert event.metadata["warned"] == ""


def test_the_event_is_recorded_after_the_commit(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    seen: list[int] = []

    class CheckingSink(InMemoryAuditSink):
        def append(self, event) -> None:
            seen.append(world.count("qc_results"))
            super().append(event)

    world.validator(sink=CheckingSink()).validate(artifact, audio)

    assert seen == [1]


def test_a_missing_file_is_an_integrity_failure_not_an_exception(
    world: World,
) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    world.storage.delete(artifact.uri)

    result = world.validator().validate(artifact, audio)

    assert result.stats is None
    (check,) = result.qc.checks
    assert check.name == "audio.integrity"
    assert check.status is QCStatus.FAIL
    assert check.detail == "file unavailable"
    assert world.stored_qc(result.qc.id) == result.qc
    (event,) = world.sink.events()
    assert event.metadata["duration_ms"] is None
    assert event.metadata["peak_dbfs"] is None
    assert event.metadata["failed"] == "audio.integrity"


def test_an_uri_the_storage_refuses_is_an_integrity_failure(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    foreign = dataclasses.replace(artifact, uri="store://elsewhere/audio/1")

    result = world.validator().validate(foreign, audio)

    (check,) = result.qc.checks
    assert (check.name, check.status) == ("audio.integrity", QCStatus.FAIL)
    assert check.detail == "file unavailable"


def test_a_file_changed_on_disk_fails_the_integrity(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    world.path().write_bytes(make_wav(sine(1.0)).data)

    result = world.validator().validate(artifact, audio)

    (check,) = result.qc.checks
    assert (check.name, check.status) == ("audio.integrity", QCStatus.FAIL)
    assert "sha256" in (check.detail or "")
    assert result.stats is None


# Refusals


def refused(world: World, artifact: Artifact, audio: AudioMetadata, code) -> None:
    with pytest.raises(AudioValidationError) as caught:
        world.validator().validate(artifact, audio)

    error = caught.value
    assert isinstance(error, DomainError)
    assert error.code == code.value
    assert error.user_message == audio_validation.USER_MESSAGES[code]
    assert world.count("qc_results") == 0
    assert world.sink.events() == ()


def test_a_non_wav_media_type_is_refused(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    other = dataclasses.replace(artifact, media_type="audio/mpeg")

    refused(world, other, audio, AudioValidationErrorCode.NOT_AUDIO)


def test_a_non_audio_artifact_is_refused(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    video = make_artifact(world.item, ArtifactKind.VIDEO, media_type="audio/wav")

    refused(world, video, audio, AudioValidationErrorCode.NOT_AUDIO)


def test_metadata_of_another_artifact_is_refused(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)))
    other = dataclasses.replace(audio, artifact_id="another-artifact")

    refused(world, artifact, other, AudioValidationErrorCode.METADATA_MISMATCH)


def test_an_artifact_that_is_not_stored_is_refused(world: World) -> None:
    artifact, audio = world.store(make_wav(sine(2.0)), persist=False)

    refused(world, artifact, audio, AudioValidationErrorCode.ARTIFACT_NOT_FOUND)


def test_every_error_code_has_a_user_message() -> None:
    assert set(audio_validation.USER_MESSAGES) == set(AudioValidationErrorCode)
    assert all(code.value.startswith("domain.") for code in AudioValidationErrorCode)
    assert AudioValidationError().code == AudioValidationErrorCode.VALIDATE.value
