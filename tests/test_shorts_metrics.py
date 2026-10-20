"""Tests for Shorts metrics normalisation (N-161, prompt #161)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.shorts_metrics import (
    SHORTS_METRICS_CATALOGUE,
    ShortsMetricsError,
    ShortsMetricsErrorCode,
    ShortsMetricsService,
    normalize_shorts_metrics,
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
    subject_id: str = "vid000001ab",
    **metrics: str | int,
) -> MetricSnapshot:
    return MetricSnapshot(
        id="snap-shorts-1",
        scope=scope,
        subject_id=subject_id,
        source="mock_analytics",
        period_start=START,
        period_end=END,
        retrieved_at=RETRIEVED,
        metrics={name: Decimal(str(value)) for name, value in metrics.items()},
    )


def test_normalises_available_shorts_metrics_and_derives_rates() -> None:
    snapshot = _snapshot(
        views=1000,
        engaged_views=750,
        swiped_away=250,
        shorts_feed_impressions=1400,
        estimated_minutes_watched="12.5",
        average_view_duration="45",
        average_view_percentage="150",
        likes=80,
        comments=12,
        shares=15,
        subscribers_gained=9,
        subscribers_lost=2,
    )

    view = normalize_shorts_metrics(
        snapshot,
        content_item_id="item-1",
        video_duration_seconds=Decimal("30"),
    )

    assert view.snapshot_id == "snap-shorts-1"
    assert view.youtube_video_id == "vid000001ab"
    assert view.content_item_id == "item-1"
    assert view.views == Decimal("1000")
    assert view.engaged_views == Decimal("750")
    assert view.swiped_away == Decimal("250")
    assert view.shorts_feed_impressions == Decimal("1400")
    assert view.estimated_minutes_watched == Decimal("12.5")
    assert view.watch_time_seconds == Decimal("750.00")
    assert view.average_view_duration_seconds == Decimal("45")
    assert view.average_view_percentage == Decimal("150")
    assert view.stay_to_watch_rate == Decimal("75.00")
    assert view.loop_rate == Decimal("1.50")
    assert view.like_rate == Decimal("8.00")
    assert view.share_rate == Decimal("1.50")
    assert view.available_metrics == SHORTS_METRICS_CATALOGUE
    assert view.missing_metrics == ()
    assert view.as_dict()["stay_to_watch_rate"] == "75.00"
    assert view.as_dict()["loop_rate"] == "1.50"


def test_absent_metrics_stay_none_and_never_default_to_zero() -> None:
    snapshot = _snapshot(views=200)

    view = normalize_shorts_metrics(snapshot)

    assert view.views == Decimal("200")
    assert view.engaged_views is None
    assert view.swiped_away is None
    assert view.shorts_feed_impressions is None
    assert view.estimated_minutes_watched is None
    assert view.watch_time_seconds is None
    assert view.average_view_duration_seconds is None
    assert view.average_view_percentage is None
    assert view.likes is None
    assert view.comments is None
    assert view.shares is None
    assert view.subscribers_gained is None
    assert view.subscribers_lost is None
    assert view.stay_to_watch_rate is None
    assert view.loop_rate is None
    assert view.like_rate is None
    assert view.share_rate is None
    assert view.available_metrics == ("views",)
    assert "engaged_views" in view.missing_metrics
    assert "average_view_percentage" in view.missing_metrics


def test_refuses_channel_scope_or_invalid_shorts_duration() -> None:
    channel_snapshot = _snapshot(scope=MetricScope.CHANNEL, views=10)
    with pytest.raises(ShortsMetricsError) as caught:
        normalize_shorts_metrics(channel_snapshot)
    assert caught.value.metrics_code is ShortsMetricsErrorCode.INVALID_SCOPE

    video_snapshot = _snapshot(views=10)
    for invalid_duration in (0, -5, 181, Decimal("180.01")):
        with pytest.raises(ShortsMetricsError) as duration_err:
            normalize_shorts_metrics(
                video_snapshot, video_duration_seconds=invalid_duration
            )
        assert duration_err.value.metrics_code is ShortsMetricsErrorCode.INVALID_DURATION


def test_service_normalises_published_shorts_and_refuses_longform(
    database: Database,
) -> None:
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    short_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.PUBLISHED,
    )
    long_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        status=ContentStatus.PUBLISHED,
    )
    short_video = make_artifact(short_item, ArtifactKind.VIDEO)
    long_video = make_artifact(long_item, ArtifactKind.VIDEO)
    short_approval = make_approval_request(short_item, [short_video]).approve(USER)
    long_approval = make_approval_request(long_item, [long_video]).approve(USER)
    short_job = (
        PublishJob.create(
            short_approval,
            ContentType.SHORTS,
            "pub:short-1",
            clock=lambda: PUBLISHED,
        )
        .start(clock=lambda: PUBLISHED)
        .succeed("vid000001ab", clock=lambda: PUBLISHED)
    )
    long_job = (
        PublishJob.create(
            long_approval,
            ContentType.LONGFORM,
            "pub:long-1",
            clock=lambda: PUBLISHED,
        )
        .start(clock=lambda: PUBLISHED)
        .succeed("vid000002ab", clock=lambda: PUBLISHED)
    )
    snapshot = _snapshot(
        subject_id="vid000001ab",
        views=500,
        engaged_views=400,
        swiped_away=100,
        average_view_duration=24,
    )
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        ContentItemRepository(connection).add(short_item)
        ContentItemRepository(connection).add(long_item)
        ArtifactRepository(connection).add(short_video)
        ArtifactRepository(connection).add(long_video)
        ApprovalRequestRepository(connection).add(short_approval)
        ApprovalRequestRepository(connection).add(long_approval)
        PublishJobRepository(connection).add(short_job)
        PublishJobRepository(connection).add(long_job)
        MetricSnapshotRepository(connection).add(snapshot)

    service = ShortsMetricsService(database, PublishResultService(database))
    views = service.normalize_for_item(
        short_item.id, video_duration_seconds=Decimal("30")
    )
    assert len(views) == 1
    assert views[0].stay_to_watch_rate == Decimal("80.00")
    assert views[0].loop_rate == Decimal("0.80")
    assert service.latest_for_item(short_item.id) == views[0]

    by_video = service.normalize_for_video("vid000001ab")
    assert len(by_video) == 1 and by_video[0].content_item_id == short_item.id

    with pytest.raises(ShortsMetricsError) as caught:
        service.normalize_for_item(long_item.id)
    assert caught.value.metrics_code is ShortsMetricsErrorCode.NOT_A_SHORT

    with pytest.raises(ShortsMetricsError) as video_caught:
        service.normalize_for_video("vid000002ab")
    assert video_caught.value.metrics_code is ShortsMetricsErrorCode.NOT_A_SHORT
