"""T-227 Dashboard E2E: Command Center screens reflect pipeline, analytics,
economics (Contribution Margin, not net profit), and approval state accurately.
"""

import json
from decimal import Decimal

from ai_youtube_agent.bootstrap import build_container, prepare_database
from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.approval_service import ApprovalService
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.cost_ledger import CostLedger
from ai_youtube_agent.content.dashboard_ai_center import AICenterScreenService
from ai_youtube_agent.content.dashboard_alerts import AlertsScreenService
from ai_youtube_agent.content.dashboard_analytics import AnalyticsScreenService
from ai_youtube_agent.content.dashboard_approvals import ApprovalCenterService
from ai_youtube_agent.content.dashboard_channel import ChannelOverviewService
from ai_youtube_agent.content.dashboard_content import ContentScreenService
from ai_youtube_agent.content.dashboard_home import HomeScreenService
from ai_youtube_agent.content.dashboard_revenue import RevenueScreenService
from ai_youtube_agent.content.dashboard_settings import SettingsScreenService
from ai_youtube_agent.content.qc import QCCheck, QCResult, QCStatus
from ai_youtube_agent.content.revenue import RevenueSource, RevenueStage
from ai_youtube_agent.content.revenue_sources import RevenueSourceService
from ai_youtube_agent.content.strategy import Cadence
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import ArtifactStorage
from ai_youtube_agent.core.config import Environment, Settings
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
from ai_youtube_agent.core.db.repositories.review import QCResultRepository
from ai_youtube_agent.core.metrics import MetricScope
from ai_youtube_agent.providers.mock_video_render import stub_mp4
from factories import (
    SYSTEM,
    T0,
    USER,
    at,
    make_channel,
    make_content_item,
    make_strategy_profile,
)
from publish_helpers import default_metadata, store_artifact


def test_dashboard_e2e_reflects_live_pipeline_economics_and_approvals(
    tmp_path,
) -> None:
    """Verifies that Home, Content, Approval Center, Revenue, Analytics, AI Center,
    Channel, Settings, and Alerts screens reflect live SQLite state and user actions."""
    settings = Settings(
        environment=Environment.TEST,
        database_path=tmp_path / "dashboard_e2e.sqlite3",
        artifact_dir=tmp_path / "artifacts",
    )
    container = build_container(settings)
    prepare_database(container)

    db = container.resolve(Database)
    storage = container.resolve(ArtifactStorage)
    approval_service = container.resolve(ApprovalService)
    cost_ledger = container.resolve(CostLedger)
    revenue_sources = container.resolve(RevenueSourceService)

    channel = make_channel(status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(
        channel, cadence=Cadence(shorts_per_day=3, longform_per_day=1)
    )
    shorts_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        status=ContentStatus.TESTING,
    )
    longform_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        status=ContentStatus.GENERATING,
    )

    with db.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(shorts_item)
        ContentItemRepository(conn).add(longform_item)

        art_repo = ArtifactRepository(conn)
        video_art = store_artifact(
            storage,
            art_repo,
            shorts_item.id,
            ArtifactKind.VIDEO,
            "video/mp4",
            stub_mp4(25_000, b"dashboard-shorts"),
        )
        meta_art = store_artifact(
            storage,
            art_repo,
            shorts_item.id,
            ArtifactKind.METADATA,
            "application/json",
            json.dumps(
                default_metadata(ContentType.SHORTS, shorts_item, channel)
            ).encode("utf-8"),
        )
        qc = QCResult.create(
            shorts_item.id,
            [video_art, meta_art],
            [QCCheck("video.fps", QCStatus.PASS)],
            clock=at(1),
        )
        QCResultRepository(conn).add(qc)

    # Request approval on shorts_item so it moves to AWAITING_APPROVAL
    req = approval_service.request_approval(shorts_item.id, requested_by=SYSTEM)
    assert req.approval.status is ApprovalStatus.PENDING

    # Record revenue and cost to verify RevenueScreenService (R-09 / C14: Contribution Margin, not net profit)
    from datetime import UTC, datetime, timedelta

    now_utc = datetime.now(UTC)
    window_start = now_utc - timedelta(hours=1)
    window_end = now_utc + timedelta(hours=1)

    revenue_sources.record(
        channel.id,
        stage=RevenueStage.ESTIMATED,
        revenue_type=RevenueSource.ADS.value,
        amount=Decimal("25.00"),
        currency="USD",
        period_start=window_start,
        period_end=window_end,
        actor=SYSTEM,
    )
    cost_ledger.record(
        channel.id,
        CostCategory.LLM,
        provider="mock_llm",
        amount=Decimal("2.50"),
        currency="USD",
        content_item_id=shorts_item.id,
    )

    # 1. Home Screen reflects active channel, 2 in-progress items, and 1 pending approval
    home_svc = container.resolve(HomeScreenService)
    home_view = home_svc.load(channel.id)
    assert home_view.channel_id == channel.id
    assert home_view.production is not None
    assert home_view.production.shorts_in_progress == 1
    assert home_view.production.longform_in_progress == 1
    assert home_view.pending_approvals_count == 1
    assert "YouTube Command Center" in home_svc.render_html(home_view)

    # 2. Content Screen separates Shorts and LongForm columns
    content_svc = container.resolve(ContentScreenService)
    content_view = content_svc.load(channel.id)
    assert content_view.total_items == 2
    assert content_view.shorts_column.total_count == 1
    assert content_view.longform_column.total_count == 1

    # 3. Approval Center shows pending request and allows human USER approval
    approval_center = container.resolve(ApprovalCenterService)
    ac_view_before = approval_center.load(channel.id)
    assert ac_view_before.pending_count == 1
    decision = approval_center.approve(
        req.approval.id,
        actor=USER,
        expected_video_artifact_id=video_art.id,
        expected_video_version=1,
    )
    assert decision.approval.status is ApprovalStatus.APPROVED
    ac_view_after = approval_center.load(channel.id)
    assert ac_view_after.pending_count == 0
    assert ac_view_after.approved_count == 1

    # 4. Revenue Screen reports Contribution Margin with is_net_profit=False
    rev_svc = container.resolve(RevenueScreenService)
    rev_view = rev_svc.load(
        channel.id,
        period_start=window_start,
        period_end=window_end,
    )
    assert rev_view.is_net_profit is False
    assert rev_view.estimated_margin is not None
    assert rev_view.estimated_margin.margin == Decimal("22.50")
    rev_html = rev_svc.render_html(rev_view)
    assert "Contribution Margin" in rev_html

    # 5. Channel, Analytics, AI Center, Settings, and Alerts screens load cleanly
    assert container.resolve(ChannelOverviewService).load(channel.id).channel_id == channel.id
    assert container.resolve(AnalyticsScreenService).load(channel.id).channel_id == channel.id
    assert container.resolve(AICenterScreenService).load(channel.id).channel_id == channel.id
    assert container.resolve(SettingsScreenService).load(channel.id).channel_id == channel.id
    assert container.resolve(AlertsScreenService).load(channel.id).channel_id == channel.id
