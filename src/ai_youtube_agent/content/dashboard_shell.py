"""Dashboard Shell and shared iOS-style design system (Prompt Pack v8, prompt Q-191),
context X3 Command Center.

Provides the shared iOS-style Command Center design system tokens, stylesheet,
navigation destinations, live badge calculation, and XSS-safe HTML app shell:

- ``DashboardScreenId``: closed enum of Command Center screens (``home``, ``channel``,
  ``content``, ``video_detail``, ``analytics``, ``revenue``, ``comments``,
  ``ai_center``, ``approvals``, ``settings``, ``alerts``).
- ``ViewLoadState``: ``ready``, ``empty``, ``loading``, ``error``.
- ``BadgeTone``: ``neutral``, ``accent``, ``info``, ``success``, ``warning``,
  ``danger``.
- ``DesignSystemTokens`` and ``DEFAULT_IOS_DESIGN_TOKENS``: SF Pro typography,
  frosted-glass blur, rounded card radii, semantic palette, and spacing scale.
- ``NavDestination`` and ``DashboardShellState``: immutable navigation and channel
  switcher state with live badge counts for pending approvals, attention/moderation
  comments, and active AI jobs.
- ``DashboardShellService``: read-only service computing the shell state for a
  selected channel (or default channel) and rendering the iOS-style HTML shell
  via ``render_dashboard_shell_html`` and ``render_design_system_css``.
"""

import html
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from types import MappingProxyType
from typing import Any, Final

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.channel import Channel
from ai_youtube_agent.content.comment import CommentLabel, ReplyStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.community import CommentRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJobStatus


class DashboardScreenId(StrEnum):
    HOME = "home"
    CHANNEL = "channel"
    CONTENT = "content"
    VIDEO_DETAIL = "video_detail"
    ANALYTICS = "analytics"
    REVENUE = "revenue"
    COMMENTS = "comments"
    AI_CENTER = "ai_center"
    APPROVALS = "approvals"
    SETTINGS = "settings"
    ALERTS = "alerts"


class ViewLoadState(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    LOADING = "loading"
    ERROR = "error"


class BadgeTone(StrEnum):
    NEUTRAL = "neutral"
    ACCENT = "accent"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class DashboardErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.dashboard_channel_not_found"
    CONTENT_NOT_FOUND = "domain.dashboard_content_not_found"
    INVALID_PARAMETER = "domain.dashboard_invalid_parameter"


USER_MESSAGES: Final[dict[DashboardErrorCode, str]] = {
    DashboardErrorCode.CHANNEL_NOT_FOUND: "The requested channel was not found.",
    DashboardErrorCode.CONTENT_NOT_FOUND: "The requested content item was not found.",
    DashboardErrorCode.INVALID_PARAMETER: (
        "One or more Command Center filter parameters are invalid."
    ),
}

_STATUS: Final[dict[DashboardErrorCode, HTTPStatus]] = {
    DashboardErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    DashboardErrorCode.CONTENT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    DashboardErrorCode.INVALID_PARAMETER: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class DashboardError(DomainError):
    default_code = DashboardErrorCode.INVALID_PARAMETER.value
    default_user_message = USER_MESSAGES[DashboardErrorCode.INVALID_PARAMETER]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: DashboardErrorCode, detail: str) -> None:
        if not isinstance(code, DashboardErrorCode):
            raise TypeError("code must be a DashboardErrorCode")
        self.dashboard_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class DesignSystemTokens:
    """Shared iOS-style Command Center design system tokens (#191)."""

    theme_name: str
    font_family_sans: str
    font_family_mono: str
    colors: Mapping[str, str]
    radii_px: Mapping[str, int]
    spacing_px: Mapping[str, int]
    blur_px: int

    def __post_init__(self) -> None:
        if not self.theme_name or not self.theme_name.strip():
            raise ValueError("theme_name must not be empty")
        if not self.font_family_sans or not self.font_family_sans.strip():
            raise ValueError("font_family_sans must not be empty")
        if not self.font_family_mono or not self.font_family_mono.strip():
            raise ValueError("font_family_mono must not be empty")
        if (
            isinstance(self.blur_px, bool)
            or not isinstance(self.blur_px, int)
            or self.blur_px < 0
        ):
            raise ValueError("blur_px must be a non-negative integer")
        if not self.colors:
            raise ValueError("colors must not be empty")
        if not self.radii_px:
            raise ValueError("radii_px must not be empty")
        if not self.spacing_px:
            raise ValueError("spacing_px must not be empty")
        object.__setattr__(self, "colors", MappingProxyType(dict(self.colors)))
        object.__setattr__(self, "radii_px", MappingProxyType(dict(self.radii_px)))
        object.__setattr__(self, "spacing_px", MappingProxyType(dict(self.spacing_px)))

    def to_css_variables(self) -> str:
        lines = [
            f"--cc-font-sans: {self.font_family_sans};",
            f"--cc-font-mono: {self.font_family_mono};",
            f"--cc-blur: {self.blur_px}px;",
        ]
        for key, color in self.colors.items():
            css_key = key.replace("_", "-")
            lines.append(f"--cc-color-{css_key}: {color};")
        for key, radius in self.radii_px.items():
            css_key = key.replace("_", "-")
            lines.append(f"--cc-radius-{css_key}: {radius}px;")
        for key, space in self.spacing_px.items():
            css_key = key.replace("_", "-")
            lines.append(f"--cc-space-{css_key}: {space}px;")
        return "\n  ".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "theme_name": self.theme_name,
            "font_family_sans": self.font_family_sans,
            "font_family_mono": self.font_family_mono,
            "colors": dict(self.colors),
            "radii_px": dict(self.radii_px),
            "spacing_px": dict(self.spacing_px),
            "blur_px": self.blur_px,
        }


DEFAULT_IOS_DESIGN_TOKENS: Final[DesignSystemTokens] = DesignSystemTokens(
    theme_name="ios_command_center",
    font_family_sans=(
        "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'SF Pro Display', "
        "system-ui, sans-serif"
    ),
    font_family_mono="ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
    colors={
        "background_primary": "#F2F2F7",
        "background_elevated": "#FFFFFF",
        "surface_glass": "rgba(255, 255, 255, 0.82)",
        "text_primary": "#1C1C1E",
        "text_secondary": "#6C6C70",
        "accent_blue": "#007AFF",
        "accent_indigo": "#5856D6",
        "semantic_green": "#34C759",
        "semantic_amber": "#FF9500",
        "semantic_red": "#FF3B30",
        "separator": "rgba(60, 60, 67, 0.16)",
    },
    radii_px={"sm": 10, "md": 14, "lg": 20, "pill": 999},
    spacing_px={"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32},
    blur_px=20,
)


@dataclass(frozen=True)
class NavDestination:
    """One navigation tab/sidebar item in the Command Center shell."""

    screen_id: DashboardScreenId
    label: str
    icon_name: str
    href: str
    primary_tab: bool
    badge_count: int = 0
    badge_tone: BadgeTone = BadgeTone.NEUTRAL
    active: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.screen_id, DashboardScreenId):
            raise TypeError("screen_id must be a DashboardScreenId")
        if not isinstance(self.badge_tone, BadgeTone):
            raise TypeError("badge_tone must be a BadgeTone")
        if not self.label or not self.label.strip():
            raise ValueError("label must not be empty")
        if not self.icon_name or not self.icon_name.strip():
            raise ValueError("icon_name must not be empty")
        if not self.href or not self.href.strip():
            raise ValueError("href must not be empty")
        if (
            isinstance(self.badge_count, bool)
            or not isinstance(self.badge_count, int)
            or self.badge_count < 0
        ):
            raise ValueError("badge_count must be a non-negative integer")
        if not isinstance(self.primary_tab, bool) or not isinstance(self.active, bool):
            raise TypeError("primary_tab and active must be booleans")

    def as_dict(self) -> dict[str, Any]:
        return {
            "screen_id": self.screen_id.value,
            "label": self.label,
            "icon_name": self.icon_name,
            "href": self.href,
            "primary_tab": self.primary_tab,
            "badge_count": self.badge_count,
            "badge_tone": self.badge_tone.value,
            "active": self.active,
        }


@dataclass(frozen=True)
class ChannelSwitcherItem:
    channel_id: str
    title: str
    youtube_channel_id: str
    handle: str | None
    status: str
    selected: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "title": self.title,
            "youtube_channel_id": self.youtube_channel_id,
            "handle": self.handle,
            "status": self.status,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class DashboardShellState:
    app_title: str
    active_screen: DashboardScreenId
    active_channel_id: str | None
    active_channel_title: str | None
    channels: tuple[ChannelSwitcherItem, ...]
    navigation: tuple[NavDestination, ...]
    pending_approvals_badge: int
    attention_comments_badge: int
    active_jobs_badge: int
    load_state: ViewLoadState
    design_system: DesignSystemTokens

    @property
    def active_channel(self) -> ChannelSwitcherItem | None:
        for channel in self.channels:
            if channel.selected:
                return channel
        return None

    @property
    def primary_tabs(self) -> tuple[NavDestination, ...]:
        return tuple(dest for dest in self.navigation if dest.primary_tab)

    @property
    def secondary_destinations(self) -> tuple[NavDestination, ...]:
        return tuple(dest for dest in self.navigation if not dest.primary_tab)

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_title": self.app_title,
            "active_screen": self.active_screen.value,
            "active_channel_id": self.active_channel_id,
            "active_channel_title": self.active_channel_title,
            "channels": [channel.as_dict() for channel in self.channels],
            "navigation": [dest.as_dict() for dest in self.navigation],
            "primary_tabs": [dest.as_dict() for dest in self.primary_tabs],
            "secondary_destinations": [
                dest.as_dict() for dest in self.secondary_destinations
            ],
            "pending_approvals_badge": self.pending_approvals_badge,
            "attention_comments_badge": self.attention_comments_badge,
            "active_jobs_badge": self.active_jobs_badge,
            "load_state": self.load_state.value,
            "design_system": self.design_system.as_dict(),
        }


_NAVIGATION_SPEC: Final[tuple[tuple[DashboardScreenId, str, str, str, bool], ...]] = (
    (DashboardScreenId.HOME, "Home", "house.fill", "/dashboard/home", True),
    (
        DashboardScreenId.CHANNEL,
        "Channel",
        "person.crop.rectangle",
        "/dashboard/channel",
        False,
    ),
    (DashboardScreenId.CONTENT, "Content", "film.stack", "/dashboard/content", True),
    (
        DashboardScreenId.APPROVALS,
        "Approvals",
        "checkmark.seal.fill",
        "/dashboard/approvals",
        True,
    ),
    (
        DashboardScreenId.ANALYTICS,
        "Analytics",
        "chart.bar.xaxis",
        "/dashboard/analytics",
        True,
    ),
    (
        DashboardScreenId.REVENUE,
        "Economics",
        "dollarsign.circle.fill",
        "/dashboard/revenue",
        False,
    ),
    (
        DashboardScreenId.COMMENTS,
        "Comments",
        "bubble.left.and.bubble.right.fill",
        "/dashboard/comments",
        False,
    ),
    (
        DashboardScreenId.AI_CENTER,
        "AI Center",
        "cpu.fill",
        "/dashboard/ai-center",
        True,
    ),
    (DashboardScreenId.ALERTS, "Alerts", "bell.badge.fill", "/dashboard/alerts", False),
    (
        DashboardScreenId.SETTINGS,
        "Settings",
        "gearshape.fill",
        "/dashboard/settings",
        False,
    ),
)

_ACTIVE_JOB_STATUSES: Final[frozenset[AIJobStatus]] = frozenset(
    {AIJobStatus.QUEUED, AIJobStatus.RUNNING, AIJobStatus.WAITING}
)


def build_navigation(
    active_screen: DashboardScreenId,
    *,
    channel_id: str | None = None,
    pending_approvals: int = 0,
    attention_comments: int = 0,
    active_jobs: int = 0,
    alerts_count: int = 0,
) -> tuple[NavDestination, ...]:
    """Build the ordered navigation destinations for the Command Center shell."""
    if not isinstance(active_screen, DashboardScreenId):
        raise TypeError("active_screen must be a DashboardScreenId")
    suffix = f"?channel_id={html.escape(channel_id, quote=True)}" if channel_id else ""
    destinations: list[NavDestination] = []
    for screen_id, label, icon_name, base_href, is_primary in _NAVIGATION_SPEC:
        badge_count = 0
        tone = BadgeTone.NEUTRAL
        if screen_id is DashboardScreenId.APPROVALS and pending_approvals > 0:
            badge_count = pending_approvals
            tone = BadgeTone.WARNING
        elif screen_id is DashboardScreenId.COMMENTS and attention_comments > 0:
            badge_count = attention_comments
            tone = BadgeTone.ACCENT
        elif screen_id is DashboardScreenId.AI_CENTER and active_jobs > 0:
            badge_count = active_jobs
            tone = BadgeTone.ACCENT
        elif screen_id is DashboardScreenId.ALERTS and alerts_count > 0:
            badge_count = alerts_count
            tone = BadgeTone.DANGER
        is_active = screen_id is active_screen or (
            active_screen is DashboardScreenId.VIDEO_DETAIL
            and screen_id is DashboardScreenId.CONTENT
        )
        destinations.append(
            NavDestination(
                screen_id=screen_id,
                label=label,
                icon_name=icon_name,
                href=f"{base_href}{suffix}",
                primary_tab=is_primary,
                badge_count=badge_count,
                badge_tone=tone,
                active=is_active,
            )
        )
    return tuple(destinations)


def render_design_system_css(
    tokens: DesignSystemTokens = DEFAULT_IOS_DESIGN_TOKENS,
) -> str:
    """Render the shared iOS-style Command Center CSS stylesheet."""
    css_vars = tokens.to_css_variables()
    return f""":root {{
  {css_vars}
}}

* {{
  box-sizing: border-box;
}}

body.cc-body {{
  margin: 0;
  font-family: var(--cc-font-sans);
  background: var(--cc-color-background-primary);
  color: var(--cc-color-text-primary);
  -webkit-font-smoothing: antialiased;
}}

.cc-shell {{
  min-height: 100vh;
  display: grid;
  grid-template-columns: 260px 1fr;
  grid-template-rows: auto 1fr auto;
}}

.cc-topbar {{
  grid-column: 1 / -1;
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--cc-space-md) var(--cc-space-xl);
  background: var(--cc-color-surface-glass);
  backdrop-filter: blur(var(--cc-blur));
  border-bottom: 1px solid var(--cc-color-separator);
}}

.cc-sidebar {{
  padding: var(--cc-space-lg);
  background: var(--cc-color-surface-glass);
  backdrop-filter: blur(var(--cc-blur));
  border-right: 1px solid var(--cc-color-separator);
}}

.cc-nav-list {{
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--cc-space-xs);
}}

.cc-nav-link {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-radius: var(--cc-radius-sm);
  text-decoration: none;
  color: var(--cc-color-text-primary);
  font-weight: 500;
}}

.cc-nav-link[aria-current="page"] {{
  background: var(--cc-color-accent-blue);
  color: #FFFFFF;
}}

.cc-main {{
  padding: var(--cc-space-xl);
  display: flex;
  flex-direction: column;
  gap: var(--cc-space-lg);
}}

.cc-card {{
  background: var(--cc-color-background-elevated);
  border-radius: var(--cc-radius-md);
  padding: var(--cc-space-lg);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
  border: 1px solid var(--cc-color-separator);
}}

.cc-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--cc-space-md);
}}

.cc-badge {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px 8px;
  border-radius: var(--cc-radius-pill);
  font-size: 12px;
  font-weight: 600;
}}

.cc-badge-neutral {{ background: rgba(108, 108, 112, 0.16); color: \
var(--cc-color-text-secondary); }}
.cc-badge-accent {{ background: rgba(0, 122, 255, 0.16); color: \
var(--cc-color-accent-blue); }}
.cc-badge-info {{ background: rgba(0, 122, 255, 0.16); color: \
var(--cc-color-accent-blue); }}
.cc-badge-success {{ background: rgba(52, 199, 89, 0.16); color: \
var(--cc-color-semantic-green); }}
.cc-badge-warning {{ background: rgba(255, 149, 0, 0.18); color: \
var(--cc-color-semantic-amber); }}
.cc-badge-danger {{ background: rgba(255, 59, 48, 0.18); color: \
var(--cc-color-semantic-red); }}

.cc-tabbar {{
  grid-column: 1 / -1;
  position: sticky;
  bottom: 0;
  display: flex;
  justify-content: space-around;
  padding: var(--cc-space-sm) var(--cc-space-md);
  background: var(--cc-color-surface-glass);
  backdrop-filter: blur(var(--cc-blur));
  border-top: 1px solid var(--cc-color-separator);
}}
"""


def render_dashboard_shell_html(
    shell: DashboardShellState,
    *,
    body_html: str = "",
    page_title: str | None = None,
) -> str:
    """Render an XSS-safe iOS-style Command Center HTML shell."""
    title_text = html.escape(page_title or shell.app_title)
    app_title = html.escape(shell.app_title)
    channel_label = html.escape(shell.active_channel_title or "No Channel Selected")
    channel_options = "".join(
        f'<option value="{html.escape(ch.channel_id, quote=True)}"'
        f"{' selected' if ch.selected else ''}>"
        f"{html.escape(ch.title)} ({html.escape(ch.status)})</option>"
        for ch in shell.channels
    )
    nav_items = "".join(_render_nav_item(dest) for dest in shell.navigation)
    tab_items = "".join(_render_tab_item(dest) for dest in shell.primary_tabs)
    content_html = body_html or _render_default_shell_placeholder(shell)
    css = render_design_system_css(shell.design_system)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title_text}</title>
  <style>{css}</style>
</head>
<body class="cc-body" data-theme="{
        html.escape(shell.design_system.theme_name, quote=True)
    }" data-load-state="{html.escape(shell.load_state.value, quote=True)}">
  <div class="cc-shell">
    <header class="cc-topbar" role="banner">
      <div class="cc-brand">
        <strong>{app_title}</strong>
        <span class="cc-badge cc-badge-accent" \
data-active-screen="{html.escape(shell.active_screen.value, quote=True)}">{
        html.escape(shell.active_screen.value)
    }</span>
      </div>
      <div class="cc-channel-switcher" aria-label="Active Channel">
        <span class="cc-channel-current">{channel_label}</span>
        <select aria-label="Channel Selector">{channel_options}</select>
      </div>
    </header>
    <aside class="cc-sidebar" aria-label="Command Center Navigation">
      <ul class="cc-nav-list">
        {nav_items}
      </ul>
    </aside>
    <main id="command-center-main" class="cc-main" role="main">
      {content_html}
    </main>
    <nav class="cc-tabbar" aria-label="Primary iOS Tab Bar">
      {tab_items}
    </nav>
  </div>
</body>
</html>"""


def _render_nav_item(dest: NavDestination) -> str:
    current = ' aria-current="page"' if dest.active else ""
    badge = (
        f'<span class="cc-badge '
        f'cc-badge-{html.escape(dest.badge_tone.value, quote=True)}">'
        f"{dest.badge_count}</span>"
        if dest.badge_count > 0
        else ""
    )
    return (
        f'<li><a class="cc-nav-link" href="{html.escape(dest.href, quote=True)}"'
        f' data-screen="{html.escape(dest.screen_id.value, quote=True)}"'
        f' data-icon="{html.escape(dest.icon_name, quote=True)}"{current}>'
        f"<span>{html.escape(dest.label)}</span>{badge}</a></li>"
    )


def _render_tab_item(dest: NavDestination) -> str:
    current = ' aria-current="page"' if dest.active else ""
    badge = f" ({dest.badge_count})" if dest.badge_count > 0 else ""
    return (
        f'<a class="cc-nav-link" href="{html.escape(dest.href, quote=True)}"'
        f' data-tab="{html.escape(dest.screen_id.value, quote=True)}"{current}>'
        f"{html.escape(dest.label)}{badge}</a>"
    )


def _render_default_shell_placeholder(shell: DashboardShellState) -> str:
    if shell.load_state is ViewLoadState.EMPTY:
        return (
            '<section class="cc-card" data-state="empty">'
            "<h2>No channels configured</h2>"
            "<p>Create a channel in Settings to begin using the Command Center.</p>"
            "</section>"
        )
    return (
        '<section class="cc-card" data-state="ready">'
        f"<h2>{html.escape(shell.active_channel_title or shell.app_title)}</h2>"
        f"<p>Pending approvals: {shell.pending_approvals_badge} | "
        f"Comments needing attention: {shell.attention_comments_badge} | "
        f"Active AI jobs: {shell.active_jobs_badge}</p>"
        "</section>"
    )


class DashboardShellService:
    """Builds the shared iOS-style Command Center shell and navigation state (#191)."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Any = None,
        app_title: str = "YouTube Command Center",
        design_tokens: DesignSystemTokens = DEFAULT_IOS_DESIGN_TOKENS,
    ) -> None:
        self._database = database
        self._clock = clock
        self._app_title = app_title
        self._design_tokens = design_tokens

    def build_shell(
        self,
        active_screen: DashboardScreenId = DashboardScreenId.HOME,
        *,
        channel_id: str | None = None,
        alerts_count: int = 0,
    ) -> DashboardShellState:
        if not isinstance(active_screen, DashboardScreenId):
            raise TypeError("active_screen must be a DashboardScreenId")
        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
            if selected_channel is None:
                switcher: tuple[ChannelSwitcherItem, ...] = ()
                pending_approvals = 0
                attention_comments = 0
                active_jobs = sum(
                    1
                    for job in AIJobRepository(connection).list_all()
                    if job.status in _ACTIVE_JOB_STATUSES
                )
                load_state = ViewLoadState.EMPTY
            else:
                switcher = tuple(
                    ChannelSwitcherItem(
                        channel_id=ch.id,
                        title=ch.title,
                        youtube_channel_id=ch.youtube.channel_id,
                        handle=ch.youtube.handle,
                        status=ch.status.value,
                        selected=(ch.id == selected_channel.id),
                    )
                    for ch in channels
                )
                pending_approvals = _count_pending_approvals(
                    connection, selected_channel.id
                )
                attention_comments = _count_attention_comments(
                    connection, selected_channel.id
                )
                active_jobs = _count_active_jobs(connection, selected_channel.id)
                load_state = ViewLoadState.READY
        finally:
            connection.close()

        selected_id = selected_channel.id if selected_channel else None
        selected_title = selected_channel.title if selected_channel else None
        navigation = build_navigation(
            active_screen,
            channel_id=selected_id,
            pending_approvals=pending_approvals,
            attention_comments=attention_comments,
            active_jobs=active_jobs,
            alerts_count=alerts_count,
        )
        return DashboardShellState(
            app_title=self._app_title,
            active_screen=active_screen,
            active_channel_id=selected_id,
            active_channel_title=selected_title,
            channels=switcher,
            navigation=navigation,
            pending_approvals_badge=pending_approvals,
            attention_comments_badge=attention_comments,
            active_jobs_badge=active_jobs,
            load_state=load_state,
            design_system=self._design_tokens,
        )

    def render_html(
        self,
        active_screen: DashboardScreenId = DashboardScreenId.HOME,
        *,
        channel_id: str | None = None,
        body_html: str = "",
        page_title: str | None = None,
        alerts_count: int = 0,
    ) -> str:
        shell = self.build_shell(
            active_screen=active_screen,
            channel_id=channel_id,
            alerts_count=alerts_count,
        )
        return render_dashboard_shell_html(
            shell, body_html=body_html, page_title=page_title
        )


def _select_channel(channels: list[Channel], channel_id: str | None) -> Channel | None:
    if channel_id is not None:
        for ch in channels:
            if ch.id == channel_id:
                return ch
        raise DashboardError(
            DashboardErrorCode.CHANNEL_NOT_FOUND,
            f"channel {channel_id} was not found",
        )
    return channels[0] if channels else None


def _count_pending_approvals(connection: Any, channel_id: str) -> int:
    item_repo = ContentItemRepository(connection)
    approval_repo = ApprovalRequestRepository(connection)
    count = 0
    for item in item_repo.list_by_channel(channel_id):
        requests = approval_repo.list_by_content_item(item.id)
        if requests and requests[-1].status is ApprovalStatus.PENDING:
            count += 1
    return count


def _count_attention_comments(connection: Any, channel_id: str) -> int:
    comment_repo = CommentRepository(connection)
    count = 0
    for comment in comment_repo.list_by_channel(channel_id):
        classifications = comment_repo.list_classifications(comment.id)
        latest_label = classifications[-1].label if classifications else None
        drafts = comment_repo.list_reply_drafts(comment.id)
        has_pending_draft = any(d.status is ReplyStatus.DRAFT for d in drafts)
        if (
            latest_label in (CommentLabel.NEEDS_ATTENTION, CommentLabel.MODERATION)
            or has_pending_draft
        ):
            count += 1
    return count


def _count_active_jobs(connection: Any, channel_id: str) -> int:
    item_ids = {
        item.id
        for item in ContentItemRepository(connection).list_by_channel(channel_id)
    }
    count = 0
    for job in AIJobRepository(connection).list_all():
        if job.status in _ACTIVE_JOB_STATUSES and (
            job.content_item_id is None or job.content_item_id in item_ids
        ):
            count += 1
    return count
