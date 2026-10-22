"""Tests for Command Center Settings screen view and service.

(Prompt Pack v8, prompt Q-201).
"""

from datetime import UTC, datetime

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.dashboard_settings import SettingsScreenService
from ai_youtube_agent.content.dashboard_shell import DashboardScreenId, ViewLoadState
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from factories import make_channel, make_strategy_profile

FIXED_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_settings_screen_empty_when_no_channels(database: Database) -> None:
    service = SettingsScreenService(database, clock=lambda: FIXED_NOW)
    view = service.load()

    assert view.load_state is ViewLoadState.EMPTY
    assert view.channel_id is None
    assert view.channel is None
    assert view.flags.shorts_enabled is True
    assert view.flags.longform_enabled is False
    assert view.providers.research_provider == "mock"

    html_out = service.render_html(view)
    assert 'data-testid="settings-screen"' in html_out
    assert "No channel selected or configured" in html_out


def test_settings_screen_exposes_channel_oauth_strategy_limits_and_flags(
    database: Database,
) -> None:
    channel = make_channel(title="Tech Pulse Lab", status=ChannelStatus.ACTIVE)
    profile = make_strategy_profile(channel)

    with database.transaction() as conn:
        ChannelRepository(conn).add(channel)
        StrategyProfileRepository(conn).add(profile)

    oauth_provider = MockYouTubeOAuthProvider()
    settings = Settings()
    flags = FeatureFlags(
        shorts_enabled=True,
        longform_enabled=True,
        publish_enabled=True,
        test_required=True,
        approval_required=True,
        auto_reply_enabled=False,
    )

    service = SettingsScreenService(
        database,
        settings=settings,
        flags=flags,
        oauth_provider=oauth_provider,
        clock=lambda: FIXED_NOW,
    )

    view = service.load(channel.id)

    assert view.shell.active_screen is DashboardScreenId.SETTINGS
    assert view.load_state is ViewLoadState.READY
    assert view.channel is not None
    assert view.channel.title == "Tech Pulse Lab"
    assert view.channel.status == "active"

    # OAuth check - authorized via mock provider, no secret exposed
    assert view.oauth.is_configured is True
    assert view.oauth.is_authorized is True
    assert len(view.oauth.scopes) > 0
    assert "access_token" not in view.oauth.as_dict()

    # Strategy & Limits
    assert view.strategy is not None
    assert view.strategy.market == profile.market.country_code
    assert view.limits is not None
    assert view.limits.cadence_shorts_per_day == profile.cadence.shorts_per_day

    # Flags
    assert view.flags.shorts_enabled is True
    assert view.flags.longform_enabled is True
    assert view.flags.publish_enabled is True
    assert view.flags.auto_reply_enabled is False

    # HTML rendering check
    html_out = service.render_html(view)
    assert 'data-testid="settings-screen"' in html_out
    assert 'data-testid="settings-channel-card"' in html_out
    assert 'data-testid="settings-oauth-card"' in html_out
    assert 'data-testid="settings-flags-card"' in html_out
    assert 'data-testid="settings-limits-card"' in html_out
    assert 'data-testid="settings-strategy-card"' in html_out
    assert 'data-testid="settings-providers-card"' in html_out
    assert 'data-testid="settings-email-card"' in html_out
    assert "Tech Pulse Lab" in html_out
    assert "SHORTS_ENABLED" in html_out
    assert "LONGFORM_ENABLED" in html_out
