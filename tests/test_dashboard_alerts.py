"""Tests for Command Center Alerts screen view and service.

(Prompt Pack v8, prompt Q-202).
"""

from datetime import UTC, datetime

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.dashboard_alerts import (
    AlertsScreenService,
)
from ai_youtube_agent.content.dashboard_shell import DashboardScreenId, ViewLoadState
from ai_youtube_agent.core.artifact import ArtifactKind
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
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.errors import ApplicationError
from ai_youtube_agent.core.health import CheckKind, HealthCheck, HealthRegistry
from ai_youtube_agent.pipeline.job import AIJobStatus
from factories import (
    make_ai_job,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _degraded_render_probe() -> None:
    """A provider probe fails by raising; its safe message becomes the detail."""
    raise ApplicationError(
        "ffmpeg overloaded",
        code="application.render_overloaded",
        user_message="FFmpeg high load",
    )


def test_alerts_screen_clean_when_no_channels(database: Database) -> None:
    service = AlertsScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.channel_id is None
    assert view.total_alerts == 0
    assert view.critical_count == 0

    html_out = service.render_html(view)
    assert 'data-testid="alerts-screen"' in html_out
    assert "Everything Operational" in html_out


def test_alerts_screen_aggregates_failures_approvals_and_health(
    database: Database,
) -> None:
    channel = make_channel(title="Cyber Studio", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)

    # Failed item
    failed_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Failed Quantum Intro",
        status=ContentStatus.FAILED,
    )

    # Item with pending approval
    pending_item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Valid AI News",
        status=ContentStatus.AWAITING_APPROVAL,
    )
    video_art = make_artifact(pending_item, ArtifactKind.VIDEO)
    req = make_approval_request(pending_item, [video_art])

    # Failed AI job
    failed_job = make_ai_job(
        failed_item,
        kind="audio.generate",
        idempotency_key="job-failed-audio-1",
        status=AIJobStatus.FAILED,
        attempts=3,
        last_error="TTS synthesis timeout after 3 attempts",
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(failed_item)
        ContentItemRepository(conn).add(pending_item)
        ArtifactRepository(conn).add(video_art)
        ApprovalRequestRepository(conn).add(req)
        AIJobRepository(conn).add(failed_job)

    # Health registry with degraded provider check
    health_reg = HealthRegistry()
    health_reg.register(
        HealthCheck(
            "render_provider",
            CheckKind.PROVIDER,
            _degraded_render_probe,
        )
    )

    service = AlertsScreenService(
        database,
        health_registry=health_reg,
        clock=lambda: FIXED_NOW,
    )

    view = service.load(channel.id)

    assert view.shell.active_screen is DashboardScreenId.ALERTS
    assert view.load_state is ViewLoadState.READY
    assert view.total_alerts >= 3
    assert view.critical_count >= 2  # item failed + job failed

    # Check failure alerts
    assert len(view.failure_alerts) >= 2
    item_alerts = [a for a in view.failure_alerts if "Failed Quantum Intro" in a.title]
    assert len(item_alerts) == 1
    assert item_alerts[0].primary_action_label == "Inspect Item"

    # Check approval alerts
    assert len(view.approval_alerts) == 1
    appr_alert = view.approval_alerts[0]
    assert "Valid AI News" in appr_alert.title
    assert appr_alert.primary_action_label == "Review & Approve"

    # Check health alerts
    assert len(view.health_alerts) >= 1
    health_alert = [a for a in view.health_alerts if "Degraded" in a.title]
    assert len(health_alert) == 1

    # Render HTML verification
    html_out = service.render_html(view)
    assert 'data-testid="alerts-screen"' in html_out
    assert "Actionable Alerts &amp; Triage" in html_out
    assert "Failed Quantum Intro" in html_out
    assert "Valid AI News" in html_out
    assert "FFmpeg high load" in html_out
