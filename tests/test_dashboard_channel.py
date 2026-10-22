"""Tests for Channel Overview screen view and service (Prompt Pack v8, prompt Q-193)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.dashboard_channel import ChannelOverviewService
from ai_youtube_agent.content.dashboard_shell import DashboardScreenId, ViewLoadState
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from factories import make_channel, make_content_item, make_strategy_profile

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_channel_overview_empty_when_no_channels(database: Database) -> None:
    service = ChannelOverviewService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.identity is None
    assert view.subscribers is None
    assert view.views_and_watch_time is None
    assert view.content_counts.total_items == 0
    html_out = service.render_html(view)
    assert 'data-testid="channel-empty-state"' in html_out


def test_channel_overview_aggregates_identity_subscribers_views_watch_time_and_counts(
    database: Database,
) -> None:
    channel = make_channel(
        title="DeepFocus Lab",
        status=ChannelStatus.ACTIVE,
    )
    profile = make_strategy_profile(channel)
    short_pub = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Pomodoro Focus Reset",
        status=ContentStatus.PUBLISHED,
    )
    long_draft = make_content_item(
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        title="Deep Work Architecture",
        status=ContentStatus.DRAFT,
    )

    p1_start = FIXED_NOW - timedelta(days=2)
    p1_end = FIXED_NOW - timedelta(days=1)
    p2_start = FIXED_NOW - timedelta(days=1)
    p2_end = FIXED_NOW

    snap1 = MetricSnapshot.create(
        MetricScope.CHANNEL,
        channel.id,
        source="youtube_analytics",
        period_start=p1_start,
        period_end=p1_end,
        metrics={
            "views": Decimal("1200"),
            "estimated_minutes_watched": Decimal("180"),
            "average_view_duration": Decimal("45"),
            "subscribers_gained": Decimal("25"),
            "subscribers_lost": Decimal("5"),
            "likes": Decimal("90"),
        },
        clock=lambda: FIXED_NOW - timedelta(hours=12),
    )
    snap2 = MetricSnapshot.create(
        MetricScope.CHANNEL,
        channel.id,
        source="youtube_analytics",
        period_start=p2_start,
        period_end=p2_end,
        metrics={
            "views": Decimal("800"),
            "estimated_minutes_watched": Decimal("120"),
            "average_view_duration": Decimal("60"),
            "subscribers_gained": Decimal("15"),
            "subscribers_lost": Decimal("3"),
            "likes": Decimal("60"),
        },
        clock=lambda: FIXED_NOW - timedelta(hours=1),
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(short_pub)
        ContentItemRepository(conn).add(long_draft)
        MetricSnapshotRepository(conn).add(snap1)
        MetricSnapshotRepository(conn).add(snap2)

    service = ChannelOverviewService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id)

    assert view.shell.active_screen is DashboardScreenId.CHANNEL
    assert view.load_state is ViewLoadState.READY
    assert view.identity is not None
    assert view.identity.title == "DeepFocus Lab"
    assert view.identity.strategy_complete is True
    assert view.identity.niche == "Personal finance"

    assert view.subscribers is not None
    assert view.subscribers.subscribers_gained == Decimal("40")
    assert view.subscribers.subscribers_lost == Decimal("8")
    assert view.subscribers.net_subscribers == Decimal("32")
    assert view.subscribers.included_periods == 2

    assert view.views_and_watch_time is not None
    assert view.views_and_watch_time.views == Decimal("2000")
    assert view.views_and_watch_time.watch_time_minutes == Decimal("300.00")
    assert view.views_and_watch_time.watch_time_hours == Decimal("5.00")
    assert view.views_and_watch_time.likes == Decimal("150")
    assert view.views_and_watch_time.comments is None

    assert view.content_counts.total_items == 2
    assert view.content_counts.shorts_total == 1
    assert view.content_counts.longform_total == 1
    assert view.content_counts.published_shorts == 1
    assert view.content_counts.in_progress_longform == 1

    rendered = service.render_html(view)
    assert 'data-testid="channel-overview-screen"' in rendered
    assert "DeepFocus Lab" in rendered
    assert "+32" in rendered
    assert "2000" in rendered


def test_channel_overview_keeps_missing_metrics_none(database: Database) -> None:
    channel = make_channel(status=ChannelStatus.ACTIVE)
    snap = MetricSnapshot.create(
        MetricScope.CHANNEL,
        channel.id,
        source="youtube_analytics",
        period_start=FIXED_NOW - timedelta(days=1),
        period_end=FIXED_NOW,
        metrics={"subscribers_gained": Decimal("10")},
        clock=lambda: FIXED_NOW,
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        MetricSnapshotRepository(conn).add(snap)

    service = ChannelOverviewService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id)

    assert view.subscribers is not None
    assert view.subscribers.net_subscribers is None
    assert view.subscribers.incomplete_period_count == 1
    assert view.views_and_watch_time is not None
    assert view.views_and_watch_time.views is None
    assert view.views_and_watch_time.watch_time_minutes is None
