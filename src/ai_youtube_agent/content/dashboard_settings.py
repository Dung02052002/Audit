"""Settings screen view and service (Prompt Pack v8, prompt Q-201), context X3
Command Center.

Builds the Command Center Settings screen view model and iOS-style HTML rendering,
exposing:
- Channel details (ID, title, handle, YouTube channel ID, status, timestamps).
- YouTube OAuth configuration and token authorization status (scopes, validity,
  without ever exposing secret access or refresh tokens, fulfilling R-07).
- Email and approval notification configuration (approval TTL hours, notifications).
- Configured AI, voice, video render, research, analytics, and upload providers.
- Channel StrategyProfile (version, market, languages, audience, niche, brand,
  monetization goals, completeness).
- Cadence and budget limits (shorts/longform daily limits, time zone, monthly/daily
  budget, currency, alert thresholds).
- Feature flags (SHORTS_ENABLED, LONGFORM_ENABLED, PUBLISH_ENABLED, TEST_REQUIRED,
  APPROVAL_REQUIRED, AUTO_REPLY_ENABLED).
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    YouTubeOAuthProvider,
)

Clock = Callable[[], datetime]

_CHANNEL_STATUS_TONES: Final[dict[ChannelStatus, BadgeTone]] = {
    ChannelStatus.ACTIVE: BadgeTone.SUCCESS,
    ChannelStatus.PAUSED: BadgeTone.WARNING,
    ChannelStatus.PENDING: BadgeTone.NEUTRAL,
    ChannelStatus.DISCONNECTED: BadgeTone.DANGER,
    ChannelStatus.ARCHIVED: BadgeTone.NEUTRAL,
}


@dataclass(frozen=True)
class ChannelSettingsView:
    channel_id: str
    title: str
    handle: str | None
    youtube_channel_id: str
    status: str
    tone: BadgeTone
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "title": self.title,
            "handle": self.handle,
            "youtube_channel_id": self.youtube_channel_id,
            "status": self.status,
            "tone": self.tone.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class OAuthSettingsView:
    provider_name: str
    is_configured: bool
    is_authorized: bool
    scopes: tuple[str, ...]
    is_expired: bool | None
    expires_at: datetime | None
    has_refresh_token: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "is_configured": self.is_configured,
            "is_authorized": self.is_authorized,
            "scopes": list(self.scopes),
            "is_expired": self.is_expired,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "has_refresh_token": self.has_refresh_token,
        }


@dataclass(frozen=True)
class EmailSettingsView:
    approval_ttl_hours: int
    email_notifications_enabled: bool
    recipient_email: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_ttl_hours": self.approval_ttl_hours,
            "email_notifications_enabled": self.email_notifications_enabled,
            "recipient_email": self.recipient_email,
        }


@dataclass(frozen=True)
class ProvidersSettingsView:
    research_provider: str
    text_provider: str
    voice_provider: str
    tts_price_per_1k: str
    tts_cost_currency: str
    render_provider: str
    ffmpeg_binary: str
    render_timeout_seconds: int
    upload_provider: str
    upload_max_attempts: int
    upload_retry_base_delay_seconds: int
    analytics_provider: str
    revenue_provider: str
    comment_provider: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "research_provider": self.research_provider,
            "text_provider": self.text_provider,
            "voice_provider": self.voice_provider,
            "tts_price_per_1k": self.tts_price_per_1k,
            "tts_cost_currency": self.tts_cost_currency,
            "render_provider": self.render_provider,
            "ffmpeg_binary": self.ffmpeg_binary,
            "render_timeout_seconds": self.render_timeout_seconds,
            "upload_provider": self.upload_provider,
            "upload_max_attempts": self.upload_max_attempts,
            "upload_retry_base_delay_seconds": self.upload_retry_base_delay_seconds,
            "analytics_provider": self.analytics_provider,
            "revenue_provider": self.revenue_provider,
            "comment_provider": self.comment_provider,
        }


@dataclass(frozen=True)
class StrategyProfileView:
    profile_id: str | None
    version: int | None
    is_complete: bool
    missing_settings: tuple[str, ...]
    market: str | None
    primary_language: str | None
    secondary_languages: tuple[str, ...]
    audience_description: str | None
    audience_level: str | None
    niche_name: str | None
    content_pillars: tuple[str, ...]
    brand_name: str | None
    brand_tone: str | None
    banned_phrases_count: int
    monetization_currency: str | None
    monetization_goals: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "is_complete": self.is_complete,
            "missing_settings": list(self.missing_settings),
            "market": self.market,
            "primary_language": self.primary_language,
            "secondary_languages": list(self.secondary_languages),
            "audience_description": self.audience_description,
            "audience_level": self.audience_level,
            "niche_name": self.niche_name,
            "content_pillars": list(self.content_pillars),
            "brand_name": self.brand_name,
            "brand_tone": self.brand_tone,
            "banned_phrases_count": self.banned_phrases_count,
            "monetization_currency": self.monetization_currency,
            "monetization_goals": list(self.monetization_goals),
        }


@dataclass(frozen=True)
class LimitsSettingsView:
    cadence_shorts_per_day: int | None
    cadence_longform_per_day: int | None
    cadence_time_zone: str
    budget_daily_limit: str | None
    budget_monthly_limit: str | None
    budget_currency: str | None
    budget_alert_thresholds: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cadence_shorts_per_day": self.cadence_shorts_per_day,
            "cadence_longform_per_day": self.cadence_longform_per_day,
            "cadence_time_zone": self.cadence_time_zone,
            "budget_daily_limit": self.budget_daily_limit,
            "budget_monthly_limit": self.budget_monthly_limit,
            "budget_currency": self.budget_currency,
            "budget_alert_thresholds": list(self.budget_alert_thresholds),
        }


@dataclass(frozen=True)
class FeatureFlagsView:
    shorts_enabled: bool
    longform_enabled: bool
    publish_enabled: bool
    test_required: bool
    approval_required: bool
    auto_reply_enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "shorts_enabled": self.shorts_enabled,
            "longform_enabled": self.longform_enabled,
            "publish_enabled": self.publish_enabled,
            "test_required": self.test_required,
            "approval_required": self.approval_required,
            "auto_reply_enabled": self.auto_reply_enabled,
        }


@dataclass(frozen=True)
class SettingsScreenView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    channel: ChannelSettingsView | None
    oauth: OAuthSettingsView
    email: EmailSettingsView
    providers: ProvidersSettingsView
    strategy: StrategyProfileView | None
    limits: LimitsSettingsView | None
    flags: FeatureFlagsView
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "screen": DashboardScreenId.SETTINGS.value,
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "channel": self.channel.as_dict() if self.channel else None,
            "oauth": self.oauth.as_dict(),
            "email": self.email.as_dict(),
            "providers": self.providers.as_dict(),
            "strategy": self.strategy.as_dict() if self.strategy else None,
            "limits": self.limits.as_dict() if self.limits else None,
            "flags": self.flags.as_dict(),
            "empty_message": self.empty_message,
        }


class SettingsScreenService:
    """Provides read-only settings data and iOS-styled HTML for Command Center
    Settings.
    """

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        flags: FeatureFlags | None = None,
        oauth_provider: YouTubeOAuthProvider | None = None,
        shell_service: DashboardShellService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or Settings()
        self._flags = flags or self._settings.flags
        self._oauth_provider = oauth_provider
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )
        self._clock = clock

    def load(self, channel_id: str | None = None) -> SettingsScreenView:
        shell = self._shell_service.build_shell(
            DashboardScreenId.SETTINGS,
            channel_id=channel_id,
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)

            strategy_profile: StrategyProfile | None = None
            if selected_channel is not None:
                strategy_profile = StrategyProfileRepository(connection).get_by_channel(
                    selected_channel.id
                )
        finally:
            connection.close()

        flags_view = FeatureFlagsView(
            shorts_enabled=self._flags.shorts_enabled,
            longform_enabled=self._flags.longform_enabled,
            publish_enabled=self._flags.publish_enabled,
            test_required=self._flags.test_required,
            approval_required=self._flags.approval_required,
            auto_reply_enabled=self._flags.auto_reply_enabled,
        )

        email_view = EmailSettingsView(
            approval_ttl_hours=self._settings.approval_ttl_hours,
            email_notifications_enabled=True,
            recipient_email="local-admin@agent.internal",
        )

        providers_view = ProvidersSettingsView(
            research_provider=self._settings.research_provider.value,
            text_provider=self._settings.text_provider.value,
            voice_provider=self._settings.voice_provider.value,
            tts_price_per_1k=str(self._settings.tts_price_per_1k_characters),
            tts_cost_currency=self._settings.tts_cost_currency,
            render_provider=self._settings.render_provider.value,
            ffmpeg_binary=self._settings.ffmpeg_binary,
            render_timeout_seconds=self._settings.render_timeout_seconds,
            upload_provider="mock",
            upload_max_attempts=self._settings.upload_max_attempts,
            upload_retry_base_delay_seconds=self._settings.upload_retry_base_delay_seconds,
            analytics_provider="mock_analytics",
            revenue_provider="mock_revenue",
            comment_provider="mock_comments",
        )

        oauth_view = self._build_oauth_view(selected_channel)

        if selected_channel is None:
            return SettingsScreenView(
                shell=shell,
                load_state=ViewLoadState.EMPTY,
                channel_id=None,
                channel=None,
                oauth=oauth_view,
                email=email_view,
                providers=providers_view,
                strategy=None,
                limits=None,
                flags=flags_view,
                empty_message=(
                    "No channel selected or configured. "
                    "Create a channel to manage channel settings."
                ),
            )

        channel_view = ChannelSettingsView(
            channel_id=selected_channel.id,
            title=selected_channel.title,
            handle=selected_channel.identifiers.handle,
            youtube_channel_id=selected_channel.identifiers.channel_id,
            status=selected_channel.status.value,
            tone=_CHANNEL_STATUS_TONES.get(selected_channel.status, BadgeTone.NEUTRAL),
            created_at=selected_channel.created_at,
            updated_at=selected_channel.updated_at,
        )

        strategy_view = self._build_strategy_view(strategy_profile)
        limits_view = self._build_limits_view(strategy_profile)

        return SettingsScreenView(
            shell=shell,
            load_state=ViewLoadState.READY,
            channel_id=selected_channel.id,
            channel=channel_view,
            oauth=oauth_view,
            email=email_view,
            providers=providers_view,
            strategy=strategy_view,
            limits=limits_view,
            flags=flags_view,
        )

    def _build_oauth_view(self, channel: Channel | None) -> OAuthSettingsView:
        scopes = tuple(s.value for s in OAuthScope)
        if self._oauth_provider is None or channel is None:
            return OAuthSettingsView(
                provider_name="youtube_oauth",
                is_configured=self._oauth_provider is not None,
                is_authorized=False,
                scopes=scopes,
                is_expired=None,
                expires_at=None,
                has_refresh_token=False,
            )

        try:
            token = self._oauth_provider.get_token(channel.id)
            eval_now = self._now()
            expired = token.is_expired(eval_now)
            return OAuthSettingsView(
                provider_name=self._oauth_provider.name,
                is_configured=True,
                is_authorized=True,
                scopes=tuple(s.value for s in token.scopes),
                is_expired=expired,
                expires_at=token.expires_at,
                has_refresh_token=token.refresh_token is not None,
            )
        except Exception:
            return OAuthSettingsView(
                provider_name=self._oauth_provider.name,
                is_configured=True,
                is_authorized=False,
                scopes=scopes,
                is_expired=None,
                expires_at=None,
                has_refresh_token=False,
            )

    def _build_strategy_view(
        self, profile: StrategyProfile | None
    ) -> StrategyProfileView | None:
        if profile is None:
            return None

        sec_langs = profile.languages.secondary if profile.languages is not None else ()
        pillars = (
            tuple(p.name for p in profile.niche.pillars)
            if profile.niche is not None
            else ()
        )
        banned_count = (
            len(profile.brand.banned_phrases) if profile.brand is not None else 0
        )
        goals = (
            tuple(g.as_dict() for g in profile.monetization.goals)
            if profile.monetization is not None
            else ()
        )

        return StrategyProfileView(
            profile_id=profile.id,
            version=profile.version,
            is_complete=profile.is_complete,
            missing_settings=profile.missing_settings,
            market=profile.market.country_code if profile.market else None,
            primary_language=(profile.languages.primary if profile.languages else None),
            secondary_languages=sec_langs,
            audience_description=(
                profile.audience.description if profile.audience else None
            ),
            audience_level=(
                profile.audience.level.value
                if profile.audience and profile.audience.level
                else None
            ),
            niche_name=profile.niche.name if profile.niche else None,
            content_pillars=pillars,
            brand_name=profile.brand.name if profile.brand else None,
            brand_tone=profile.brand.tone if profile.brand else None,
            banned_phrases_count=banned_count,
            monetization_currency=(
                profile.monetization.currency if profile.monetization else None
            ),
            monetization_goals=goals,
        )

    def _build_limits_view(
        self, profile: StrategyProfile | None
    ) -> LimitsSettingsView | None:
        if profile is None:
            return None

        cadence = profile.cadence
        budget = profile.budget

        return LimitsSettingsView(
            cadence_shorts_per_day=cadence.shorts_per_day if cadence else None,
            cadence_longform_per_day=(cadence.longform_per_day if cadence else None),
            cadence_time_zone=cadence.time_zone if cadence else "UTC",
            budget_daily_limit=str(budget.daily_limit) if budget else None,
            budget_monthly_limit=str(budget.monthly_limit) if budget else None,
            budget_currency=budget.currency if budget else None,
            budget_alert_thresholds=(
                budget.alert_thresholds if budget else (50, 80, 100)
            ),
        )

    def render_html(self, view: SettingsScreenView) -> str:
        if view.load_state is ViewLoadState.EMPTY or view.channel is None:
            body = f"""<div class="yt-grid" data-testid="settings-screen">
  <section class="yt-card yt-grid__span-12">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Configuration</p>
        <h2 class="yt-card__title">System &amp; Channel Settings</h2>
      </div>
    </div>
    <p class="yt-empty">\
{html.escape(view.empty_message or "No channel settings available.")}</p>
  </section>
</div>"""
            return render_dashboard_shell_html(
                view.shell,
                body_html=body,
                page_title="Settings — YouTube Command Center",
            )

        ch = view.channel
        fl = view.flags
        pr = view.providers
        oa = view.oauth
        em = view.email
        st = view.strategy
        lim = view.limits

        # Feature flags pills
        def flag_pill(name: str, enabled: bool) -> str:
            tone = "success" if enabled else "neutral"
            status = "ON" if enabled else "OFF"
            return (
                '<div style="display:flex;justify-content:space-between;'
                "align-items:center;padding:8px 0;"
                'border-bottom:1px solid rgba(255,255,255,0.06);">'
                f'<span style="font-weight:500;">{html.escape(name)}</span>'
                f'<span class="yt-pill yt-pill--{tone}">{status}</span></div>'
            )

        flags_html = "".join(
            [
                flag_pill("SHORTS_ENABLED", fl.shorts_enabled),
                flag_pill("LONGFORM_ENABLED", fl.longform_enabled),
                flag_pill("PUBLISH_ENABLED", fl.publish_enabled),
                flag_pill("TEST_REQUIRED", fl.test_required),
                flag_pill("APPROVAL_REQUIRED", fl.approval_required),
                flag_pill("AUTO_REPLY_ENABLED", fl.auto_reply_enabled),
            ]
        )

        oauth_badge = (
            '<span class="yt-pill yt-pill--success">Authorized</span>'
            if oa.is_authorized
            else '<span class="yt-pill yt-pill--warning">'
            "Unauthorized / Not Connected</span>"
        )

        scopes_list = "".join(
            '<li style="font-family:monospace;font-size:12px;'
            "margin-bottom:4px;word-break:break-all;"
            f'">{html.escape(s)}</li>'
            for s in oa.scopes
        )

        body = f"""<div class="yt-grid" data-testid="settings-screen">
  <!-- Channel Identity Card -->
  <section class="yt-card yt-grid__span-6" data-testid="settings-channel-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Identity &amp; Ownership</p>
        <h2 class="yt-card__title">Channel Settings</h2>
      </div>
      <span class="yt-pill yt-pill--{ch.tone.value}">{html.escape(ch.status)}</span>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;">
      <div>
        <p class="yt-stat-label">Title</p>
        <p style="font-weight:600;font-size:16px;">{html.escape(ch.title)}</p>
      </div>
      <div>
        <p class="yt-stat-label">Handle</p>
        <p style="font-weight:500;">{html.escape(ch.handle or "—")}</p>
      </div>
      <div>
        <p class="yt-stat-label">YouTube ID</p>
        <code style="font-size:13px;">{html.escape(ch.youtube_channel_id)}</code>
      </div>
      <div>
        <p class="yt-stat-label">Internal ID</p>
        <code style="font-size:12px;">{html.escape(ch.channel_id[:12])}...</code>
      </div>
    </div>
  </section>

  <!-- OAuth & YouTube Security Card -->
  <section class="yt-card yt-grid__span-6" data-testid="settings-oauth-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Security &amp; Google API (R-07 Safe)</p>
        <h2 class="yt-card__title">YouTube OAuth</h2>
      </div>
      {oauth_badge}
    </div>
    <p style="font-size:13px;color:rgba(255,255,255,0.7);margin-bottom:8px;">Provider: \
<strong>{html.escape(oa.provider_name)}</strong> · Refreshable: <strong>\
{oa.has_refresh_token}</strong> · Expired: <strong>{oa.is_expired}</strong></p>
    <p class="yt-stat-label" style="margin-top:8px;">Granted / Required Scopes:</p>
    <ul style="padding-left:16px;margin-top:4px;color:rgba(255,255,255,0.85);">
      {scopes_list}
    </ul>
    <p style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:8px;">* Secret \
tokens and credentials are encrypted outside source control and never displayed.</p>
  </section>

  <!-- Operational Flags Card -->
  <section class="yt-card yt-grid__span-6" data-testid="settings-flags-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Governance Controls</p>
        <h2 class="yt-card__title">Feature Flags</h2>
      </div>
    </div>
    <div style="margin-top:8px;">
      {flags_html}
    </div>
  </section>

  <!-- Cadence & Daily Limits Card -->
  <section class="yt-card yt-grid__span-6" data-testid="settings-limits-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Safety Envelopes</p>
        <h2 class="yt-card__title">Limits &amp; Cadence</h2>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px;">
      <div>
        <p class="yt-stat-label">Shorts Daily Limit</p>
        <p style="font-size:18px;font-weight:700;">{
            lim.cadence_shorts_per_day
            if lim and lim.cadence_shorts_per_day is not None
            else "Unset"
        }</p>
      </div>
      <div>
        <p class="yt-stat-label">LongForm Daily Limit</p>
        <p style="font-size:18px;font-weight:700;">{
            lim.cadence_longform_per_day
            if lim and lim.cadence_longform_per_day is not None
            else "Unset"
        }</p>
      </div>
      <div>
        <p class="yt-stat-label">Daily Budget Limit</p>
        <p style="font-size:16px;font-weight:600;">\
{html.escape(lim.budget_daily_limit or "Unset")} \
{html.escape(lim.budget_currency or "")}</p>
      </div>
      <div>
        <p class="yt-stat-label">Monthly Budget Limit</p>
        <p style="font-size:16px;font-weight:600;">\
{html.escape(lim.budget_monthly_limit or "Unset")} \
{html.escape(lim.budget_currency or "")}</p>
      </div>
      <div style="grid-column:span 2;">
        <p class="yt-stat-label">Cadence Time Zone</p>
        <p style="font-weight:500;">\
{html.escape(lim.cadence_time_zone if lim else "UTC")}</p>
      </div>
    </div>
  </section>

  <!-- Strategy Profile Card -->
  <section class="yt-card yt-grid__span-12" data-testid="settings-strategy-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Content Blueprint (v{st.version if st else 1})</p>
        <h2 class="yt-card__title">Strategy Profile</h2>
      </div>
      <span class="yt-pill \
yt-pill--{"success" if st and st.is_complete else "warning"}">\
{"Complete" if st and st.is_complete else "Incomplete"}</span>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4, \
1fr);gap:16px;margin-top:12px;">
      <div>
        <p class="yt-stat-label">Market</p>
        <p style="font-weight:600;">\
{html.escape(st.market if st and st.market else "Unset")}</p>
      </div>
      <div>
        <p class="yt-stat-label">Languages</p>
        <p style="font-weight:500;">\
{html.escape(st.primary_language if st and st.primary_language else "Unset")} \
{f"(+{len(st.secondary_languages)})" if st and st.secondary_languages else ""}</p>
      </div>
      <div>
        <p class="yt-stat-label">Niche</p>
        <p style="font-weight:500;">\
{html.escape(st.niche_name if st and st.niche_name else "Unset")}</p>
      </div>
      <div>
        <p class="yt-stat-label">Audience Level</p>
        <p style="font-weight:500;">{html.escape(st.audience_level or "Mixed")}</p>
      </div>
    </div>
  </section>

  <!-- Providers & Infrastructure Card -->
  <section class="yt-card yt-grid__span-6" data-testid="settings-providers-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Registered Engine Backends</p>
        <h2 class="yt-card__title">Providers</h2>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px;">
      <div>
        <p class="yt-stat-label">Research Provider</p>
        <p style="font-weight:500;">{html.escape(pr.research_provider)}</p>
      </div>
      <div>
        <p class="yt-stat-label">Text / LLM Provider</p>
        <p style="font-weight:500;">{html.escape(pr.text_provider)}</p>
      </div>
      <div>
        <p class="yt-stat-label">Voice / TTS Provider</p>
        <p style="font-weight:500;">{html.escape(pr.voice_provider)} \
(${html.escape(pr.tts_price_per_1k)}/1k)</p>
      </div>
      <div>
        <p class="yt-stat-label">Video Renderer</p>
        <p style="font-weight:500;">{html.escape(pr.render_provider)} \
({pr.render_timeout_seconds}s timeout)</p>
      </div>
    </div>
  </section>

  <!-- Email & Approval Card -->
  <section class="yt-card yt-grid__span-6" data-testid="settings-email-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Human-in-the-Loop Gateway</p>
        <h2 class="yt-card__title">Approval &amp; Notification</h2>
      </div>
    </div>
    <div style="margin-top:8px;">
      <p class="yt-stat-label">Approval Expiry TTL</p>
      <p style="font-size:18px;font-weight:700;">{em.approval_ttl_hours} hours</p>
      <p class="yt-stat-label" style="margin-top:8px;">Notification Gateway</p>
      <p style="font-weight:500;">\
{html.escape(em.recipient_email or "Internal App")}</p>
    </div>
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="Settings — YouTube Command Center",
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
