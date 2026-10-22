"""Tests for LongForm metrics normalisation (N-162, prompt #162)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.longform_metrics import (
    LONGFORM_METRICS_CATALOGUE,
    LongFormMetricsError,
    LongFormMetricsErrorCode,
    LongFormMetricsService,
    normalize_longform_metrics,
)
from ai_youtube_agent.core.artifact import ArtifactKind
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
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)
RETRIEVED = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 5, tzinfo=UTC)


def _snapshot(
    *,
    scope: MetricScope = MetricScope.VIDEO,
    subject_id: str = "vid000010ab",
    **metrics: str | int,
) -> MetricSnapshot:
    return MetricSnapshot(
        id="snap-longform-1",
        scope=scope,
        subject_id=subject_id,
        source="mock_analytics",
        period_start=START,
        period_end=END,
        retrieved_at=RETRIEVED,
        metrics={name: Decimal(str(value)) for name, value in metrics.items()},
    )


def test_normalises_available_longform_metrics_and_derives_units_and_rates() -> None:
    snapshot = _snapshot(
        views=2000,
        impressions=25000,
        impressions_click_through_rate="6.4",
        estimated_minutes_watched=900,
        average_view_duration=270,
        average_view_percentage=45,
        likes=140,
        comments=30,
        shares=20,
        subscribers_gained=25,
        subscribers_lost=3,
    )

    view = normalize_longform_metrics(
        snapshot,
        content_item_id="item-lf-1",
        video_duration_seconds=Decimal("600"),
    )

    assert view.snapshot_id == "snap-longform-1"
    assert view.youtube_video_id == "vid000010ab"
    assert view.content_item_id == "item-lf-1"
    assert view.views == Decimal("2000")
    assert view.impressions == Decimal("25000")
    assert view.impressions_click_through_rate == Decimal("6.4")
    assert view.estimated_minutes_watched == Decimal("900")
    assert view.watch_time_hours == Decimal("15.00")
    assert view.watch_time_seconds == Decimal("54000.00")
    assert view.average_view_duration_seconds == Decimal("270")
    assert view.average_view_duration_minutes == Decimal("4.50")
    assert view.average_view_percentage == Decimal("45")
    assert view.like_rate == Decimal("7.00")
    assert view.comment_rate == Decimal("1.50")
    assert view.share_rate == Decimal("1.00")
    assert view.subscriber_conversion_rate == Decimal("1.25")
    assert view.available_metrics == LONGFORM_METRICS_CATALOGUE
    assert view.missing_metrics == ()
    assert view.as_dict()["watch_time_hours"] == "15.00"
    assert view.as_dict()["average_view_duration_minutes"] == "4.50"


def test_absent_longform_metrics_remain_none_never_zero() -> None:
    snapshot = _snapshot(views=500)

    view = normalize_longform_metrics(snapshot)

    assert view.views == Decimal("500")
    assert view.impressions is None
    assert view.impressions_click_through_rate is None
    assert view.estimated_minutes_watched is None
    assert view.watch_time_hours is None
    assert view.watch_time_seconds is None
    assert view.average_view_duration_seconds is None
    assert view.average_view_duration_minutes is None
    assert view.average_view_percentage is None
    assert view.like_rate is None
    assert view.comment_rate is None
    assert view.share_rate is None
    assert view.subscriber_conversion_rate is None
    assert view.available_metrics == ("views",)
    assert "impressions_click_through_rate" in view.missing_metrics
    assert "average_view_percentage" in view.missing_metrics


def test_refuses_channel_scope_or_out_of_range_longform_duration() -> None:
    channel_snapshot = _snapshot(scope=MetricScope.CHANNEL, views=10)
    with pytest.raises(LongFormMetricsError) as caught:
        normalize_longform_metrics(channel_snapshot)
    assert caught.value.metrics_code is LongFormMetricsErrorCode.INVALID_SCOPE

    video_snapshot = _snapshot(views=10)
    for bad_duration in (0, 60, 180, 14401):
        with pytest.raises(LongFormMetricsError) as dur_err:
            normalize_longform_metrics(
                video_snapshot, video_duration_seconds=bad_duration
            )
        assert dur_err.value.metrics_code is LongFormMetricsErrorCode.INVALID_DURATION


def test_service_normalises_published_longform_and_refuses_shorts(
    database: Database,
) -> None:
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    long_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        status=ContentStatus.PUBLISHED,
    )
    short_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.PUBLISHED,
    )
    long_video = make_artifact(long_item, ArtifactKind.VIDEO)
    short_video = make_artifact(short_item, ArtifactKind.VIDEO)
    long_approval = make_approval_request(long_item, [long_video]).approve(USER)
    short_approval = make_approval_request(short_item, [short_video]).approve(USER)
    long_job = (
        PublishJob.create(
            long_approval,
            ContentType.LONGFORM,
            "pub:long-10",
            clock=lambda: PUBLISHED,
        )
        .start(clock=lambda: PUBLISHED)
        .succeed("vid000010ab", clock=lambda: PUBLISHED)
    )
    short_job = (
        PublishJob.create(
            short_approval,
            ContentType.SHORTS,
            "pub:short-10",
            clock=lambda: PUBLISHED,
        )
        .start(clock=lambda: PUBLISHED)
        .succeed("vid000011ab", clock=lambda: PUBLISHED)
    )
    snapshot = _snapshot(
        subject_id="vid000010ab",
        views=1000,
        estimated_minutes_watched=300,
        average_view_duration=180,
    )
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        ContentItemRepository(connection).add(long_item)
        ContentItemRepository(connection).add(short_item)
        ArtifactRepository(connection).add(long_video)
        ArtifactRepository(connection).add(short_video)
        ApprovalRequestRepository(connection).add(long_approval)
        ApprovalRequestRepository(connection).add(short_approval)
        PublishJobRepository(connection).add(long_job)
        PublishJobRepository(connection).add(short_job)
        MetricSnapshotRepository(connection).add(snapshot)

    service = LongFormMetricsService(database, PublishResultService(database))
    views = service.normalize_for_item(
        long_item.id, video_duration_seconds=Decimal("600")
    )
    assert len(views) == 1
    assert views[0].watch_time_hours == Decimal("5.00")
    assert views[0].average_view_duration_minutes == Decimal("3.00")
    assert (
        service.latest_for_item(long_item.id, video_duration_seconds=Decimal("600"))
        == views[0]
    )

    by_video = service.normalize_for_video("vid000010ab")
    assert len(by_video) == 1 and by_video[0].content_item_id == long_item.id

    with pytest.raises(LongFormMetricsError) as caught:
        service.normalize_for_item(short_item.id)
    assert caught.value.metrics_code is LongFormMetricsErrorCode.NOT_LONGFORM

    with pytest.raises(LongFormMetricsError) as video_caught:
        service.normalize_for_video("vid000011ab")
    assert video_caught.value.metrics_code is LongFormMetricsErrorCode.NOT_LONGFORM
