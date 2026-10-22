"""Tests for contribution margin calculator (O-179, prompt #179)."""

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.contribution_margin import (
    METRIC_LABEL,
    NOT_NET_PROFIT_DISCLAIMER,
    ContributionMarginCalculator,
    ContributionMarginError,
    ContributionMarginErrorCode,
)
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.revenue_sources import RevenueSourceService
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
IN_WINDOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class TestContributionMarginCalculator:
    def test_channel_and_item_contribution_margin_never_labelled_net_profit(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = make_strategy_profile(channel)
        item = make_content_item(channel, profile, status=ContentStatus.PUBLISHED)
        video = make_artifact(item, ArtifactKind.VIDEO)
        approval = make_approval_request(item, [video]).approve(USER)
        vid = "vid000001cm"
        job = (
            PublishJob.create(
                approval, ContentType.SHORTS, f"pub:{vid}", clock=lambda: IN_WINDOW
            )
            .start(clock=lambda: IN_WINDOW)
            .succeed(vid, clock=lambda: IN_WINDOW)
        )
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            ContentItemRepository(connection).add(item)
            ArtifactRepository(connection).add(video)
            ApprovalRequestRepository(connection).add(approval)
            PublishJobRepository(connection).add(job)

        audit = AuditLog(InMemoryAuditSink())
        rev_service = RevenueSourceService(database, audit, clock=lambda: NOW)
        ledger = CostLedger(database, audit, clock=lambda: IN_WINDOW)
        calc = ContributionMarginCalculator(database, rev_service, ledger)

        # Record channel FINAL and ESTIMATED revenue
        rev_service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="ads",
            amount=Decimal("200.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        rev_service.record(
            channel.id,
            stage=RevenueStage.ESTIMATED,
            revenue_type="ads",
            amount=Decimal("250.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
        )

        # Record video FINAL revenue
        rev_service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="ads",
            amount=Decimal("50.00"),
            currency="USD",
            period_start=START,
            period_end=END,
            actor=USER,
            youtube_video_id=vid,
        )

        # Record tracked contribution costs
        ledger.record(
            channel.id,
            CostCategory.LLM,
            provider="mock_llm",
            amount=Decimal("2.50"),
            currency="USD",
            content_item_id=item.id,
            ref="llm:1",
        )
        ledger.record(
            channel.id,
            CostCategory.TTS,
            provider="mock_tts",
            amount=Decimal("7.50"),
            currency="USD",
            content_item_id=item.id,
            ref="tts:1",
        )
        ledger.record(
            channel.id,
            CostCategory.STORAGE,
            provider="local_storage",
            amount=Decimal("10.00"),
            currency="USD",
            ref="storage:1",
        )

        # 1. Channel FINAL contribution margin: 200.00 - 20.00 = 180.00 (90.00%)
        ch_final = calc.for_channel(
            channel.id,
            stage=RevenueStage.FINAL,
            period_start=START,
            period_end=END,
        )
        assert ch_final.metric_label == METRIC_LABEL
        assert ch_final.is_net_profit is False
        assert ch_final.disclaimer == NOT_NET_PROFIT_DISCLAIMER
        assert "net_profit" not in ch_final.as_dict()
        assert ch_final.is_final_revenue is True
        assert ch_final.revenue_total == Decimal("200.00")
        assert ch_final.tracked_contribution_costs_total == Decimal("20.000000")
        assert ch_final.contribution_margin == Decimal("180.000000")
        assert ch_final.contribution_margin_ratio == Decimal("90.00")

        # 2. Channel ESTIMATED contribution margin: 250.00 - 20.00 = 230.00 (92.00%)
        ch_est = calc.for_channel(
            channel.id,
            stage=RevenueStage.ESTIMATED,
            period_start=START,
            period_end=END,
        )
        assert ch_est.is_final_revenue is False
        assert ch_est.revenue_total == Decimal("250.00")
        assert ch_est.contribution_margin == Decimal("230.000000")
        assert ch_est.contribution_margin_ratio == Decimal("92.00")

        # 3. Content item FINAL contribution margin: 50.00 - (2.50 + 7.50) = 40.00
        # (80.00%)
        item_final = calc.for_content_item(
            item.id,
            stage=RevenueStage.FINAL,
            period_start=START,
            period_end=END,
        )
        assert item_final.scope is MetricScope.VIDEO
        assert item_final.youtube_video_id == vid
        assert item_final.revenue_total == Decimal("50.00")
        assert item_final.tracked_contribution_costs_total == Decimal("10.000000")
        assert item_final.contribution_margin == Decimal("40.000000")
        assert item_final.contribution_margin_ratio == Decimal("80.00")
        assert item_final.is_net_profit is False

    def test_zero_revenue_negative_margin_none_ratio_currency_mismatch_fails(
        self, database: Database
    ) -> None:
        channel = make_channel(status=ChannelStatus.ACTIVE)
        profile = dataclasses.replace(
            make_strategy_profile(channel), budget=None, monetization=None
        )
        item = make_content_item(channel, profile)
        with database.transaction() as connection:
            ChannelRepository(connection).add(channel)
            StrategyProfileRepository(connection).add(profile)
            ContentItemRepository(connection).add(item)

        audit = AuditLog(InMemoryAuditSink())
        rev_service = RevenueSourceService(database, audit, clock=lambda: NOW)
        ledger = CostLedger(database, audit, clock=lambda: IN_WINDOW)
        calc = ContributionMarginCalculator(database, rev_service, ledger)

        ledger.record(
            channel.id,
            CostCategory.RENDER,
            provider="ffmpeg",
            amount=Decimal("5.00"),
            currency="USD",
            content_item_id=item.id,
            ref="render:1",
        )
        report = calc.for_content_item(
            item.id,
            stage=RevenueStage.ESTIMATED,
            period_start=START,
            period_end=END,
        )
        assert report.revenue_total == Decimal("0")
        assert report.contribution_margin == Decimal("-5.000000")
        assert report.contribution_margin_ratio is None

        # Record revenue in EUR while costs are in USD -> CURRENCY_MISMATCH
        rev_service.record(
            channel.id,
            stage=RevenueStage.FINAL,
            revenue_type="ads",
            amount=Decimal("50.00"),
            currency="EUR",
            period_start=START,
            period_end=END,
            actor=USER,
        )
        with pytest.raises(ContributionMarginError) as exc_curr:
            calc.for_channel(
                channel.id,
                stage=RevenueStage.FINAL,
                period_start=START,
                period_end=END,
            )
        assert (
            exc_curr.value.margin_code is ContributionMarginErrorCode.CURRENCY_MISMATCH
        )
