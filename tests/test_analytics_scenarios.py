"""End-to-end Phase N analytics scenarios (N-169, prompt #169).

Covers pagination, missing fields (never zero-filled), stale/lagging data, cache
invalidation and fallback, Shorts/LongForm normalisation, watch-time and subscriber
period aggregation, and provider failures.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.analytics_freshness import (
    AnalyticsFreshnessService,
    FreshnessStatus,
)
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.channel_metrics import (
    ChannelMetricsSync,
    MetricsSyncError,
    MetricsSyncErrorCode,
)
from ai_youtube_agent.content.ctr import CTRSync
from ai_youtube_agent.content.longform_metrics import LongFormMetricsService
from ai_youtube_agent.content.retention import RetentionSync
from ai_youtube_agent.content.shorts_metrics import ShortsMetricsService
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.subscriber_change import (
    SubscriberChangeError,
    SubscriberChangeErrorCode,
    SubscriberChangeService,
)
from ai_youtube_agent.content.video_metrics import VideoMetricsSync
from ai_youtube_agent.content.watch_time import (
    WatchTimeError,
    WatchTimeErrorCode,
    WatchTimeService,
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
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.analytics import (
    AnalyticsErrorCode,
    AnalyticsProviderError,
)
from ai_youtube_agent.providers.analytics_cache import (
    AnalyticsCache,
    AnalyticsCacheOutcome,
)
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import OAuthScope
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
PUBLISHED = datetime(2026, 10, 3, tzinfo=UTC)


def _vid(n: int) -> str:
    return f"vid{n:06d}ab"


def _publish_item(
    database: Database,
    channel: Channel,
    profile: StrategyProfile,
    youtube_video_id: str,
    *,
    content_type: ContentType = ContentType.SHORTS,
    published_at: datetime = PUBLISHED,
):
    item = make_content_item(
        channel,
        profile,
        content_type=content_type,
        status=ContentStatus.PUBLISHED,
    )
    video = make_artifact(item, ArtifactKind.VIDEO)
    approval = make_approval_request(item, [video]).approve(USER)
    job = (
        PublishJob.create(
            approval, content_type, f"pub:{youtube_video_id}", clock=lambda: published_at
        )
        .start(clock=lambda: published_at)
        .succeed(youtube_video_id, clock=lambda: published_at)
    )
    with database.transaction() as connection:
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        PublishJobRepository(connection).add(job)
    return item


def test_pagination_across_sparse_pages_and_missing_fields_never_zero_filled(
    database: Database,
) -> None:
    now = NOW
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)

    short_items = [
        _publish_item(
            database, channel, profile, _vid(n), content_type=ContentType.SHORTS
        )
        for n in range(1, 6)
    ]
    long_item = _publish_item(
        database, channel, profile, _vid(6), content_type=ContentType.LONGFORM
    )

    provider = MockAnalyticsProvider(clock=lambda: now)
    provider.set_channel_metrics(channel.youtube.channel_id, {"views": Decimal("5000")})
    # Seed sparse video metrics: videos 2 and 4 have no data at all; video 1 has Shorts metrics;
    # video 6 has LongForm metrics without retention or CTR.
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(1),
        {
            "views": Decimal("800"),
            "engaged_views": Decimal("600"),
            "swiped_away": Decimal("200"),
            "estimated_minutes_watched": Decimal("10"),
            "average_view_duration": Decimal("30"),
            "likes": Decimal("64"),
        },
    )
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(3),
        {"views": Decimal("120")},
    )
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(5),
        {"views": Decimal("90"), "shares": Decimal("9")},
    )
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(6),
        {
            "views": Decimal("1500"),
            "estimated_minutes_watched": Decimal("450"),
            "average_view_duration": Decimal("180"),
            "likes": Decimal("75"),
            "subscribers_gained": Decimal("15"),
        },
    )

    results = PublishResultService(database)
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    oauth = MockYouTubeOAuthProvider(clock=lambda: now)

    video_sync = VideoMetricsSync(
        database,
        provider,
        oauth,
        results,
        audit,
        page_size=2,
        clock=lambda: now,
    )
    sync_result = video_sync.sync(
        channel.id,
        period_start=START,
        period_end=END,
        actor=USER,
        metrics=(
            "views",
            "engaged_views",
            "swiped_away",
            "estimated_minutes_watched",
            "average_view_duration",
            "likes",
            "shares",
            "subscribers_gained",
        ),
    )

    # 6 eligible videos with page_size=2 -> 3 pages. Videos 2 and 4 have no data.
    assert sync_result.pages == 3
    assert sync_result.created_count == 4
    assert sync_result.no_data_video_ids == (_vid(2), _vid(4))

    shorts_service = ShortsMetricsService(database, results)
    short_view = shorts_service.latest_for_item(
        short_items[0].id, video_duration_seconds=Decimal("20")
    )
    assert short_view is not None
    assert short_view.stay_to_watch_rate == Decimal("75.00")
    assert short_view.loop_rate == Decimal("1.50")
    assert short_view.like_rate == Decimal("8.00")
    assert short_view.share_rate is None
    assert short_view.average_view_percentage is None
    assert short_view.comments is None

    # Video 2 had no data -> no snapshot stored.
    assert shorts_service.latest_for_item(short_items[1].id) is None

    longform_service = LongFormMetricsService(database, results)
    lf_view = longform_service.latest_for_item(
        long_item.id, video_duration_seconds=Decimal("600")
    )
    assert lf_view is not None
    assert lf_view.watch_time_hours == Decimal("7.50")
    assert lf_view.average_view_duration_minutes == Decimal("3.00")
    assert lf_view.like_rate == Decimal("5.00")
    assert lf_view.subscriber_conversion_rate == Decimal("1.00")
    assert lf_view.impressions is None
    assert lf_view.impressions_click_through_rate is None
    assert lf_view.average_view_percentage is None


def test_retention_and_ctr_only_stored_when_present_and_enrich_existing_snapshots(
    database: Database,
) -> None:
    now = NOW
    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
    _publish_item(database, channel, profile, _vid(1))
    _publish_item(database, channel, profile, _vid(2))

    provider = MockAnalyticsProvider(clock=lambda: now)
    provider.set_channel_metrics(channel.youtube.channel_id, {"views": Decimal("100")})
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(1),
        {
            "views": Decimal("400"),
            "average_view_percentage": Decimal("82.5"),
            "average_view_duration": Decimal("33"),
            "impressions": Decimal("5000"),
            "impressions_click_through_rate": Decimal("8.0"),
        },
    )
    provider.set_video_metrics(
        channel.youtube.channel_id,
        _vid(2),
        {
            "views": Decimal("100"),
            "impressions": Decimal("900"),
            # No average_view_percentage and no impressions_click_through_rate!
        },
    )

    results = PublishResultService(database)
    audit = AuditLog(InMemoryAuditSink())
    oauth = MockYouTubeOAuthProvider(clock=lambda: now)

    retention_sync = RetentionSync(
        database, provider, oauth, results, audit, clock=lambda: now
    )
    ctr_sync = CTRSync(
        database, provider, oauth, results, audit, clock=lambda: now
    )

    ret_res = retention_sync.sync(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert [o.youtube_video_id for o in ret_res.outcomes] == [_vid(1)]
    assert ret_res.unavailable_video_ids == (_vid(2),)
    assert retention_sync.for_video(_vid(2)) == []

    ctr_res = ctr_sync.sync_videos(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert [o.youtube_video_id for o in ctr_res.outcomes] == [_vid(1)]
    assert ctr_res.unavailable_video_ids == (_vid(2),)
    assert ctr_sync.for_video(_vid(2)) == []

    # Video 1's newest snapshot has both retention and CTR merged cleanly.
    latest_ret = retention_sync.latest_for_video(_vid(1))
    latest_ctr = ctr_sync.latest_for_video(_vid(1))
    assert latest_ret is not None and latest_ret.average_view_percentage == Decimal("82.5")
    assert latest_ctr is not None and latest_ctr.impressions_click_through_rate == Decimal("8.0")


def test_stale_data_lagging_periods_and_cache_fallback_and_invalidation(
    database: Database,
) -> None:
    # Start 12 hours after END -> within the 2-day data lag, so data is LAGGING.
    now = END + timedelta(hours=12)
    channel = make_channel(status=ChannelStatus.ACTIVE)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)

    inner = MockAnalyticsProvider(clock=lambda: now)
    inner.set_channel_metrics(
        channel.youtube.channel_id,
        {
            "views": Decimal("1000"),
            "estimated_minutes_watched": Decimal("300"),
            "subscribers_gained": Decimal("20"),
            "subscribers_lost": Decimal("4"),
        },
    )
    cache = AnalyticsCache(inner, clock=lambda: now)
    oauth = MockYouTubeOAuthProvider(clock=lambda: now)
    audit = AuditLog(InMemoryAuditSink())
    channel_sync = ChannelMetricsSync(
        database, cache, oauth, audit, clock=lambda: now
    )
    freshness = AnalyticsFreshnessService(database, clock=lambda: now)

    first = channel_sync.sync(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert first.complete is False
    assert first.snapshot is not None
    status_1 = freshness.for_snapshot(first.snapshot.id)
    assert status_1.status is FreshnessStatus.LAGGING
    assert status_1.needs_refresh is True

    # Within 6h channel_max_age -> cache HIT without calling inner provider.
    now = now + timedelta(hours=2)
    second = channel_sync.sync(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert second.created is False
    assert inner.calls == 1
    assert cache.stats().hits == 1

    # Advance 3 days past END -> past the 2-day lag AND past the 6h TTL.
    # Inject a transient failure first -> AnalyticsCache serves STALE_FALLBACK.
    now = END + timedelta(days=3)
    inner.fail_next(AnalyticsErrorCode.TIMEOUT)
    _, fallback_info = cache.query_with_info(
        cache._entries[next(iter(cache._entries))].query
    )
    assert fallback_info.outcome is AnalyticsCacheOutcome.STALE_FALLBACK
    assert fallback_info.stale is True

    # Invalidate channel cache and re-sync with updated settled numbers.
    cache.invalidate_channel(channel.youtube.channel_id)
    inner.set_channel_metrics(
        channel.youtube.channel_id,
        {
            "views": Decimal("1250"),
            "estimated_minutes_watched": Decimal("360"),
            "subscribers_gained": Decimal("25"),
            "subscribers_lost": Decimal("5"),
        },
    )
    settled = channel_sync.sync(
        channel.id, period_start=START, period_end=END, actor=USER
    )
    assert settled.created is True
    assert settled.complete is True
    assert settled.snapshot is not None
    settled_freshness = freshness.for_snapshot(settled.snapshot.id)
    assert settled_freshness.status is FreshnessStatus.FRESH
    assert settled_freshness.is_complete is True


def test_provider_failure_retries_and_overlapping_period_guards(
    database: Database,
) -> None:
    now = NOW
    channel = make_channel(status=ChannelStatus.ACTIVE)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)

    provider = MockAnalyticsProvider(clock=lambda: now)
    provider.set_channel_metrics(
        channel.youtube.channel_id,
        {
            "views": Decimal("500"),
            "estimated_minutes_watched": Decimal("140"),
            "subscribers_gained": Decimal("10"),
            "subscribers_lost": Decimal("2"),
        },
    )
    waits: list[float] = []
    oauth = MockYouTubeOAuthProvider(clock=lambda: now)
    audit = AuditLog(InMemoryAuditSink())
    sync = ChannelMetricsSync(
        database,
        provider,
        oauth,
        audit,
        retry_policy=UploadRetryPolicy(max_attempts=3, base_delay_seconds=1),
        sleep=waits.append,
        clock=lambda: now,
    )

    # 2 transient RATE_LIMITED failures recover on the 3rd attempt.
    provider.fail_next(AnalyticsErrorCode.RATE_LIMITED, times=2)
    w1 = sync.sync(
        channel.id,
        period_start=START,
        period_end=START + timedelta(days=7),
        actor=USER,
    )
    assert w1.created is True
    assert waits == [1.0, 2.0]

    # Non-retryable QUOTA_EXCEEDED fails immediately without extra retries.
    provider.fail_next(AnalyticsErrorCode.QUOTA_EXCEEDED)
    with pytest.raises(AnalyticsProviderError) as quota_err:
        sync.sync(
            channel.id,
            period_start=START + timedelta(days=7),
            period_end=START + timedelta(days=14),
            actor=USER,
        )
    assert quota_err.value.analytics_code is AnalyticsErrorCode.QUOTA_EXCEEDED

    # Unauthorized OAuth scope is refused before touching the database.
    bad_oauth = MockYouTubeOAuthProvider(
        clock=lambda: now, default_scopes=[OAuthScope.YOUTUBE_READONLY]
    )
    bad_sync = ChannelMetricsSync(
        database, provider, bad_oauth, audit, clock=lambda: now
    )
    with pytest.raises(Exception):
        bad_sync.sync(
            channel.id,
            period_start=START + timedelta(days=7),
            period_end=START + timedelta(days=14),
            actor=USER,
        )

    # Future period_end is refused with PERIOD_NOT_ENDED.
    with pytest.raises(MetricsSyncError) as period_err:
        sync.sync(
            channel.id,
            period_start=START,
            period_end=now + timedelta(days=1),
            actor=USER,
        )
    assert period_err.value.sync_code is MetricsSyncErrorCode.PERIOD_NOT_ENDED

    # Sync an overlapping period [START + 3d, START + 10d) and verify WatchTimeService
    # and SubscriberChangeService both refuse to sum overlapping windows.
    sync.sync(
        channel.id,
        period_start=START + timedelta(days=3),
        period_end=START + timedelta(days=10),
        actor=USER,
    )
    watch_service = WatchTimeService(database)
    with pytest.raises(WatchTimeError) as wt_err:
        watch_service.aggregate_for_subject(MetricScope.CHANNEL, channel.id)
    assert wt_err.value.watch_time_code is WatchTimeErrorCode.OVERLAPPING_PERIODS

    sub_service = SubscriberChangeService(
        database, provider, oauth, audit, clock=lambda: now
    )
    with pytest.raises(SubscriberChangeError) as sub_err:
        sub_service.aggregate_for_subject(MetricScope.CHANNEL, channel.id)
    assert (
        sub_err.value.subscriber_code
        is SubscriberChangeErrorCode.OVERLAPPING_PERIODS
    )
