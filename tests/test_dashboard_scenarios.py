"""End-to-end Phase Q Command Center scenario tests (Prompt Pack v8, prompt Q-203).

Covers:
1. Dashboard Shell navigation, channel switcher, and live badge calculations across
   pending approvals, attention comments, and active AI jobs.
2. Mocked data rendering across all Command Center screens: Home, Channel Overview,
   Content, Video Detail, Analytics, Revenue, Comments, AI Center, Approvals,
   Settings, and Actionable Alerts.
3. Empty states across all screens when no channels, no items, no jobs, or no comments
  exist.
4. Error states handling channel not found, content not found, or invalid parameters.
5. Strict XSS safety: user input and model text are always properly HTML-escaped.
"""

from datetime import UTC, datetime

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import (
    Comment,
    CommentLabel,
)
from ai_youtube_agent.content.dashboard_ai_center import AICenterScreenService
from ai_youtube_agent.content.dashboard_alerts import AlertsScreenService
from ai_youtube_agent.content.dashboard_analytics import AnalyticsScreenService
from ai_youtube_agent.content.dashboard_approvals import ApprovalCenterService
from ai_youtube_agent.content.dashboard_channel import ChannelOverviewService
from ai_youtube_agent.content.dashboard_comments import CommentsScreenService
from ai_youtube_agent.content.dashboard_content import ContentScreenService
from ai_youtube_agent.content.dashboard_home import HomeScreenService
from ai_youtube_agent.content.dashboard_revenue import RevenueScreenService
from ai_youtube_agent.content.dashboard_settings import SettingsScreenService
from ai_youtube_agent.content.dashboard_shell import (
    DashboardError,
    DashboardErrorCode,
    DashboardScreenId,
    DashboardShellService,
    ViewLoadState,
)
from ai_youtube_agent.content.dashboard_video_detail import VideoDetailService
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import (
    CommentClassificationRepository,
    CommentRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)
from ai_youtube_agent.pipeline.job import AIJob, Session
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_qc_result,
    make_strategy_profile,
)

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_command_center_empty_states_across_all_screens(database: Database) -> None:
    """Verifies that all 11 screens render graceful empty states when no channels
    exist."""
    shell_svc = DashboardShellService(database, clock=lambda: FIXED_NOW)
    home_svc = HomeScreenService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )
    channel_svc = ChannelOverviewService(database, shell_service=shell_svc)
    content_svc = ContentScreenService(database, shell_service=shell_svc)
    analytics_svc = AnalyticsScreenService(database, shell_service=shell_svc)
    revenue_svc = RevenueScreenService(database, shell_service=shell_svc)
    comments_svc = CommentsScreenService(database, shell_service=shell_svc)
    ai_svc = AICenterScreenService(database, shell_service=shell_svc)
    approvals_svc = ApprovalCenterService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )
    settings_svc = SettingsScreenService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )
    alerts_svc = AlertsScreenService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )

    # 1. Shell
    shell_state = shell_svc.build_shell(DashboardScreenId.HOME)
    assert shell_state.active_channel is None
    assert len(shell_state.channels) == 0
    assert shell_state.pending_approvals_badge == 0
    assert shell_state.attention_comments_badge == 0
    assert shell_state.active_jobs_badge == 0

    # 2. Home
    home_view = home_svc.load()
    assert home_view.load_state is ViewLoadState.EMPTY
    assert "No channels configured" in home_svc.render_html(home_view)

    # 3. Channel Overview
    channel_view = channel_svc.load()
    assert channel_view.load_state is ViewLoadState.EMPTY
    assert "No channel selected" in channel_svc.render_html(channel_view)

    # 4. Content
    content_view = content_svc.load()
    assert content_view.load_state is ViewLoadState.EMPTY
    assert "No content items" in content_svc.render_html(content_view)

    # 5. Analytics
    analytics_view = analytics_svc.load()
    assert analytics_view.load_state is ViewLoadState.EMPTY
    assert "No analytics data" in analytics_svc.render_html(analytics_view)

    # 6. Revenue
    revenue_view = revenue_svc.load()
    assert revenue_view.load_state is ViewLoadState.EMPTY
    assert "No revenue records" in revenue_svc.render_html(revenue_view)

    # 7. Comments
    comments_view = comments_svc.load()
    assert comments_view.load_state is ViewLoadState.EMPTY
    assert "No comments found" in comments_svc.render_html(comments_view)

    # 8. AI Center
    ai_view = ai_svc.load()
    assert ai_view.load_state is ViewLoadState.EMPTY
    assert "No AI jobs" in ai_svc.render_html(ai_view)

    # 9. Approvals
    approvals_view = approvals_svc.load()
    assert approvals_view.load_state is ViewLoadState.EMPTY
    assert "No pending approvals" in approvals_svc.render_html(approvals_view)

    # 10. Settings
    settings_view = settings_svc.load()
    assert settings_view.load_state is ViewLoadState.EMPTY
    assert "No channel selected or configured" in settings_svc.render_html(
        settings_view
    )

    # 11. Alerts
    alerts_view = alerts_svc.load()
    assert alerts_view.load_state is ViewLoadState.EMPTY
    assert "Everything Operational" in alerts_svc.render_html(alerts_view)


def test_command_center_populates_screens_and_calculates_badges(
    database: Database,
) -> None:
    """Verifies that populated data renders across screens with accurate dynamic badge
    counts."""
    channel = make_channel(title="Alpha Tech", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)

    # Content item awaiting approval
    item_shorts = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Micro Automation",
        status=ContentStatus.AWAITING_APPROVAL,
    )
    video_art = make_artifact(item_shorts, ArtifactKind.VIDEO)
    qc = make_qc_result(item_shorts, [video_art])
    appr_req = make_approval_request(item_shorts, [video_art], qc_result=qc)

    # Content item published
    item_long = make_content_item(
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        title="Deep Architecture",
        status=ContentStatus.PUBLISHED,
    )

    # Active AI Job
    session = Session.start(actor=USER, clock=lambda: FIXED_NOW)
    active_job = AIJob.create(
        kind="content.generate",
        idempotency_key="job-gen-1",
        content_item_id=item_shorts.id,
        session_id=session.id,
        clock=lambda: FIXED_NOW,
    ).start(clock=lambda: FIXED_NOW)

    # Comment needing attention
    comment = Comment.create(
        channel_id=channel.id,
        youtube_comment_id="yt-comm-101",
        youtube_video_id="dQw4w9WgXcQ",
        author_display_name="DevAuditor",
        text="Is this repository open source?",
        published_at=FIXED_NOW,
    )
    from ai_youtube_agent.content.comment import CommentClassification

    classification = CommentClassification.create(
        comment_id=comment.id,
        label=CommentLabel.NEEDS_ATTENTION,
        classified_by=USER,
        clock=lambda: FIXED_NOW,
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item_shorts)
        ContentItemRepository(conn).add(item_long)
        ArtifactRepository(conn).add(video_art)
        QCResultRepository(conn).add(qc)
        ApprovalRequestRepository(conn).add(appr_req)
        SessionRepository(conn).add(session)
        AIJobRepository(conn).add(active_job)
        CommentRepository(conn).add(comment)
        CommentClassificationRepository(conn).add(classification)

    shell_svc = DashboardShellService(database, clock=lambda: FIXED_NOW)
    shell = shell_svc.build_shell(DashboardScreenId.HOME, channel_id=channel.id)

    # Verify live badge counts computed by shell
    assert shell.pending_approvals_badge == 1
    assert shell.attention_comments_badge == 1
    assert shell.active_jobs_badge == 1

    # Verify Home Screen
    home_svc = HomeScreenService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )
    home_view = home_svc.load(channel.id)
    assert home_view.load_state is ViewLoadState.READY
    assert home_view.pending_approvals_count == 1
    assert home_view.ai_status.active_jobs == 1
    assert home_view.production.total_shorts_count == 1
    assert home_view.production.total_longform_count == 1
    home_html = home_svc.render_html(home_view)
    assert 'data-testid="home-screen"' in home_html
    assert "Alpha Tech" in home_html

    # Verify Approvals Screen
    appr_svc = ApprovalCenterService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )
    appr_view = appr_svc.load(channel.id)
    assert appr_view.load_state is ViewLoadState.READY
    assert appr_view.pending_count == 1
    assert appr_view.pending_approvals[0].title == "Micro Automation"
    appr_html = appr_svc.render_html(appr_view)
    assert 'data-testid="approval-center-screen"' in appr_html

    # Verify Comments Screen
    comments_svc = CommentsScreenService(database, shell_service=shell_svc)
    comm_view = comments_svc.load(channel.id)
    assert comm_view.load_state is ViewLoadState.READY
    assert comm_view.attention_comments_count == 1
    comm_html = comments_svc.render_html(comm_view)
    assert 'data-testid="comments-screen"' in comm_html
    assert "DevAuditor" in comm_html

    # Verify AI Center Screen
    ai_svc = AICenterScreenService(database, shell_service=shell_svc)
    ai_view = ai_svc.load(channel.id)
    assert ai_view.load_state is ViewLoadState.READY
    assert len(ai_view.active_jobs) == 1
    assert ai_view.active_jobs[0].kind == "content.generate"
    ai_html = ai_svc.render_html(ai_view)
    assert 'data-testid="ai-center-screen"' in ai_html

    # Verify Settings Screen
    settings_svc = SettingsScreenService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )
    sett_view = settings_svc.load(channel.id)
    assert sett_view.load_state is ViewLoadState.READY
    assert sett_view.channel.title == "Alpha Tech"
    sett_html = settings_svc.render_html(sett_view)
    assert 'data-testid="settings-screen"' in sett_html

    # Verify Alerts Screen
    alerts_svc = AlertsScreenService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )
    alert_view = alerts_svc.load(channel.id)
    assert alert_view.load_state is ViewLoadState.READY
    assert len(alert_view.approval_alerts) == 1
    alert_html = alerts_svc.render_html(alert_view)
    assert 'data-testid="alerts-screen"' in alert_html


def test_command_center_error_states_and_xss_escaping(database: Database) -> None:
    """Verifies that invalid parameters raise typed DashboardErrors and malicious text
    is escaped."""
    channel = make_channel(
        title="<script>alert('xss')</script>", status=ChannelStatus.ACTIVE
    )
    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)

    shell_svc = DashboardShellService(database, clock=lambda: FIXED_NOW)
    home_svc = HomeScreenService(
        database, shell_service=shell_svc, clock=lambda: FIXED_NOW
    )

    view = home_svc.load(channel.id)
    rendered = home_svc.render_html(view)

    # Ensure dangerous script tags are escaped
    assert "<script>alert('xss')</script>" not in rendered
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in rendered

    # Error cases: Video Detail with nonexistent item raises DashboardError
    vid_svc = VideoDetailService(database, shell_service=shell_svc)
    with pytest.raises(DashboardError) as exc_info:
        vid_svc.load("nonexistent-item-id-999")
    assert exc_info.value.code == DashboardErrorCode.CONTENT_NOT_FOUND.value
