"""Tests for Content Screen view and service (Prompt Pack v8, prompt Q-194)."""

from datetime import UTC, datetime

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.dashboard_content import ContentScreenService
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
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_content_screen_empty_when_no_channels(database: Database) -> None:
    service = ContentScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.total_items == 0
    assert view.shorts_column.total_count == 0
    assert view.longform_column.total_count == 0
    html_out = service.render_html(view)
    assert 'data-testid="content-screen"' in html_out
    assert 'data-testid="content-empty-banner"' in html_out


def test_content_screen_separates_shorts_and_longform_by_lifecycle_state(
    database: Database,
) -> None:
    channel = make_channel(title="TechShorts Studio", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)

    short_draft = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Short Draft 1",
        status=ContentStatus.DRAFT,
    )
    short_awaiting = make_content_item(
        channel,
        profile,
        content_type=ContentType.SHORTS,
        title="Short Ready for Approval",
        status=ContentStatus.AWAITING_APPROVAL,
    )
    long_testing = make_content_item(
        channel,
        profile,
        content_type=ContentType.LONGFORM,
        title="LongForm Deep Dive",
        status=ContentStatus.TESTING,
    )

    video_art = make_artifact(
        short_awaiting,
        ArtifactKind.VIDEO,
        media_type="video/mp4",
    )

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)
        ContentItemRepository(conn).add(short_draft)
        ContentItemRepository(conn).add(short_awaiting)
        ContentItemRepository(conn).add(long_testing)
        ArtifactRepository(conn).add(video_art)

    service = ContentScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load(channel.id)

    assert view.shell.active_screen is DashboardScreenId.CONTENT
    assert view.load_state is ViewLoadState.READY
    assert view.total_items == 3
    assert view.shorts_column.total_count == 2
    assert view.longform_column.total_count == 1

    assert view.shorts_column.stage_for(ContentStatus.DRAFT).count == 1
    assert view.shorts_column.stage_for(ContentStatus.AWAITING_APPROVAL).count == 1
    awaiting_card = view.shorts_column.stage_for(ContentStatus.AWAITING_APPROVAL).items[
        0
    ]
    assert awaiting_card.latest_video_version == 1
    assert awaiting_card.detail_href == f"/content/{short_awaiting.id}"

    assert view.longform_column.stage_for(ContentStatus.TESTING).count == 1
    assert view.longform_column.stage_for(ContentStatus.DRAFT).count == 0

    filtered = service.load(
        channel.id,
        content_type=ContentType.LONGFORM,
        status=ContentStatus.TESTING,
    )
    assert filtered.total_items == 1
    assert filtered.shorts_column.total_count == 0
    assert filtered.longform_column.total_count == 1
    assert len(filtered.longform_column.stages) == 1

    html_out = service.render_html(view)
    assert 'data-testid="content-column-shorts"' in html_out
    assert 'data-testid="content-column-longform"' in html_out
    assert "Short Ready for Approval" in html_out
    assert "LongForm Deep Dive" in html_out
