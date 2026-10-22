"""Tests for the video metrics sync (N-160, prompt #160)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.channel_metrics import (
    MetricsSyncError,
    MetricsSyncErrorCode,
)
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.video_metrics import (
    DEFAULT_VIDEO_METRICS,
    VIDEOS_SYNCED,
    VideoMetricsSync,
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
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.analytics import (
    AnalyticsErrorCode,
    AnalyticsPage,
    AnalyticsProviderError,
    AnalyticsQuery,
)
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
PUBLISHED_BEFORE = datetime(2026, 10, 5, tzinfo=UTC)


def video_id(n: int) -> str:
    return f"vid{n:06d}ab"  # 11 characters


def publish_video(
    database: Database,
    channel: Channel,
    profile: StrategyProfile,
    youtube_video_id: str,
    published_at: datetime = PUBLISHED_BEFORE,
) -> None:
    """Store a published Shorts item with a succeeded publish job (no upload)."""
    item = make_content_item(channel, profile, status=ContentStatus.PUBLISHED)
    video = make_artifact(item, ArtifactKind.VIDEO)
    approval = make_approval_request(item, [video]).approve(USER)
    job = (
        PublishJob.create(
            approval,
            ContentType.SHORTS,
            f"pub:{youtube_video_id}",
            clock=lambda: published_at,
        )
        .start(clock=lambda: published_at)
        .succeed(youtube_video_id, clock=lambda: published_at)
    )
    with database.transaction() as connection:
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        PublishJobRepository(connection).add(job)


class Fixture:
    def __init__(self, database: Database, **options) -> None:
        self.now = NOW
        self.database = database
        self.channel = make_channel(status=ChannelStatus.ACTIVE)
        self.profile = make_strategy_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.profile)
        self.provider = options.pop("provider", None) or MockAnalyticsProvider(
            clock=self.clock
        )
        self.oauth = MockYouTubeOAuthProvider(clock=self.clock)
        self.sink = InMemoryAuditSink()
        self.waits: list[float] = []
        self.sync = VideoMetricsSync(
            database,
            self.provider,
            self.oauth,
            PublishResultService(database),
            AuditLog(self.sink),
            retry_policy=UploadRetryPolicy(max_attempts=3, base_delay_seconds=2),
            sleep=self.waits.append,
            clock=self.clock,
            **options,
        )

    def clock(self) -> datetime:
        return self.now

    @property
    def youtube_id(self) -> str:
        return self.channel.youtube.channel_id

    def publish(self, *numbers: int, published_at: datetime = PUBLISHED_BEFORE) -> None:
        for number in numbers:
            publish_video(
                self.database,
                self.channel,
                self.profile,
                video_id(number),
                published_at,
            )

    def seed(self, number: int, **metrics: int) -> None:
        self.provider.set_video_metrics(
            self.youtube_id,
            video_id(number),
            {name: Decimal(value) for name, value in metrics.items()},
        )

    def run(self, **overrides):
        values = dict(period_start=START, period_end=END, actor=USER)
        values.update(overrides)
        return self.sync.sync(self.channel.id, **values)

    def stored(self, number: int):
        with self.database.transaction() as connection:
            return MetricSnapshotRepository(connection).list_by_subject(
                MetricScope.VIDEO, video_id(number)
            )


@pytest.fixture
def fx(database: Database) -> Fixture:
    value = Fixture(database)
    value.provider.set_channel_metrics(value.youtube_id, {"views": Decimal("1")})
    return value


def test_every_published_video_gets_a_snapshot_with_only_its_returned_metrics(
    fx: Fixture,
) -> None:
    fx.publish(1, 2, 3)
    fx.seed(1, views=100, likes=7)
    fx.seed(2, views=50, shares=1, estimated_minutes_watched=20)

    result = fx.run()

    assert sorted(o.youtube_video_id for o in result.outcomes) == [
        video_id(1),
        video_id(2),
    ]
    assert result.no_data_video_ids == (video_id(3),)
    assert result.created_count == 2 and result.unchanged_count == 0
    first = next(o for o in result.outcomes if o.youtube_video_id == video_id(1))
    assert dict(first.snapshot.metrics) == {
        "views": Decimal("100"),
        "likes": Decimal("7"),
    }
    assert first.snapshot.scope is MetricScope.VIDEO
    assert first.snapshot.subject_id == video_id(1)
    assert first.snapshot.source == "mock_analytics"
    assert "average_view_duration" in first.missing and "likes" not in first.missing
    assert [len(fx.stored(n)) for n in (1, 2, 3)] == [1, 1, 0]
    assert result.complete is True and result.pages == 1


def test_a_video_published_after_the_period_is_skipped(fx: Fixture) -> None:
    fx.publish(1)
    fx.publish(2, published_at=datetime(2026, 10, 18, tzinfo=UTC))
    fx.seed(1, views=1)
    fx.seed(2, views=2)

    result = fx.run()

    assert result.requested_video_ids == (video_id(1),)
    assert result.skipped_video_ids == (video_id(2),)
    assert fx.stored(2) == []


def test_only_the_named_published_videos_are_read(fx: Fixture) -> None:
    fx.publish(1, 2)
    fx.seed(1, views=1)
    fx.seed(2, views=2)

    result = fx.run(video_ids=[video_id(2)])

    assert [o.youtube_video_id for o in result.outcomes] == [video_id(2)]
    assert fx.stored(1) == []


def test_a_video_this_channel_did_not_publish_is_refused_before_any_request(
    fx: Fixture,
) -> None:
    fx.publish(1)

    with pytest.raises(MetricsSyncError) as caught:
        fx.run(video_ids=[video_id(1), video_id(9)])

    assert caught.value.sync_code is MetricsSyncErrorCode.VIDEO_NOT_PUBLISHED
    assert fx.provider.calls == 0


def test_a_channel_without_published_videos_makes_no_request(fx: Fixture) -> None:
    result = fx.run()

    assert result.outcomes == () and result.requested_video_ids == ()
    assert result.pages == 0 and result.complete is False
    assert fx.provider.calls == 0
    [event] = fx.sink.events()
    assert event.action == VIDEOS_SYNCED
    assert event.metadata["requested_count"] == 0


def test_many_videos_are_read_page_by_page(database: Database) -> None:
    fx = Fixture(database, page_size=2)
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("1")})
    fx.publish(1, 2, 3, 4, 5)
    for number in (1, 2, 3, 4, 5):
        fx.seed(number, views=number)

    result = fx.run()

    assert result.pages == 3
    assert result.created_count == 5
    assert fx.provider.calls == 3


def test_a_repeated_sync_stores_nothing_new_and_changed_numbers_add_a_snapshot(
    fx: Fixture,
) -> None:
    fx.publish(1)
    fx.seed(1, views=10)

    first = fx.run()
    fx.now += timedelta(minutes=5)
    again = fx.run()
    fx.now += timedelta(minutes=5)
    fx.seed(1, views=11)
    changed = fx.run()

    assert first.created_count == 1
    assert again.created_count == 0 and again.unchanged_count == 1
    assert changed.created_count == 1
    assert [dict(s.metrics)["views"] for s in fx.stored(1)] == [
        Decimal("10"),
        Decimal("11"),
    ]


def test_retryable_errors_are_retried_and_others_stop_the_sync(fx: Fixture) -> None:
    fx.publish(1)
    fx.seed(1, views=10)
    fx.provider.fail_next(AnalyticsErrorCode.TIMEOUT)

    assert fx.run().created_count == 1
    assert fx.waits == [2.0]

    fx.provider.fail_next(AnalyticsErrorCode.UNAUTHORIZED)
    fx.now += timedelta(minutes=5)
    fx.seed(1, views=12)
    with pytest.raises(AnalyticsProviderError):
        fx.run()

    assert len(fx.stored(1)) == 1


def test_a_source_that_never_ends_its_pages_is_stopped(database: Database) -> None:
    class Looping:
        name = "loop_provider"

        def query(self, query: AnalyticsQuery, *, token=None) -> AnalyticsPage:
            return AnalyticsPage(
                (), "o1", "loop_provider", query.period_start, query.period_end, NOW
            )

        def check(self) -> None:
            return None

    fx = Fixture(database, provider=Looping())
    fx.publish(1)

    with pytest.raises(MetricsSyncError) as caught:
        fx.run()

    assert caught.value.sync_code is MetricsSyncErrorCode.TOO_MANY_PAGES
    assert fx.stored(1) == []


def test_the_period_must_have_ended_and_the_channel_must_be_ready(
    fx: Fixture, database: Database
) -> None:
    fx.publish(1)
    with pytest.raises(MetricsSyncError) as period:
        fx.run(period_end=NOW + timedelta(days=1))
    assert period.value.sync_code is MetricsSyncErrorCode.PERIOD_NOT_ENDED

    pending = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(pending)
    with pytest.raises(MetricsSyncError) as channel:
        fx.sync.sync(pending.id, period_start=START, period_end=END, actor=USER)
    assert channel.value.sync_code is MetricsSyncErrorCode.CHANNEL_NOT_SYNCABLE
    with pytest.raises(MetricsSyncError):
        fx.sync.sync("unknown", period_start=START, period_end=END, actor=USER)
    assert fx.provider.calls == 0


def test_recent_data_is_reported_as_incomplete(fx: Fixture) -> None:
    fx.publish(1)
    fx.seed(1, views=1)

    result = fx.run(period_start=NOW - timedelta(days=7), period_end=NOW)

    assert result.created_count == 1
    assert result.complete is False
    assert result.data_through == NOW - timedelta(days=2)


def test_the_audit_event_has_counts_and_no_metric_values(fx: Fixture) -> None:
    fx.publish(1, 2)
    fx.seed(1, views=987654)

    fx.run()

    [event] = fx.sink.events()
    assert event.action == VIDEOS_SYNCED
    assert event.entity.id == fx.channel.id
    assert event.metadata["requested_count"] == 2
    assert event.metadata["created_count"] == 1
    assert event.metadata["no_data_count"] == 1
    assert "987654" not in {str(value) for value in event.metadata.values()}


def test_the_default_video_metrics_do_not_ask_for_retention_or_click_through() -> None:
    assert "average_view_percentage" not in DEFAULT_VIDEO_METRICS
    assert "impressions_click_through_rate" not in DEFAULT_VIDEO_METRICS
