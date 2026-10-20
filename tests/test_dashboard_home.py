"""Tests for the Command Center Home Screen (Q-192, prompt #192)."""

from datetime import UTC, datetime
from decimal import Decimal

from ai_youtube_agent.content.budget_guard import BudgetGuard
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.cost import CostCategory, CostRecord
from ai_youtube_agent.content.dashboard_home import HomeScreenService
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardShellService,
    ViewLoadState,
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
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.db.repositories.usage import ProductionStartRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import (
    CheckKind,
    HealthCheck,
    HealthRegistry,
)
from ai_youtube_agent.core.production_start import ProductionStart
from ai_youtube_agent.pipeline.job import AIJob, Session
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _registry(*, healthy: bool = True) -> HealthRegistry:
    reg = HealthRegistry()
    reg.register(
        HealthCheck("settings", CheckKind.APPLICATION, lambda: None)
    )
    if not healthy:
        reg.register(
            HealthCheck(
                "voice_provider",
                CheckKind.PROVIDER,
                lambda: (_ for _ in ()).throw(RuntimeError("degraded")),
            )
        )
    return reg


def test_home_screen_shows_channel_health_ai_status_production_approvals_and_alerts(
    database: Database,
) -> None:
    channel = make_channel(title="Main Channel", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    item_pending = make_content_item(
        channel,
        profile,
        title="Short Pending Review",
        content_type=ContentType.SHORTS,
        status=ContentStatus.AWAITING_APPROVAL,
    )
    item_published = make_content_item(
        channel,
        profile,
        title="Published Short",
        content_type=ContentType.SHORTS,
        status=ContentStatus.PUBLISHED,
    )
    item_failed = make_content_item(
        channel,
        profile,
        title="Failed LongForm",
        content_type=ContentType.LONGFORM,
        status=ContentStatus.FAILED,
    )
    video = make_artifact(item_pending, ArtifactKind.VIDEO)
    approval = make_approval_request(item_pending, [video])
    session = Session.start(USER, clock=lambda: NOW)
    running_job = AIJob.create(
        "video.render",
        "gen:home-1",
        content_item_id=item_pending.id,
        session_id=session.id,
        clock=lambda: NOW,
    ).start(clock=lambda: NOW)
    prod_start = ProductionStart.create(item_pending, clock=lambda: NOW)
    cost = CostRecord.create(
        channel.id,
        CostCategory.TTS,
        provider="mock",
        amount=Decimal("9.00"),
        currency="USD",
        content_item_id=item_pending.id,
        ref=video.id,
        clock=lambda: NOW,
    )

    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        ContentItemRepository(connection).add(item_pending)
        ContentItemRepository(connection).add(item_published)
        ContentItemRepository(connection).add(item_failed)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        SessionRepository(connection).add(session)
        AIJobRepository(connection).add(running_job)
        ProductionStartRepository(connection).add(prod_start)
        CostRecordRepository(connection).add(cost)

    sink = InMemoryAuditSink()
    guard = BudgetGuard(database, AuditLog(sink), clock=lambda: NOW)
    service = HomeScreenService(
        database,
        DashboardShellService(database),
        _registry(healthy=True),
        FeatureFlags(),
        budget_guard=guard,
        clock=lambda: NOW,
    )

    view = service.build_view(channel_id=channel.id)

    assert view.load_state is ViewLoadState.READY
    assert view.channel_health is not None
    assert view.channel_health.channel_status == "active"
    assert view.channel_health.strategy_complete is True
    assert view.channel_health.system_health_status == "ok"
    assert view.channel_health.tone is BadgeTone.SUCCESS

    assert view.ai_status.state == "running"
    assert view.ai_status.active_session_id == session.id
    assert view.ai_status.running_jobs == 1
    assert view.ai_status.shorts_enabled is True
    assert view.ai_status.longform_enabled is False
    assert view.ai_status.auto_reply_enabled is False

    assert view.production is not None
    assert view.production.shorts_in_progress == 1
    assert view.production.shorts_published_total == 1
    assert view.production.failed_items_count == 1
    assert view.production.today_shorts_started == 1

    assert view.pending_approvals_count == 1
    assert view.pending_approvals[0].approval_request_id == approval.id
    assert view.pending_approvals[0].video_version == 1

    categories = {alert.category for alert in view.alerts}
    assert "approval" in categories
    assert "failure" in categories
    assert "budget" in categories

    html_output = service.render_html(channel_id=channel.id)
    assert 'data-widget="channel-health"' in html_output
    assert 'data-widget="ai-status"' in html_output
    assert 'data-widget="production-count"' in html_output
    assert "Short Pending Review" in html_output


def test_home_screen_empty_state_when_no_channels(database: Database) -> None:
    service = HomeScreenService(
        database,
        DashboardShellService(database),
        _registry(healthy=False),
        FeatureFlags(),
        clock=lambda: NOW,
    )
    view = service.build_view()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.channel_health is None
    assert view.production is None
    assert len(view.alerts) == 1
    assert view.alerts[0].category == "health"

