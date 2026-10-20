"""Tests for Analytics Screen view and service (Prompt Pack v8, prompt Q-196)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.dashboard_analytics import AnalyticsScreenService
from ai_youtube_agent.content.dashboard_shell import DashboardScreenId, ViewLoadState
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
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
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _seed_published_item(
    db: Database,
    channel: Channel,
    profile: StrategyProfile,
    *,
    content_type: ContentType,
    title: str,
    youtube_video_id: str,
) -> ContentItem:
    item = make_content_item(
        channel,
        profile,
        content_type=content_type,
        title=title,
        status=ContentStatus.PUBLISHED,
    )
    video_art = make_artifact(item, ArtifactKind.VIDEO, media_type="video/mp4")
    approval = make_approval_request(item, [video_art]).approve(USER)
    job = (
        PublishJob.create(
            approval,
            content_type,
            f"idem-{youtube_video_id}",
            clock=lambda: FIXED_NOW,
        )
        .start(clock=lambda: FIXED_NOW)
        .succeed(
            youtube_video_id,
            published_at=FIXED_NOW - timedelta(days=5),
            clock=lambda: FIXED_NOW,
        )
    )
    with db.transaction() as conn:
        ContentItemRepository(conn).add(item)
        ArtifactRepository(conn).add(video_art)
        ApprovalRequestRepository(conn).add(approval)
        PublishJobRepository(conn).add(job)
    return item


def test_analytics_screen_empty_when_no_channels(database: Database) -> None:
    service = AnalyticsScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.channel_id is None
    assert view.shorts.published_video_count == 0
    assert view.longform.published_video_count == 0


def test_analytics_screen_time_range_and_shorts_vs_longform(
    database: Database,
) -> None:
    channel = make_channel(title="Analytics Lab", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)

    _seed_published_item(
        database,
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Short #1",
        youtube_video_id="dQw4w9WgXcQ",
    )
    _seed_published_item(
        database,
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        title="LongForm #1",
        youtube_video_id="9bZkp7q19f0",
    )

    p_start = FIXED_NOW - timedelta(days=4)
    p_end = FIXED_NOW - timedelta(days=3)

    chan_snap = MetricSnapshot.create(
        MetricScope.CHANNEL,
        channel.id,
        source="youtube_analytics",
        period_start=p_start,
        period_end=p_end,
        metrics={
            "views": Decimal("5000"),
            "estimated_minutes_watched": Decimal("900"),
            "average_view_percentage": Decimal("62.5"),
            "impressions": Decimal("40000"),
            "impressions_click_through_rate": Decimal("7.5"),
            "subscribers_gained": Decimal("50"),
            "subscribers_lost": Decimal("10"),
        },
        clock=lambda: FIXED_NOW - timedelta(hours=1),
    )
    short_snap = MetricSnapshot.create(
        MetricScope.VIDEO,
        "dQw4w9WgXcQ",
        source="youtube_analytics",
        period_start=p_start,
        period_end=p_end,
        metrics={
            "views": Decimal("3200"),
            "engaged_views": Decimal("2400"),
            "swiped_away": Decimal("800"),
            "estimated_minutes_watched": Decimal("300"),
            "average_view_percentage": Decimal("78.0"),
        },
        clock=lambda: FIXED_NOW - timedelta(hours=1),
    )
    long_snap = MetricSnapshot.create(
        MetricScope.VIDEO,
        "9bZkp7q19f0",
        source="youtube_analytics",
        period_start=p_start,
        period_end=p_end,
        metrics={
            "views": Decimal("1800"),
            "impressions": Decimal("25000"),
            "impressions_click_through_rate": Decimal("6.8"),
            "estimated_minutes_watched": Decimal("600"),
            "average_view_percentage": Decimal("51.2"),
        },
        clock=lambda: FIXED_NOW - timedelta(hours=1),
    )

    with database.transaction() as conn:
        repo = MetricSnapshotRepository(conn)
        repo.add(chan_snap)
        repo.add(short_snap)
        repo.add(long_snap)

    service = AnalyticsScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id, period_start=p_start, period_end=p_end)

    assert view.shell.active_screen is DashboardScreenId.ANALYTICS
    assert view.load_state is ViewLoadState.READY
    assert view.freshness_status == "fresh"
    assert view.channel_views_watch_time is not None
    assert view.channel_views_watch_time.views == Decimal("5000")
    assert view.channel_views_watch_time.watch_time_hours == Decimal("15.00")
    assert view.channel_retention_percentage == Decimal("62.5")
    assert view.channel_ctr_percentage == Decimal("7.5")
    assert len(view.period_rows) == 1

    assert view.shorts.published_video_count == 1
    assert view.shorts.total_views == Decimal("3200")
    assert view.shorts.total_engaged_views == Decimal("2400")
    assert view.shorts.stay_to_watch_rate == Decimal("75.00")
    assert view.shorts.subscribers_gained is None

    assert view.longform.published_video_count == 1
    assert view.longform.total_views == Decimal("1800")
    assert view.longform.total_watch_time_hours == Decimal("10.00")
    assert view.longform.impressions_click_through_rate == Decimal("6.80")

    rendered = service.render_html(view)
    assert 'data-testid="analytics-screen"' in rendered
    assert 'data-testid="analytics-shorts-card"' in rendered
    assert 'data-testid="analytics-longform-card"' in rendered
    assert "75.00%" in rendered

