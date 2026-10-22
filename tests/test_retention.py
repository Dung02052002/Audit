"""Tests for retention metrics sync and extraction (N-164, prompt #164)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.retention import (
    RETENTION_SYNCED,
    RetentionError,
    RetentionErrorCode,
    RetentionSync,
    extract_retention,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 5, tzinfo=UTC)


def _vid(n: int) -> str:
    return f"vid{n:06d}ab"


def _publish(database: Database, channel, profile, video_id: str) -> None:
    item = make_content_item(channel, profile, status=ContentStatus.PUBLISHED)
    video = make_artifact(item, ArtifactKind.VIDEO)
    approval = make_approval_request(item, [video]).approve(USER)
    job = (
        PublishJob.create(
            approval, ContentType.SHORTS, f"pub:{video_id}", clock=lambda: PUBLISHED
        )
        .start(clock=lambda: PUBLISHED)
        .succeed(video_id, clock=lambda: PUBLISHED)
    )
    with database.transaction() as connection:
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        PublishJobRepository(connection).add(job)


def test_extract_retention_reports_available_only_when_percentage_is_present() -> None:
    present = MetricSnapshot(
        id="s1",
        scope=MetricScope.VIDEO,
        subject_id=_vid(1),
        source="mock_analytics",
        period_start=START,
        period_end=END,
        retrieved_at=NOW,
        metrics={
            "average_view_percentage": Decimal("68.4"),
            "average_view_duration": Decimal("41.0"),
        },
    )
    view = extract_retention(present)
    assert view.available is True
    assert view.average_view_percentage == Decimal("68.4")
    assert view.average_view_duration_seconds == Decimal("41.0")

    missing = MetricSnapshot(
        id="s2",
        scope=MetricScope.VIDEO,
        subject_id=_vid(1),
        source="mock_analytics",
        period_start=START,
        period_end=END,
        retrieved_at=NOW,
        metrics={"views": Decimal("100")},
    )
    missing_view = extract_retention(missing)
    assert missing_view.available is False
    assert missing_view.average_view_percentage is None
    assert missing_view.average_view_duration_seconds is None


def test_extract_retention_refuses_negative_or_impossible_percentage() -> None:
    for bad_pct in (Decimal("-0.1"), Decimal("1000.1")):
        snap = MetricSnapshot(
            id="bad",
            scope=MetricScope.VIDEO,
            subject_id=_vid(1),
            source="mock_analytics",
            period_start=START,
            period_end=END,
            retrieved_at=NOW,
            metrics={"average_view_percentage": bad_pct},
        )
        with pytest.raises(RetentionError) as caught:
            extract_retention(snap)
        assert caught.value.retention_code is RetentionErrorCode.INVALID_RETENTION_VALUE


def test_sync_stores_retention_only_when_actually_available_and_is_idempotent(
    database: Database,
) -> None:
    now = NOW
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
    for n in (1, 2, 3):
        _publish(database, channel, profile, _vid(n))

    provider = MockAnalyticsProvider(clock=lambda: now)
    provider.set_channel_metrics(channel.youtube.channel_id, {"views": Decimal("1")})
    # Video 1 has full retention; Video 2 has only duration (no percentage); Video 3
    # has no data.
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(1),
        {
            "average_view_percentage": Decimal("72.5"),
            "average_view_duration": Decimal("29"),
        },
    )
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(2),
        {"average_view_duration": Decimal("15")},
    )

    sink = InMemoryAuditSink()
    sync = RetentionSync(
        database,
        provider,
        MockYouTubeOAuthProvider(clock=lambda: now),
        PublishResultService(database),
        AuditLog(sink),
        clock=lambda: now,
    )

    result = sync.sync(channel.id, period_start=START, period_end=END, actor=USER)

    assert [o.youtube_video_id for o in result.outcomes] == [_vid(1)]
    assert result.unavailable_video_ids == (_vid(2), _vid(3))
    assert result.created_count == 1 and result.unchanged_count == 0
    assert result.outcomes[0].retention.average_view_percentage == Decimal("72.5")
    assert result.outcomes[0].retention.average_view_duration_seconds == Decimal("29")

    # Video 2 and Video 3 must have NO stored snapshot (never defaulted to 0).
    assert sync.for_video(_vid(2)) == []
    assert sync.for_video(_vid(3)) == []
    latest = sync.latest_for_video(_vid(1))
    assert latest is not None and latest.average_view_percentage == Decimal("72.5")

    # Re-running with the same retention numbers stores nothing new.
    now = now + timedelta(minutes=10)
    repeat = sync.sync(channel.id, period_start=START, period_end=END, actor=USER)
    assert repeat.created_count == 0 and repeat.unchanged_count == 1
    assert len(sync.for_video(_vid(1))) == 1

    events = sink.events()
    assert events[-1].action == RETENTION_SYNCED
    assert events[-1].metadata["available_count"] == 1
    assert events[-1].metadata["unavailable_count"] == 2
