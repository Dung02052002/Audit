"""Tests for Phase K video QC checks (prompts K-116..K-125).

Validates:
- K-116: VideoArtifactTestHarness orchestration, check execution, persistence and audit;
- K-117: check_container with valid MP4, empty bytes, corrupt headers and
  unrecognized brands;
- K-118: check_resolution for Shorts (9:16 vertical) and LongForm (16:9 horizontal);
- K-119: check_fps for target matching, low fps warning, and invalid fps;
- K-120: check_duration for content type bounds (Shorts 1..180s, LongForm 181..14400s)
  and audio synchronization tolerance;
- K-121: check_audio for presence, duration sync, silence, and clipping thresholds;
- K-122: check_subtitles for SRT/VTT timing, UTF-8/NFC encoding, and missing captions;
- K-123: check_black_frames for excessive black/frozen frames;
- K-124: check_render_integrity for incomplete/corrupt MP4 boxes, checksums, and job
  lineage;
- K-125: check_metadata for consistency between artifacts, project/script versions,
  chapters, attributions, and AI disclosure.
"""

import hashlib
import io
import math
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_youtube_agent.content.longform_metadata import ChapterMetadata, LongFormMetadata
from ai_youtube_agent.content.policy_check import PolicyStatus
from ai_youtube_agent.content.qc import QCResult, QCStatus
from ai_youtube_agent.content.qc_harness import (
    QCHarnessError,
    QCHarnessErrorCode,
    VideoArtifactTestHarness,
    all_video_checks,
)
from ai_youtube_agent.content.qc_video_checks import (
    AUDIO_CHECK_NAME,
    BLACK_FRAME_CHECK_NAME,
    CONTAINER_CHECK_NAME,
    DURATION_CHECK_NAME,
    FPS_CHECK_NAME,
    INTEGRITY_CHECK_NAME,
    METADATA_CHECK_NAME,
    RESOLUTION_CHECK_NAME,
    SUBTITLE_CHECK_NAME,
    VideoInspectionContext,
    check_audio,
    check_black_frames,
    check_container,
    check_duration,
    check_fps,
    check_metadata,
    check_render_integrity,
    check_resolution,
    check_subtitles,
)
from ai_youtube_agent.content.shorts_metadata import ShortsMetadata
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.artifact_storage import LocalFileStorage
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentItem, ContentType
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
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from factories import USER, at

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def make_sine_wav(
    duration_ms: int = 2000,
    *,
    amplitude: int = 8000,
    sample_rate: int = 16000,
) -> bytes:
    frames = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        samples = [
            int(amplitude * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))
            for i in range(frames)
        ]
        wav.writeframes(struct.pack(f"<{frames}h", *samples))
    return buf.getvalue()


def make_context(
    *,
    content_type: ContentType = ContentType.SHORTS,
    duration_ms: int = 30_000,
    expected_duration_ms: int = 30_000,
    expected_width: int = 1080,
    expected_height: int = 1920,
    expected_fps: int = 30,
    actual_width: int | None = None,
    actual_height: int | None = None,
    actual_fps: float | None = None,
    video_data: bytes | None = None,
    min_duration_ms: int = 1000,
    max_duration_ms: int = 60_000,
    **kwargs: Any,
) -> VideoInspectionContext:
    data = (
        video_data
        if video_data is not None
        else stub_mp4(duration_ms, b"dummy-frame-data")
    )
    return VideoInspectionContext(
        content_item_id="item-qc-1",
        content_type=content_type,
        video_data=data,
        expected_width=expected_width,
        expected_height=expected_height,
        expected_fps=expected_fps,
        expected_duration_ms=expected_duration_ms,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        actual_width=actual_width if actual_width is not None else expected_width,
        actual_height=actual_height if actual_height is not None else expected_height,
        actual_fps=actual_fps if actual_fps is not None else float(expected_fps),
        **kwargs,
    )


# --- K-117 Container Tests ---


def test_container_check_passes_for_valid_mp4() -> None:
    ctx = make_context(duration_ms=25_000)
    check = check_container(ctx)
    assert check.name == CONTAINER_CHECK_NAME
    assert check.status == QCStatus.PASS
    assert "Valid MP4 container" in (check.detail or "")


def test_container_check_fails_for_empty_video() -> None:
    ctx = make_context(video_data=b"")
    check = check_container(ctx)
    assert check.status == QCStatus.FAIL
    assert "empty" in (check.detail or "")


def test_container_check_fails_for_corrupt_box() -> None:
    ctx = make_context(video_data=b"NOT_AN_MP4_FILE_AT_ALL_JUST_RANDOM_GARBAGE")
    check = check_container(ctx)
    assert check.status == QCStatus.FAIL
    assert "Invalid MP4 container" in (check.detail or "")


# --- K-118 Resolution Tests ---


def test_resolution_check_passes_for_shorts_9_16() -> None:
    ctx = make_context(
        content_type=ContentType.SHORTS,
        expected_width=1080,
        expected_height=1920,
        actual_width=1080,
        actual_height=1920,
    )
    check = check_resolution(ctx)
    assert check.name == RESOLUTION_CHECK_NAME
    assert check.status == QCStatus.PASS
    assert "9:16 vertical" in (check.detail or "")


def test_resolution_check_passes_for_longform_16_9() -> None:
    ctx = make_context(
        content_type=ContentType.LONGFORM,
        expected_width=1920,
        expected_height=1080,
        actual_width=1920,
        actual_height=1080,
        min_duration_ms=180_000,
        max_duration_ms=14_400_000,
    )
    check = check_resolution(ctx)
    assert check.status == QCStatus.PASS
    assert "16:9 horizontal" in (check.detail or "")


def test_resolution_check_fails_when_aspect_ratio_inverted() -> None:
    ctx = make_context(
        content_type=ContentType.SHORTS,
        expected_width=1080,
        expected_height=1920,
        actual_width=1920,
        actual_height=1080,
    )
    check = check_resolution(ctx)
    assert check.status == QCStatus.FAIL
    assert "9:16 vertical" in (check.detail or "")


def test_resolution_check_fails_when_dimensions_differ() -> None:
    ctx = make_context(
        content_type=ContentType.SHORTS,
        expected_width=1080,
        expected_height=1920,
        actual_width=720,
        actual_height=1280,
    )
    check = check_resolution(ctx)
    assert check.status == QCStatus.FAIL
    assert "does not match expected" in (check.detail or "")


# --- K-119 FPS Tests ---


def test_fps_check_passes_for_exact_match() -> None:
    ctx = make_context(expected_fps=30, actual_fps=30.0)
    check = check_fps(ctx)
    assert check.name == FPS_CHECK_NAME
    assert check.status == QCStatus.PASS


def test_fps_check_warns_for_low_fps() -> None:
    ctx = make_context(expected_fps=15, actual_fps=15.0)
    check = check_fps(ctx)
    assert check.status == QCStatus.WARN
    assert "below standard minimum" in (check.detail or "")


def test_fps_check_fails_for_mismatched_fps() -> None:
    ctx = make_context(expected_fps=30, actual_fps=24.0)
    check = check_fps(ctx)
    assert check.status == QCStatus.FAIL
    assert "differs from configured" in (check.detail or "")


# --- K-120 Duration Tests ---


def test_duration_check_passes_for_shorts_within_bounds() -> None:
    ctx = make_context(
        content_type=ContentType.SHORTS,
        duration_ms=45_000,
        expected_duration_ms=45_000,
        min_duration_ms=1000,
        max_duration_ms=60_000,
    )
    check = check_duration(ctx)
    assert check.name == DURATION_CHECK_NAME
    assert check.status == QCStatus.PASS


def test_duration_check_fails_when_shorts_under_one_second() -> None:
    ctx = make_context(
        content_type=ContentType.SHORTS,
        duration_ms=500,
        expected_duration_ms=500,
    )
    check = check_duration(ctx)
    assert check.status == QCStatus.FAIL
    assert "under 1 second" in (check.detail or "")


def test_duration_check_fails_when_shorts_exceeds_max() -> None:
    ctx = make_context(
        content_type=ContentType.SHORTS,
        duration_ms=75_000,
        expected_duration_ms=75_000,
        max_duration_ms=60_000,
    )
    check = check_duration(ctx)
    assert check.status == QCStatus.FAIL
    assert "exceeds maximum" in (check.detail or "")


def test_duration_check_fails_when_longform_under_three_minutes() -> None:
    ctx = make_context(
        content_type=ContentType.LONGFORM,
        duration_ms=120_000,
        expected_duration_ms=120_000,
        min_duration_ms=180_000,
        max_duration_ms=14_400_000,
    )
    check = check_duration(ctx)
    assert check.status == QCStatus.FAIL
    assert "under 180 seconds" in (check.detail or "")


def test_duration_check_fails_when_audio_sync_drifts() -> None:
    ctx = make_context(
        content_type=ContentType.SHORTS,
        duration_ms=30_000,
        expected_duration_ms=35_000,
    )
    check = check_duration(ctx)
    assert check.status == QCStatus.FAIL
    assert "differs from audio" in (check.detail or "")


# --- K-121 Audio Tests ---


def test_audio_check_passes_for_clean_wav() -> None:
    wav_bytes = make_sine_wav(duration_ms=2000, amplitude=8000)
    ctx = make_context(
        duration_ms=2000,
        expected_duration_ms=2000,
        audio_data=wav_bytes,
    )
    check = check_audio(ctx)
    assert check.name == AUDIO_CHECK_NAME
    assert check.status == QCStatus.PASS
    assert "Valid audio track" in (check.detail or "")


def test_audio_check_fails_when_missing() -> None:
    ctx = make_context(audio_data=None)
    check = check_audio(ctx)
    assert check.status == QCStatus.FAIL
    assert "missing" in (check.detail or "")


def test_audio_check_fails_for_digital_silence() -> None:
    wav_bytes = make_sine_wav(duration_ms=2000, amplitude=0)
    ctx = make_context(
        duration_ms=2000,
        expected_duration_ms=2000,
        audio_data=wav_bytes,
    )
    check = check_audio(ctx)
    assert check.status == QCStatus.FAIL
    assert "audio.silence" in (check.detail or "")


def test_audio_check_fails_for_clipped_audio() -> None:
    wav_bytes = make_sine_wav(duration_ms=2000, amplitude=32767)
    ctx = make_context(
        duration_ms=2000,
        expected_duration_ms=2000,
        audio_data=wav_bytes,
    )
    check = check_audio(ctx)
    assert check.status == QCStatus.FAIL
    assert "audio.level" in (check.detail or "")


def test_audio_check_fails_when_audio_video_duration_drifts() -> None:
    wav_bytes = make_sine_wav(duration_ms=2000, amplitude=8000)
    ctx = make_context(
        duration_ms=4000,
        expected_duration_ms=2000,
        audio_data=wav_bytes,
    )
    check = check_audio(ctx)
    assert check.status == QCStatus.FAIL
    assert "drifts from video duration" in (check.detail or "")


# --- K-122 Subtitle Tests ---


VALID_SRT = (
    b"1\n00:00:00,000 --> 00:00:02,500\nWelcome to our channel\n\n"
    b"2\n00:00:02,500 --> 00:00:05,000\nSubscribe for more tips\n"
)

VALID_VTT = (
    b"WEBVTT\n\n"
    b"1\n00:00:00.000 --> 00:00:02.500\nWelcome to our channel\n\n"
    b"2\n00:00:02.500 --> 00:00:05.000\nSubscribe for more tips\n"
)


def test_subtitle_check_passes_for_valid_srt_and_vtt() -> None:
    ctx_srt = make_context(
        duration_ms=5000,
        expected_duration_ms=5000,
        captions_enabled=True,
        subtitle_data=VALID_SRT,
        subtitle_media_type="application/x-subrip",
        expected_caption_count=2,
        scene_windows_ms=((0, 2500), (2500, 5000)),
    )
    res_srt = check_subtitles(ctx_srt)
    assert res_srt.name == SUBTITLE_CHECK_NAME
    assert res_srt.status == QCStatus.PASS
    assert "Valid SRT subtitles" in (res_srt.detail or "")

    ctx_vtt = make_context(
        duration_ms=5000,
        expected_duration_ms=5000,
        captions_enabled=True,
        subtitle_data=VALID_VTT,
        subtitle_media_type="text/vtt",
        expected_caption_count=2,
    )
    res_vtt = check_subtitles(ctx_vtt)
    assert res_vtt.status == QCStatus.PASS
    assert "Valid WebVTT subtitles" in (res_vtt.detail or "")


def test_subtitle_check_passes_when_captions_disabled() -> None:
    ctx = make_context(captions_enabled=False, subtitle_data=None)
    check = check_subtitles(ctx)
    assert check.status == QCStatus.PASS


def test_subtitle_check_fails_when_missing_or_bom_or_crlf() -> None:
    assert (
        check_subtitles(make_context(captions_enabled=True, subtitle_data=None)).status
        == QCStatus.FAIL
    )
    assert (
        check_subtitles(
            make_context(
                captions_enabled=True, subtitle_data=b"\xef\xbb\xbf" + VALID_SRT
            )
        ).status
        == QCStatus.FAIL
    )
    assert (
        check_subtitles(
            make_context(
                captions_enabled=True,
                subtitle_data=VALID_SRT.replace(b"\n", b"\r\n"),
            )
        ).status
        == QCStatus.FAIL
    )


def test_subtitle_check_fails_on_overlapping_cues_or_missing_scene_coverage() -> None:
    overlapping_srt = (
        b"1\n00:00:00,000 --> 00:00:03,000\nFirst line\n\n"
        b"2\n00:00:02,000 --> 00:00:04,000\nOverlapping second line\n"
    )
    ctx_overlap = make_context(
        duration_ms=5000,
        expected_duration_ms=5000,
        subtitle_data=overlapping_srt,
    )
    assert check_subtitles(ctx_overlap).status == QCStatus.FAIL

    ctx_uncovered = make_context(
        duration_ms=10_000,
        expected_duration_ms=10_000,
        subtitle_data=VALID_SRT,
        scene_windows_ms=((0, 2500), (2500, 5000), (5000, 10_000)),
    )
    res_uncovered = check_subtitles(ctx_uncovered)
    assert res_uncovered.status == QCStatus.FAIL
    assert "Missing captions for scene 3" in (res_uncovered.detail or "")


# --- K-123 Black Frame Tests ---


def test_black_frame_check_passes_for_normal_video() -> None:
    ctx = make_context(
        frame_luma_samples=[0.35, 0.42, 0.51, 0.38, 0.60, 0.47],
    )
    check = check_black_frames(ctx)
    assert check.name == BLACK_FRAME_CHECK_NAME
    assert check.status == QCStatus.PASS


def test_black_frame_check_fails_for_all_zero_mdat_or_excessive_black_luma() -> None:
    black_mp4 = stub_mp4(5000, b"\x00" * 1024)
    assert (
        check_black_frames(make_context(video_data=black_mp4)).status == QCStatus.FAIL
    )

    ctx_luma_black = make_context(
        frame_luma_samples=[0.0, 0.01, 0.0, 0.5, 0.6],
    )
    assert check_black_frames(ctx_luma_black).status == QCStatus.FAIL


def test_black_frame_check_fails_for_frozen_frames() -> None:
    frozen_mp4 = stub_mp4(5000, b"\x7f" * 1024)
    assert (
        check_black_frames(make_context(video_data=frozen_mp4)).status == QCStatus.FAIL
    )

    ctx_hashes = make_context(
        frame_hashes=["h1", "h2", "h2", "h2", "h2", "h2", "h2"],
    )
    assert check_black_frames(ctx_hashes).status == QCStatus.FAIL


# --- K-124 Render Integrity Tests ---


def test_render_integrity_passes_for_valid_mp4_and_matching_lineage() -> None:
    raw = stub_mp4(10_000, b"render-ok-payload")
    sha = hashlib.sha256(raw).hexdigest()
    ctx = make_context(
        video_data=raw,
        expected_video_sha256=sha,
        expected_video_size=len(raw),
        render_job_status="succeeded",
        render_job_video_sha256=sha,
        render_job_timeline_sha256="timeline-sha-1",
        current_timeline_sha256="timeline-sha-1",
    )
    check = check_render_integrity(ctx)
    assert check.name == INTEGRITY_CHECK_NAME
    assert check.status == QCStatus.PASS


def test_render_integrity_fails_for_truncated_mp4_or_stale_timeline() -> None:
    raw = stub_mp4(10_000, b"render-ok-payload")
    truncated = raw[:-10]
    assert (
        check_render_integrity(make_context(video_data=truncated)).status
        == QCStatus.FAIL
    )

    sha = hashlib.sha256(raw).hexdigest()
    ctx_stale = make_context(
        video_data=raw,
        expected_video_sha256=sha,
        expected_video_size=len(raw),
        render_job_status="succeeded",
        render_job_video_sha256=sha,
        render_job_timeline_sha256="old-timeline-sha",
        current_timeline_sha256="new-timeline-sha",
    )
    res_stale = check_render_integrity(ctx_stale)
    assert res_stale.status == QCStatus.FAIL
    assert "timeline changed" in (res_stale.detail or "")


# --- K-125 Metadata Tests ---


def test_metadata_check_passes_and_detects_staleness() -> None:
    shorts_meta = ShortsMetadata(
        project_id="proj-1",
        project_version=2,
        content_item_id="item-qc-1",
        channel_id="ch-1",
        script_id="scr-1",
        script_version=1,
        strategy_version=1,
        title="Quick AI Coding Tip #Shorts",
        description="Learn this coding tip!\n\nMusic by CC Artist\n\n#Shorts",
        tags=("shorts", "coding"),
        language="en-US",
        made_for_kids=False,
        contains_synthetic_media=True,
        disclosure_decision_id="disc-1",
        policy_rule_set_id="policy",
        policy_rule_set_version=1,
        policy_status=PolicyStatus.PASS,
    )
    ctx_ok = make_context(
        content_type=ContentType.SHORTS,
        metadata_data=shorts_meta.to_json_bytes(),
        expected_project_id="proj-1",
        expected_project_version=2,
        expected_script_id="scr-1",
        expected_script_version=1,
        expected_language="en-US",
        expected_attributions=("Music by CC Artist",),
        expected_disclosure_id="disc-1",
        expected_synthetic_media=True,
    )
    res_ok = check_metadata(ctx_ok)
    assert res_ok.name == METADATA_CHECK_NAME
    assert res_ok.status == QCStatus.PASS

    ctx_stale = make_context(
        content_type=ContentType.SHORTS,
        metadata_data=shorts_meta.to_json_bytes(),
        expected_project_id="proj-1",
        expected_project_version=3,
    )
    res_stale = check_metadata(ctx_stale)
    assert res_stale.status == QCStatus.FAIL
    assert "stale" in (res_stale.detail or "").lower()


def test_metadata_check_validates_longform_chapters_in_description() -> None:
    chapters = (
        ChapterMetadata(title="Intro", start_ms=0, end_ms=60_000),
        ChapterMetadata(title="Deep Dive", start_ms=60_000, end_ms=140_000),
        ChapterMetadata(title="Summary", start_ms=140_000, end_ms=200_000),
    )
    lf_meta_missing_desc_chapters = LongFormMetadata(
        project_id="lf-proj-1",
        project_version=1,
        content_item_id="item-qc-1",
        channel_id="ch-1",
        script_id="scr-lf-1",
        script_version=1,
        strategy_version=1,
        title="Complete Architecture Guide",
        description="A deep dive without chapter timestamps.",
        tags=("architecture",),
        chapters=chapters,
        language="en-US",
        made_for_kids=False,
        contains_synthetic_media=False,
        disclosure_decision_id="disc-lf-1",
        policy_rule_set_id="policy",
        policy_rule_set_version=1,
        policy_status=PolicyStatus.PASS,
    )
    ctx_missing_ch = make_context(
        content_type=ContentType.LONGFORM,
        duration_ms=200_000,
        expected_duration_ms=200_000,
        expected_width=1920,
        expected_height=1080,
        metadata_data=lf_meta_missing_desc_chapters.to_bytes(),
        expected_chapters_enabled=True,
        expected_chapter_count=3,
    )
    res = check_metadata(ctx_missing_ch)
    assert res.status == QCStatus.FAIL
    assert "missing formatted YouTube chapters" in (res.detail or "")


# --- K-116 Test Harness Integration Tests ---


def test_qc_harness_inspect_and_record(database: Database, tmp_path: Path) -> None:
    """Validate that VideoArtifactTestHarness executes checks.

    It also stores QCResult and audits.
    """
    from factories import make_channel, make_strategy_profile

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    storage = LocalFileStorage(tmp_path / "artifacts")
    harness = VideoArtifactTestHarness(database, storage, audit, clock=at(T0))

    with database.transaction() as conn:
        channel = make_channel()
        ChannelRepository(conn).add(channel)

        strategy = make_strategy_profile(channel)
        StrategyProfileRepository(conn).add(strategy)

        item = ContentItem.create(
            channel.id,
            strategy.id,
            strategy.version,
            ContentType.SHORTS,
            "QC Test Item",
            clock=at(T0),
        )
        ContentItemRepository(conn).add(item)

        duration_ms = 30_000
        video_bytes = stub_mp4(duration_ms, b"video-frame-bytes")
        video_sha = hashlib.sha256(video_bytes).hexdigest()

    file_stored = storage.write(
        item.id, ArtifactKind.VIDEO, 1, video_bytes, media_type="video/mp4"
    )

    with database.transaction() as conn:
        artifact = Artifact.create(
            item.id,
            ArtifactKind.VIDEO,
            uri=file_stored.uri,
            sha256=video_sha,
            size_bytes=len(video_bytes),
            media_type="video/mp4",
            clock=at(T0),
        )
        ArtifactRepository(conn).add(artifact)

    qc_result = harness.inspect_and_record(
        item.id,
        actor=USER,
        actual_width=1080,
        actual_height=1920,
        actual_fps=30.0,
    )

    assert isinstance(qc_result, QCResult)
    assert qc_result.content_item_id == item.id
    assert qc_result.status == QCStatus.PASS
    assert len(qc_result.checks) == 4

    check_names = {c.name for c in qc_result.checks}
    assert check_names == {
        CONTAINER_CHECK_NAME,
        RESOLUTION_CHECK_NAME,
        FPS_CHECK_NAME,
        DURATION_CHECK_NAME,
    }

    with database.transaction() as conn:
        stored_res = QCResultRepository(conn).get(qc_result.id)
        assert stored_res is not None
        assert stored_res.id == qc_result.id
        assert len(stored_res.checks) == 4

    events = [e for e in sink.events() if e.action == "qc.evaluated"]
    assert len(events) == 1
    assert events[0].metadata["overall_status"] == "pass"
    assert events[0].metadata["checks_count"] == 4
    assert len(all_video_checks()) == 9


def test_qc_harness_missing_video_raises_error(
    database: Database, tmp_path: Path
) -> None:
    from factories import make_channel, make_strategy_profile

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    storage = LocalFileStorage(tmp_path / "artifacts")
    harness = VideoArtifactTestHarness(database, storage, audit, clock=at(T0))

    with database.transaction() as conn:
        channel = make_channel()
        ChannelRepository(conn).add(channel)
        strategy = make_strategy_profile(channel)
        StrategyProfileRepository(conn).add(strategy)
        item = ContentItem.create(
            channel.id,
            strategy.id,
            strategy.version,
            ContentType.SHORTS,
            "Item",
            clock=at(T0),
        )
        ContentItemRepository(conn).add(item)

    with pytest.raises(QCHarnessError) as exc_info:
        harness.inspect_and_record(item.id, actor=USER)
    assert exc_info.value.code == QCHarnessErrorCode.VIDEO_MISSING
