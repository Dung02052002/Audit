"""Tests for Approval Center UI view and service (Prompt Pack v8, prompt Q-200)."""

from datetime import UTC, datetime

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.dashboard_approvals import ApprovalCenterService
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
from ai_youtube_agent.core.db.repositories.review import (
    ApprovalRequestRepository,
    QCResultRepository,
)
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


def test_approval_center_empty_when_no_channels(database: Database) -> None:
    service = ApprovalCenterService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.channel_id is None
    assert view.pending_count == 0


def test_approval_center_shows_pending_approvals_exact_artifact_version_and_handles_decision(
    database: Database,
) -> None:
    channel = make_channel(title="Review Studio", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)
    item = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Compound Interest Explained",
        status=ContentStatus.AWAITING_APPROVAL,
    )
    video_art = make_artifact(item, ArtifactKind.VIDEO, media_type="video/mp4")
    qc = make_qc_result(item, [video_art])
    req = make_approval_request(item, [video_art], qc_result=qc)

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(item)
        ArtifactRepository(conn).add(video_art)
        QCResultRepository(conn).add(qc)
        ApprovalRequestRepository(conn).add(req)

    service = ApprovalCenterService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id)

    assert view.shell.active_screen is DashboardScreenId.APPROVALS
    assert view.load_state is ViewLoadState.READY
    assert view.pending_count == 1
    assert len(view.pending_approvals) == 1
    pending = view.pending_approvals[0]
    assert pending.approval_request_id == req.id
    assert pending.video_artifact_id == video_art.id
    assert pending.video_version == video_art.version
    assert pending.video_sha256 == video_art.sha256
    assert pending.is_stale is False
    assert pending.is_expired is False

    html_out = service.render_html(view)
    assert 'data-testid="approval-center-screen"' in html_out
    assert "Compound Interest Explained" in html_out
    assert video_art.sha256[:12] in html_out

    # Approve bound to exact video artifact version
    decision = service.approve(
        req.id,
        actor=USER,
        expected_video_artifact_id=video_art.id,
        expected_video_version=video_art.version,
        note="Looks ready to publish",
    )
    assert decision.approval.status is ApprovalStatus.APPROVED
    assert decision.content_item.status is ContentStatus.APPROVED

    after = service.load(channel.id)
    assert after.pending_count == 0
    assert after.approved_count == 1

