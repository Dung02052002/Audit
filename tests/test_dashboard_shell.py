"""Tests for the Dashboard Shell and shared iOS-style design system (Q-191, prompt
#191).
"""

from datetime import UTC, datetime

import pytest

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import (
    Comment,
    CommentClassification,
    CommentLabel,
    ReplyDraft,
)
from ai_youtube_agent.content.dashboard_shell import (
    DEFAULT_IOS_DESIGN_TOKENS,
    BadgeTone,
    DashboardError,
    DashboardErrorCode,
    DashboardScreenId,
    DashboardShellService,
    ViewLoadState,
    build_navigation,
    render_dashboard_shell_html,
    render_design_system_css,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.job import AIJob
from factories import (
    USER,
    make_approval_request,
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_design_system_tokens_are_immutable_and_render_css_variables() -> None:
    tokens = DEFAULT_IOS_DESIGN_TOKENS
    css = render_design_system_css(tokens)

    assert tokens.theme_name == "ios_command_center"
    assert "--cc-color-accent-blue: #007AFF;" in css
    assert "--cc-blur: 20px;" in css
    assert "--cc-radius-md: 14px;" in css
    with pytest.raises(TypeError):
        tokens.colors["accent_blue"] = "#000000"  # type: ignore[index]


def test_build_navigation_marks_active_screen_and_populates_badges() -> None:
    nav = build_navigation(
        DashboardScreenId.APPROVALS,
        channel_id="ch-1",
        pending_approvals=3,
        attention_comments=5,
        active_jobs=2,
        alerts_count=1,
    )

    by_id = {dest.screen_id: dest for dest in nav}
    assert by_id[DashboardScreenId.APPROVALS].active is True
    assert by_id[DashboardScreenId.APPROVALS].badge_count == 3
    assert by_id[DashboardScreenId.APPROVALS].badge_tone is BadgeTone.WARNING
    assert (
        by_id[DashboardScreenId.APPROVALS].href
        == "/dashboard/approvals?channel_id=ch-1"
    )
    assert by_id[DashboardScreenId.COMMENTS].badge_count == 5
    assert by_id[DashboardScreenId.AI_CENTER].badge_count == 2
    assert by_id[DashboardScreenId.ALERTS].badge_count == 1
    assert by_id[DashboardScreenId.ALERTS].badge_tone is BadgeTone.DANGER
    assert by_id[DashboardScreenId.HOME].active is False

    detail_nav = build_navigation(DashboardScreenId.VIDEO_DETAIL)
    content_dest = next(
        d for d in detail_nav if d.screen_id is DashboardScreenId.CONTENT
    )
    assert content_dest.active is True


def test_shell_service_handles_empty_state_when_no_channels_exist(
    database: Database,
) -> None:
    service = DashboardShellService(database)
    shell = service.build_shell(active_screen=DashboardScreenId.HOME)

    assert shell.load_state is ViewLoadState.EMPTY
    assert shell.active_channel_id is None
    assert shell.channels == ()
    assert shell.pending_approvals_badge == 0
    html_text = render_dashboard_shell_html(shell)
    assert 'data-load-state="empty"' in html_text
    assert "No channels configured" in html_text


def test_shell_service_computes_live_badges_and_escapes_html(
    database: Database,
) -> None:
    channel = make_channel(title='Studio <Alpha> & "Beta"', status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    item = make_content_item(channel, profile, status=ContentStatus.AWAITING_APPROVAL)
    video = make_artifact(item, ArtifactKind.VIDEO)
    approval = make_approval_request(item, [video])
    comment = Comment(
        id="c-1",
        channel_id=channel.id,
        youtube_comment_id="yt-c-1",
        youtube_video_id="vid000001ab",
        parent_comment_id=None,
        author_display_name="Viewer",
        text="Great video, question?",
        published_at=NOW,
    )
    classification = CommentClassification.create(
        comment.id,
        CommentLabel.NEEDS_ATTENTION,
        classified_by=USER,
        rationale="Creator question",
        clock=lambda: NOW,
    )
    draft = ReplyDraft.create(
        comment.id, "Thanks for watching!", created_by=USER, clock=lambda: NOW
    )
    running_job = AIJob.create(
        "video.render",
        "gen:shell-test-1",
        content_item_id=item.id,
        clock=lambda: NOW,
    ).start(clock=lambda: NOW)

    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(profile)
        ContentItemRepository(connection).add(item)
        ArtifactRepository(connection).add(video)
        ApprovalRequestRepository(connection).add(approval)
        CommentRepository(connection).add(comment)
        CommentRepository(connection).add_classification(classification)
        CommentRepository(connection).add_reply_draft(draft)
        AIJobRepository(connection).add(running_job)

    service = DashboardShellService(database)
    shell = service.build_shell(
        active_screen=DashboardScreenId.HOME, channel_id=channel.id
    )

    assert shell.load_state is ViewLoadState.READY
    assert shell.active_channel_id == channel.id
    assert shell.pending_approvals_badge == 1
    assert shell.attention_comments_badge == 1
    assert shell.active_jobs_badge == 1
    assert len(shell.primary_tabs) == 5
    assert len(shell.secondary_destinations) == 5

    rendered = service.render_html(
        active_screen=DashboardScreenId.HOME, channel_id=channel.id
    )
    assert "Studio &lt;Alpha&gt; &amp; &quot;Beta&quot;" in rendered
    assert "<Alpha>" not in rendered

    with pytest.raises(DashboardError) as caught:
        service.build_shell(channel_id="missing-channel")
    assert caught.value.dashboard_code is DashboardErrorCode.CHANNEL_NOT_FOUND
