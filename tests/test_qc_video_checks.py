"""Tests for Phase K video QC checks (prompts K-116..K-120).

Validates:
- K-116: VideoArtifactTestHarness orchestration, check execution, persistence and audit;
- K-117: check_container with valid MP4, empty bytes, corrupt headers and unrecognized brands;
- K-118: check_resolution for Shorts (9:16 vertical) and LongForm (16:9 horizontal);
- K-119: check_fps for target matching, low fps warning, and invalid fps;
- K-120: check_duration for content type bounds (Shorts 1..180s, LongForm 181..14400s)
  and audio synchronization tolerance.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from factories import USER, at

from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.qc_harness import (
    QCHarnessError,
    QCHarnessErrorCode,
    VideoArtifactTestHarness,
)
from ai_youtube_agent.content.qc_video_checks import (
    CONTAINER_CHECK_NAME,
    DURATION_CHECK_NAME,
    FPS_CHECK_NAME,
    RESOLUTION_CHECK_NAME,
    VideoInspectionContext,
    check_container,
    check_duration,
    check_fps,
    check_resolution,
)
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
    AudioMetadataRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.providers.mock_video_render import stub_mp4

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


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
) -> VideoInspectionContext:
    data = video_data if video_data is not None else stub_mp4(duration_ms, b"dummy-frame-data")
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
    # Giving horizontal 1920x1080 to a Shorts item
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
    # 720x1280 (9:16) when 1080x1920 was expected
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
        duration_ms=120_000,  # 2 minutes (< 180s)
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
        expected_duration_ms=35_000,  # 5s difference (tolerance is ~250ms)
    )
    check = check_duration(ctx)
    assert check.status == QCStatus.FAIL
    assert "differs from audio" in (check.detail or "")


# --- K-116 Test Harness Integration Tests ---


def test_qc_harness_inspect_and_record(database: Database, tmp_path: Path) -> None:
    """Validate that VideoArtifactTestHarness executes checks, stores QCResult and audits."""
    import hashlib
    from factories import make_channel, make_strategy_profile

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    storage = LocalFileStorage(tmp_path / "artifacts")
    harness = VideoArtifactTestHarness(database, storage, audit, clock=at(T0))

    # Setup database entities
    with database.transaction() as conn:
        channel = make_channel()
        ChannelRepository(conn).add(channel)

        strategy = make_strategy_profile(channel.id)
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

        # Write mock video artifact
        duration_ms = 30_000
        video_bytes = stub_mp4(duration_ms, b"video-frame-bytes")
        video_sha = hashlib.sha256(video_bytes).hexdigest()

    file_stored = storage.write(video_bytes, item.id, ArtifactKind.VIDEO, 1, "video/mp4")

    with database.transaction() as conn:
        artifact = Artifact.create(
            item.id,
            ArtifactKind.VIDEO,
            1,
            file_stored.uri,
            video_sha,
            len(video_bytes),
            "video/mp4",
            clock=at(T0),
        )
        ArtifactRepository(conn).add(artifact)

    # Run inspection harness
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

    # Verify check names match K-117 through K-120
    check_names = {c.name for c in qc_result.checks}
    assert check_names == {
        CONTAINER_CHECK_NAME,
        RESOLUTION_CHECK_NAME,
        FPS_CHECK_NAME,
        DURATION_CHECK_NAME,
    }

    # Verify persistence in QCResultRepository
    with database.transaction() as conn:
        stored_res = QCResultRepository(conn).get(qc_result.id)
        assert stored_res is not None
        assert stored_res.id == qc_result.id
        assert len(stored_res.checks) == 4

    # Verify audit event
    events = [e for e in sink.events if e.action == "qc.evaluated"]
    assert len(events) == 1
    assert events[0].metadata["overall_status"] == "pass"
    assert events[0].metadata["checks_count"] == 4


def test_qc_harness_missing_video_raises_error(database: Database, tmp_path: Path) -> None:
    from factories import make_channel, make_strategy_profile

    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    storage = LocalFileStorage(tmp_path / "artifacts")
    harness = VideoArtifactTestHarness(database, storage, audit, clock=at(T0))

    with database.transaction() as conn:
        channel = make_channel()
        ChannelRepository(conn).add(channel)
        strategy = make_strategy_profile(channel.id)
        StrategyProfileRepository(conn).add(strategy)
        item = ContentItem.create(
            channel.id, strategy.id, strategy.version, ContentType.SHORTS, "Item", clock=at(T0)
        )
        ContentItemRepository(conn).add(item)

    with pytest.raises(QCHarnessError) as exc_info:
        harness.inspect_and_record(item.id, actor=USER)
    assert exc_info.value.code == QCHarnessErrorCode.VIDEO_MISSING
