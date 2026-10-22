"""Tests for revenue sources registry and recording (O-173, prompt #173)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.revenue_sources import (
    BUILTIN_REVENUE_STREAMS,
    REVENUE_SOURCE_RECORDED,
    RevenueSourceError,
    RevenueSourceErrorCode,
    RevenueSourceService,
    RevenueStreamCategory,
    RevenueStreamRegistry,
    RevenueStreamSpec,
)
from ai_youtube_agent.content.strategy import (
    Monetization,
    RevenueGoal,
    RevenueSource,
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
PUBLISHED_AT = datetime(2026, 10, 5, tzinfo=UTC)


class TestRevenueStreamRegistry:
    def test_contains_all_builtin_revenue_sources_and_supports_custom_streams(
        self,
    ) -> None:
        registry = RevenueStreamRegistry()
        assert len(BUILTIN_REVENUE_STREAMS) == len(RevenueSource)
        for source in RevenueSource:
            spec = registry.get(source.value)
            assert spec.builtin is True

        custom = RevenueStreamSpec(
            revenue_type="course_sales",
            category=RevenueStreamCategory.CUSTOM,
            default_source="gumroad_ledger",
            supports_estimated=False,
            supports_final=True,
            supports_video_scope=False,
        )
        registry.register(custom)
        assert registry.get("course_sales") == custom

        with pytest.raises(RevenueSourceError) as exc_info:
            registry.register(custom)
        assert (
            exc_info.value.source_code is RevenueSourceErrorCode.DUPLICATE_REVENUE_TYPE
        )

        with pytest.raises(RevenueSourceError) as exc_unknown:
            registry.get("unregistered_stream")
        assert (
            exc_unknown.value.source_code is RevenueSourceErrorCode.UNKNOWN_REVENUE_TYPE
        )


class TestRevenueSourceService:
    def test_records_ads_memberships_affiliate_sponsorship_and_custom_streams(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        monetization = Monetization(
            goals=(
                RevenueGoal(source=RevenueSource.ADS, monthly_target=Decimal("200.00")),
                RevenueGoal(
                    source=RevenueSource.SPONSORSHIPS,
                    monthly_target=Decimal("500.00"),
                ),
                RevenueGoal(source=RevenueSource.AFFILIATE),
            ),
            currency="USD",
        )
        profile = make_strategy_profile(channel, monetization=monetization)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)

        sink = InMemoryAuditSink()
        registry = RevenueStreamRegistry(
            extra_streams=(
                RevenueStreamSpec(
                    revenue_type="patreon",
                    category=RevenueStreamCategory.CUSTOM,
                    default_source="patreon_export",
                ),
            )
        )
        service = RevenueSourceService(
            database,
            AuditLog(sink),
            registry=registry,
            clock=lambda: NOW,
        )

        r_ads = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="ads",
            amount=Decimal("150.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        r_mem = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="memberships",
            amount=Decimal("45.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        r_spon = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="sponsorships",
            amount=Decimal("600.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        r_aff = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="affiliate",
            amount=Decimal("85.50"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        r_pat = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="patreon",
            amount=Decimal("30.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        assert r_ads.created is True
        assert r_mem.created is True
        assert r_spon.created is True
        assert r_aff.created is True
        assert r_pat.created is True
        assert sink.entries[-1].action == REVENUE_SOURCE_RECORDED

        # Idempotent replay returns created=False
        replay = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="sponsorships",
            amount=Decimal("600.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        assert replay.created is False
        assert replay.record.id == r_spon.record.id

        # Also record an ESTIMATED figure and verify breakdown never mixes stages
        service.record(
            channel.id,
            stage=RevenueStage.ESTIMATED,
            revenue_type="ads",
            amount=Decimal("999.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )

        breakdown = service.breakdown(
            MetricScope.CHANNEL,
            channel.id,
            stage=RevenueStage.FINAL,
            period_start=START,
            period_end=END,
        )
        assert breakdown.is_final is True
        assert breakdown.currency == "USD"
        assert breakdown.total_amount == Decimal("910.50")
        assert breakdown.by_category == {
            "youtube_ads": Decimal("150.00"),
            "youtube_fan_funding": Decimal("45.00"),
            "partner_external": Decimal("685.50"),
            "custom": Decimal("30.00"),
        }
        assert len(breakdown.goal_progress) == 3
        assert all(gp.is_guaranteed is False for gp in breakdown.goal_progress)
        progress_by_type = {gp.revenue_type: gp for gp in breakdown.goal_progress}
        assert progress_by_type["ads"].target_delta == Decimal("-50.00")
        assert progress_by_type["sponsorships"].target_delta == Decimal("100.00")
        assert progress_by_type["affiliate"].target_delta is None

    def test_video_scope_and_stage_constraints(self, database: Database) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        item = make_content_item(channel, profile, status=ContentStatus.PUBLISHED)
        video = make_artifact(item, ArtifactKind.VIDEO)
        approval = make_approval_request(item, [video]).approve(USER)
        vid = "vid000001rs"
        job = (
            PublishJob.create(
                approval, ContentType.SHORTS, f"pub:{vid}", clock=lambda: PUBLISHED_AT
            )
            .start(clock=lambda: PUBLISHED_AT)
            .succeed(vid, clock=lambda: PUBLISHED_AT)
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            ContentItemRepository(connection).add(item)
            ArtifactRepository(connection).add(video)
            ApprovalRequestRepository(connection).add(approval)
            PublishJobRepository(connection).add(job)

        registry = RevenueStreamRegistry(
            extra_streams=(
                RevenueStreamSpec(
                    revenue_type="channel_grant",
                    category=RevenueStreamCategory.CUSTOM,
                    default_source="grant_ledger",
                    supports_estimated=False,
                    supports_final=True,
                    supports_video_scope=False,
                ),
            )
        )
        service = RevenueSourceService(
            database,
            AuditLog(InMemoryAuditSink()),
            registry=registry,
            clock=lambda: NOW,
        )

        # Video-scoped sponsorship on a published video succeeds
        vid_outcome = service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="sponsorships",
            amount=Decimal("250.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
            youtube_video_id=vid,
        )
        assert vid_outcome.record.scope is MetricScope.VIDEO
        assert vid_outcome.record.subject_id == vid

        # Stream that disallows estimated or video scope raises RevenueSourceError
        with pytest.raises(RevenueSourceError) as exc_stage:
            service.record(
                channel.id,
                stage=RevenueStage.ESTIMATED,
                revenue_type="channel_grant",
                amount=Decimal("100.00"),
                currency="USD",
                period_start=START,
                period_end=END,
                actor=USER,
            )
        assert exc_stage.value.source_code is RevenueSourceErrorCode.STAGE_NOT_SUPPORTED

        with pytest.raises(RevenueSourceError) as exc_scope:
            service.record(
                channel.id,
                stage=RevenueStage.FINAL,
                revenue_type="channel_grant",
                amount=Decimal("100.00"),
                currency="USD",
                period_start=START,
                period_end=END,
                actor=USER,
                youtube_video_id=vid,
            )
        assert exc_scope.value.source_code is RevenueSourceErrorCode.SCOPE_NOT_SUPPORTED
