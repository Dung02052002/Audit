"""Tests for Revenue Screen view and service (Prompt Pack v8, prompt Q-197)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.contribution_margin import (
    METRIC_LABEL,
    NOT_NET_PROFIT_DISCLAIMER,
)
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.dashboard_revenue import RevenueScreenService
from ai_youtube_agent.content.dashboard_shell import DashboardScreenId, ViewLoadState
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.economics import (
    CostRecordRepository,
    RevenueRecordRepository,
)
from factories import make_channel

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_revenue_screen_empty_when_no_channels(database: Database) -> None:
    service = RevenueScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.channel_id is None
    assert view.metric_label == METRIC_LABEL
    assert view.is_net_profit is False
    assert view.disclaimer == NOT_NET_PROFIT_DISCLAIMER


def test_revenue_screen_separates_estimated_and_final_revenue_and_computes_contribution_margin(
    database: Database,
) -> None:
    channel = make_channel(title="Finance Studio", status=ChannelStatus.ACTIVE)

    p_start = FIXED_NOW - timedelta(days=7)
    p_end = FIXED_NOW - timedelta(days=1)

    est_rec = RevenueRecord.create(
        RevenueStage.ESTIMATED,
        MetricScope.CHANNEL,
        channel.id,
        revenue_type="ads",
        source="youtube_analytics",
        amount=Decimal("250.00"),
        currency="USD",
        period_start=p_start,
        period_end=p_end,
        clock=lambda: FIXED_NOW,
    )
    fin_rec = RevenueRecord.create(
        RevenueStage.FINAL,
        MetricScope.CHANNEL,
        channel.id,
        revenue_type="ads",
        source="youtube_analytics",
        amount=Decimal("240.00"),
        currency="USD",
        period_start=p_start,
        period_end=p_end,
        clock=lambda: FIXED_NOW,
    )
    cost_llm = CostRecord.create(
        channel.id,
        CostCategory.LLM,
        provider="openai",
        amount=Decimal("12.50"),
        currency="USD",
        clock=lambda: FIXED_NOW - timedelta(days=3),
    )
    cost_tts = CostRecord.create(
        channel.id,
        CostCategory.TTS,
        provider="elevenlabs",
        amount=Decimal("7.50"),
        currency="USD",
        clock=lambda: FIXED_NOW - timedelta(days=2),
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        RevenueRecordRepository(conn).add(est_rec)
        RevenueRecordRepository(conn).add(fin_rec)
        CostRecordRepository(conn).add(cost_llm)
        CostRecordRepository(conn).add(cost_tts)

    service = RevenueScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id, period_start=p_start, period_end=FIXED_NOW)

    assert view.shell.active_screen is DashboardScreenId.REVENUE
    assert view.load_state is ViewLoadState.READY
    assert view.metric_label == "contribution_margin"
    assert view.is_net_profit is False
    assert view.disclaimer == NOT_NET_PROFIT_DISCLAIMER

    assert view.estimated_revenue is not None
    assert view.estimated_revenue.total_amount == Decimal("250.00")
    assert view.final_revenue is not None
    assert view.final_revenue.total_amount == Decimal("240.00")

    assert view.costs is not None
    assert view.costs.total_amount == Decimal("20.000000")

    assert view.estimated_margin is not None
    assert view.estimated_margin.revenue_stage is RevenueStage.ESTIMATED
    assert view.estimated_margin.contribution_margin == Decimal("230.000000")
    assert view.estimated_margin.is_net_profit is False

    assert view.final_margin is not None
    assert view.final_margin.revenue_stage is RevenueStage.FINAL
    assert view.final_margin.contribution_margin == Decimal("220.000000")
    assert view.final_margin.is_net_profit is False

    html_out = service.render_html(view)
    assert 'data-testid="revenue-screen"' in html_out
    assert 'data-testid="revenue-estimated-card"' in html_out
    assert 'data-testid="revenue-final-card"' in html_out
    assert 'data-testid="revenue-margin-disclaimer"' in html_out
    assert "contribution_margin" in html_out
    assert "is_net_profit=false" in html_out

