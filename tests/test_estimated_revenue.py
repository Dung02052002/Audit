"""Tests for estimated revenue sync and queries (O-171, prompt #171)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.channel_metrics import (
    MetricsSyncError,
    MetricsSyncErrorCode,
)
from ai_youtube_agent.content.estimated_revenue import (
    ESTIMATED_CHANNEL_SYNCED,
    ESTIMATED_VIDEOS_SYNCED,
    EstimatedRevenueError,
    EstimatedRevenueErrorCode,
    EstimatedRevenueSync,
)
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage
from ai_youtube_agent.content.strategy import RevenueSource, StrategyProfile
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
from ai_youtube_agent.core.db.repositories.economics import RevenueRecordRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.publish import PublishJob
from ai_youtube_agent.pipeline.publish_result import PublishResultService
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.mock_revenue import MockRevenueProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.revenue import RevenueErrorCode, RevenueProviderError
from ai_youtube_agent.providers.youtube_oauth import OAuthScope, YouTubeOAuthError
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
    return f"vid{n:06d}ab"


def publish_video(
    database: Database,
    channel: Channel,
    profile: StrategyProfile,
    youtube_video_id: str,
    published_at: datetime = PUBLISHED_BEFORE,
) -> None:
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
        self.provider = options.pop("provider", None) or MockRevenueProvider(
            clock=self.clock
        )
        self.oauth = options.pop("oauth", None) or MockYouTubeOAuthProvider(
            clock=self.clock
        )
        self.sink = InMemoryAuditSink()
        self.waits: list[float] = []
        self.sync = EstimatedRevenueSync(
            database,
            self.provider,
            self.oauth,
            AuditLog(self.sink),
            results=PublishResultService(database),
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


class TestChannelEstimatedRevenueSync:
    def test_stores_estimated_records_and_leaves_final_untouched(
        self, database: Database
    ) -> None:
        fx = Fixture(database)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.ESTIMATED,
            amounts={"ads": Decimal("42.50"), "shorts_ads": Decimal("11.25")},
            currency="USD",
        )
        # Seed a pre-existing FINAL record to verify it is never overwritten or mixed
        final_record = RevenueRecord.create(
            RevenueStage.FINAL,
            MetricScope.CHANNEL,
            fx.channel.id,
            revenue_type="ads",
            source="mock_revenue",
            amount=Decimal("40.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            clock=lambda: NOW,
        )
        with database.transaction() as connection:
            RevenueRecordRepository(connection).add(final_record)

        result = fx.sync.sync_channel(
            fx.channel.id,
            period_start=START,
            period_end=END,
            actor=USER,
            revenue_types=("ads", "shorts_ads", "memberships"),
        )
        assert result.stage is RevenueStage.ESTIMATED
        assert result.is_final is False
        assert result.created_count == 2
        assert result.unchanged_count == 0
        assert result.missing_revenue_types == ("memberships",)
        assert result.complete is True
        assert [r.amount for r in result.records] == [
            Decimal("42.50"),
            Decimal("11.25"),
        ]
        assert all(r.stage is RevenueStage.ESTIMATED for r in result.records)

        # Stored estimated query never returns the FINAL record
        stored_est = fx.sync.channel_records(fx.channel.id)
        assert len(stored_est) == 2
        assert all(r.stage is RevenueStage.ESTIMATED for r in stored_est)

        # And the FINAL record is still intact in the repository
        with database.transaction() as connection:
            all_records = RevenueRecordRepository(connection).list_by_subject(
                MetricScope.CHANNEL, fx.channel.id
            )
        assert len(all_records) == 3
        assert any(
            r.id == final_record.id
            and r.stage is RevenueStage.FINAL
            and r.amount == Decimal("40.00")
            for r in all_records
        )
        assert fx.sink.entries[-1].action == ESTIMATED_CHANNEL_SYNCED

    def test_idempotent_on_unchanged_estimate_and_appends_on_revision(
        self, database: Database
    ) -> None:
        fx = Fixture(database)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.ESTIMATED,
            amounts={"ads": Decimal("18.00")},
        )
        first = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert first.created_count == 1

        second = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert second.created_count == 0
        assert second.unchanged_count == 1
        assert second.records[0].id == first.records[0].id

        # Revise the estimate
        fx.now = NOW + timedelta(hours=3)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.ESTIMATED,
            amounts={"ads": Decimal("19.75")},
        )
        third = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert third.created_count == 1
        assert len(fx.sync.channel_records(fx.channel.id)) == 2
        latest = fx.sync.latest_by_type(
            MetricScope.CHANNEL,
            fx.channel.id,
            period_start=START,
            period_end=END,
        )
        assert latest["ads"].amount == Decimal("19.75")

    def test_empty_provider_response_creates_no_records(
        self, database: Database
    ) -> None:
        fx = Fixture(database)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.ESTIMATED,
            amounts={},
        )
        result = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert result.records == ()
        assert result.created_count == 0
        assert result.missing_revenue_types == (RevenueSource.ADS.value,)
        assert fx.sync.channel_records(fx.channel.id) == []

    def test_retries_transient_provider_errors_and_checks_monetary_scope(
        self, database: Database
    ) -> None:
        oauth_no_monetary = MockYouTubeOAuthProvider(
            default_scopes=frozenset({OAuthScope.YT_ANALYTICS_READONLY}),
            clock=lambda: NOW,
        )
        fx_denied = Fixture(database, oauth=oauth_no_monetary)
        with pytest.raises(YouTubeOAuthError):
            fx_denied.sync.sync_channel(
                fx_denied.channel.id,
                period_start=START,
                period_end=END,
                actor=USER,
            )

        fx = Fixture(database)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.ESTIMATED,
            amounts={"ads": Decimal("10.00")},
        )
        fx.provider.fail_next(RevenueErrorCode.UNAVAILABLE, times=2)
        result = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert result.created_count == 1
        assert len(fx.waits) == 2

    def test_rejects_future_period_start(self, database: Database) -> None:
        fx = Fixture(database)
        with pytest.raises(EstimatedRevenueError) as exc_info:
            fx.sync.sync_channel(
                fx.channel.id,
                period_start=NOW + timedelta(days=1),
                period_end=NOW + timedelta(days=2),
                actor=USER,
            )
        assert (
            exc_info.value.revenue_code
            is EstimatedRevenueErrorCode.PERIOD_NOT_STARTED
        )


class TestVideoEstimatedRevenueSync:
    def test_syncs_published_videos_and_paginates(self, database: Database) -> None:
        fx = Fixture(database, page_size=1)
        fx.publish(1, 2, 3)
        fx.provider.set_video_revenue(
            fx.youtube_id,
            video_id(1),
            stage=RevenueStage.ESTIMATED,
            amounts={"ads": Decimal("5.10")},
        )
        fx.provider.set_video_revenue(
            fx.youtube_id,
            video_id(2),
            stage=RevenueStage.ESTIMATED,
            amounts={"ads": Decimal("7.40")},
        )
        result = fx.sync.sync_videos(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert result.created_count == 2
        assert result.no_data_video_ids == (video_id(3),)
        assert result.pages == 3
        assert fx.sync.video_records(video_id(1))[0].amount == Decimal("5.10")
        assert fx.sync.video_records(video_id(2))[0].amount == Decimal("7.40")
        assert fx.sync.video_records(video_id(3)) == []
        assert fx.sink.entries[-1].action == ESTIMATED_VIDEOS_SYNCED

    def test_rejects_unowned_video_id(self, database: Database) -> None:
        fx = Fixture(database)
        fx.publish(1)
        with pytest.raises(MetricsSyncError) as exc_info:
            fx.sync.sync_videos(
                fx.channel.id,
                period_start=START,
                period_end=END,
                actor=USER,
                video_ids=(video_id(99),),
            )
        assert exc_info.value.sync_code is MetricsSyncErrorCode.VIDEO_NOT_PUBLISHED
