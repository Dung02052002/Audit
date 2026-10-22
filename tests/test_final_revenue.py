"""Tests for finalized revenue sync, freshness, and reconciliation (O-172, prompt
#172).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.channel_metrics import (
    MetricsSyncError,
    MetricsSyncErrorCode,
)
from ai_youtube_agent.content.estimated_revenue import EstimatedRevenueSync
from ai_youtube_agent.content.final_revenue import (
    FINAL_CHANNEL_SYNCED,
    FINAL_VIDEOS_SYNCED,
    FinalRevenueError,
    FinalRevenueErrorCode,
    FinalRevenueSync,
)
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage
from ai_youtube_agent.content.strategy import StrategyProfile
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
from ai_youtube_agent.providers.mock_revenue import MockRevenueProvider
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
    return f"vid{n:06d}fn"


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
        self.estimated_sync = EstimatedRevenueSync(
            database,
            self.provider,
            self.oauth,
            AuditLog(self.sink),
            results=PublishResultService(database),
            retry_policy=UploadRetryPolicy(max_attempts=3, base_delay_seconds=2),
            sleep=self.waits.append,
            clock=self.clock,
        )
        self.sync = FinalRevenueSync(
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


class TestFinalRevenueSync:
    def test_stores_final_revenue_alongside_estimated_with_source_and_freshness(
        self, database: Database
    ) -> None:
        fx = Fixture(database)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.ESTIMATED,
            amounts={"ads": Decimal("100.00")},
            currency="USD",
        )
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.FINAL,
            amounts={"ads": Decimal("96.40"), "memberships": Decimal("25.00")},
            currency="USD",
        )
        est_res = fx.estimated_sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        fin_res = fx.sync.sync_channel(
            fx.channel.id,
            period_start=START,
            period_end=END,
            actor=USER,
            revenue_types=("ads", "memberships", "super_thanks"),
        )
        assert fin_res.stage is RevenueStage.FINAL
        assert fin_res.is_final is True
        assert fin_res.source == "mock_revenue"
        assert fin_res.created_count == 2
        assert fin_res.missing_revenue_types == ("super_thanks",)
        assert fin_res.outcomes[0].freshness.source == "mock_revenue"
        assert fin_res.outcomes[0].freshness.is_stale is False
        assert fx.sink.entries[-1].action == FINAL_CHANNEL_SYNCED

        # Estimated record is untouched and separate
        assert len(fx.estimated_sync.channel_records(fx.channel.id)) == 1
        assert (
            fx.estimated_sync.channel_records(fx.channel.id)[0].id
            == est_res.records[0].id
        )
        assert len(fx.sync.channel_records(fx.channel.id)) == 2

        # Side-by-side comparison calculates variance without mutating either record
        comparison = fx.sync.compare_with_estimated(
            MetricScope.CHANNEL,
            fx.channel.id,
            revenue_type="ads",
            period_start=START,
            period_end=END,
        )
        assert comparison.estimated is not None
        assert comparison.final is not None
        assert comparison.estimated.amount == Decimal("100.00")
        assert comparison.final.amount == Decimal("96.40")
        assert comparison.variance == Decimal("-3.60")
        assert comparison.currency == "USD"

    def test_refuses_period_that_has_not_ended(self, database: Database) -> None:
        fx = Fixture(database)
        with pytest.raises(MetricsSyncError) as exc_info:
            fx.sync.sync_channel(
                fx.channel.id,
                period_start=START,
                period_end=NOW + timedelta(hours=1),
                actor=USER,
            )
        assert exc_info.value.sync_code is MetricsSyncErrorCode.PERIOD_NOT_ENDED

    def test_idempotent_and_appends_new_final_record_on_correction(
        self, database: Database
    ) -> None:
        fx = Fixture(database)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.FINAL,
            amounts={"ads": Decimal("50.00")},
        )
        first = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        second = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert first.created_count == 1
        assert second.created_count == 0
        assert second.unchanged_count == 1
        assert second.records[0].id == first.records[0].id

        # Later correction from provider appends a new FINAL record
        fx.now = NOW + timedelta(days=5)
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.FINAL,
            amounts={"ads": Decimal("51.25")},
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
        assert latest["ads"].amount == Decimal("51.25")

    def test_freshness_detects_stale_final_record_and_rejects_estimated(
        self, database: Database
    ) -> None:
        fx = Fixture(database, max_age=timedelta(days=7))
        fx.provider.set_channel_revenue(
            fx.youtube_id,
            stage=RevenueStage.FINAL,
            amounts={"ads": Decimal("80.00")},
        )
        res = fx.sync.sync_channel(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        rec = res.records[0]
        stale_view = fx.sync.freshness(rec, now=NOW + timedelta(days=10))
        assert stale_view.is_stale is True
        assert stale_view.age == timedelta(days=10)

        est_record = RevenueRecord.create(
            RevenueStage.ESTIMATED,
            MetricScope.CHANNEL,
            fx.channel.id,
            revenue_type="ads",
            source="mock_revenue",
            amount=Decimal("80.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            clock=lambda: NOW,
        )
        with pytest.raises(FinalRevenueError) as exc_info:
            fx.sync.freshness(est_record, now=NOW)
        assert exc_info.value.revenue_code is FinalRevenueErrorCode.STAGE_MISMATCH

    def test_sync_videos_final_revenue(self, database: Database) -> None:
        fx = Fixture(database, page_size=2)
        fx.publish(1, 2)
        fx.provider.set_video_revenue(
            fx.youtube_id,
            video_id(1),
            stage=RevenueStage.FINAL,
            amounts={"ads": Decimal("14.20")},
        )
        res = fx.sync.sync_videos(
            fx.channel.id, period_start=START, period_end=END, actor=USER
        )
        assert res.created_count == 1
        assert res.no_data_video_ids == (video_id(2),)
        assert fx.sync.video_records(video_id(1))[0].amount == Decimal("14.20")
        assert fx.sink.entries[-1].action == FINAL_VIDEOS_SYNCED
