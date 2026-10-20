"""Home Screen view and service (Prompt Pack v8, prompt Q-192), context X3 Command Center.

Builds the Command Center Home Screen view model and iOS-style HTML rendering,
aggregating:
- Channel health (channel status, strategy completeness, and system/provider health).
- AI status (active session, running/queued/waiting/failed AI jobs, and feature flags).
- Production counts (in-progress and published Shorts/LongForm items, plus today's
  production starts and publishes vs configured cadence limits).
- Pending approvals queue preview (bound to exact video artifact version).
- Actionable alert summaries (health degradation, failed jobs/items, pending approvals,
  incomplete strategy, and budget threshold alerts from ``BudgetGuard``).
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ai_youtube_agent import __version__
from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.budget_guard import BudgetGuard, BudgetGuardError
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.daily_limit_gate import day_window, strategy_time_zone
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.db.repositories.usage import DailyUsageRepository
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.health import HealthRegistry, HealthStatus
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus, SessionStatus

Clock = Callable[[], datetime]

_IN_PROGRESS_STATUSES: Final[frozenset[ContentStatus]] = frozenset(
    {
        ContentStatus.DRAFT,
        ContentStatus.GENERATING,
        ContentStatus.TESTING,
        ContentStatus.PREVIEW_READY,
        ContentStatus.AWAITING_APPROVAL,
        ContentStatus.APPROVED,
        ContentStatus.PUBLISHING,
    }
)


@dataclass(frozen=True)
class ChannelHealthCard:
    channel_id: str
    channel_title: str
    youtube_channel_id: str
    channel_status: str
    strategy_configured: bool
    strategy_complete: bool
    missing_settings: tuple[str, ...]
    system_health_status: str
    degraded_checks: tuple[str, ...]
    tone: BadgeTone

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "youtube_channel_id": self.youtube_channel_id,
            "channel_status": self.channel_status,
            "strategy_configured": self.strategy_configured,
            "strategy_complete": self.strategy_complete,
            "missing_settings": list(self.missing_settings),
            "system_health_status": self.system_health_status,
            "degraded_checks": list(self.degraded_checks),
            "tone": self.tone.value,
        }


@dataclass(frozen=True)
class AIStatusSummary:
    state: str
    active_session_id: str | None
    running_jobs: int
    queued_jobs: int
    waiting_jobs: int
    failed_jobs: int
    shorts_enabled: bool
    longform_enabled: bool
    publish_enabled: bool
    auto_reply_enabled: bool
    tone: BadgeTone

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "active_session_id": self.active_session_id,
            "running_jobs": self.running_jobs,
            "queued_jobs": self.queued_jobs,
            "waiting_jobs": self.waiting_jobs,
            "failed_jobs": self.failed_jobs,
            "shorts_enabled": self.shorts_enabled,
            "longform_enabled": self.longform_enabled,
            "publish_enabled": self.publish_enabled,
            "auto_reply_enabled": self.auto_reply_enabled,
            "tone": self.tone.value,
        }


@dataclass(frozen=True)
class ProductionSummary:
    shorts_in_progress: int
    longform_in_progress: int
    total_in_progress: int
    shorts_published_total: int
    longform_published_total: int
    failed_items_count: int
    today_shorts_started: int
    today_shorts_published: int
    today_shorts_limit: int | None
    today_longform_started: int
    today_longform_published: int
    today_longform_limit: int | None
    time_zone: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "shorts_in_progress": self.shorts_in_progress,
            "longform_in_progress": self.longform_in_progress,
            "total_in_progress": self.total_in_progress,
            "shorts_published_total": self.shorts_published_total,
            "longform_published_total": self.longform_published_total,
            "failed_items_count": self.failed_items_count,
            "today_shorts_started": self.today_shorts_started,
            "today_shorts_published": self.today_shorts_published,
            "today_shorts_limit": self.today_shorts_limit,
            "today_longform_started": self.today_longform_started,
            "today_longform_published": self.today_longform_published,
            "today_longform_limit": self.today_longform_limit,
            "time_zone": self.time_zone,
        }


@dataclass(frozen=True)
class PendingApprovalSummaryItem:
    approval_request_id: str
    content_item_id: str
    title: str
    content_type: str
    video_artifact_id: str | None
    video_version: int | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_request_id": self.approval_request_id,
            "content_item_id": self.content_item_id,
            "title": self.title,
            "content_type": self.content_type,
            "video_artifact_id": self.video_artifact_id,
            "video_version": self.video_version,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class HomeAlertPreview:
    alert_id: str
    category: str
    severity: BadgeTone
    title: str
    message: str
    action_href: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "category": self.category,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "action_href": self.action_href,
        }


@dataclass(frozen=True)
class HomeScreenView:
    channel_id: str | None
    load_state: ViewLoadState
    generated_at: datetime
    channel_health: ChannelHealthCard | None
    ai_status: AIStatusSummary
    production: ProductionSummary | None
    pending_approvals: tuple[PendingApprovalSummaryItem, ...]
    pending_approvals_count: int
    alerts: tuple[HomeAlertPreview, ...]
    shell: DashboardShellState

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "load_state": self.load_state.value,
            "generated_at": self.generated_at.isoformat(),
            "channel_health": (
                self.channel_health.as_dict() if self.channel_health else None
            ),
            "ai_status": self.ai_status.as_dict(),
            "production": self.production.as_dict() if self.production else None,
            "pending_approvals": [
                item.as_dict() for item in self.pending_approvals
            ],
            "pending_approvals_count": self.pending_approvals_count,
            "alerts": [alert.as_dict() for alert in self.alerts],
            "shell": self.shell.as_dict(),
        }


class HomeScreenService:
    """Builds the Command Center Home Screen view model and HTML (#192)."""

    def __init__(
        self,
        database: Database,
        shell_service: DashboardShellService,
        health_registry: HealthRegistry,
        flags: FeatureFlags,
        budget_guard: BudgetGuard | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._shell = shell_service
        self._health = health_registry
        self._flags = flags
        self._budget_guard = budget_guard
        self._clock = clock

    def build_view(self, *, channel_id: str | None = None) -> HomeScreenView:
        now = self._now()
        health_report = self._health.run(__version__)
        degraded_checks = tuple(
            check.name
            for check in health_report.checks
            if check.status is not HealthStatus.OK
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected = _select_channel(channels, channel_id)
            all_sessions = SessionRepository(connection).list_all()
            all_jobs = AIJobRepository(connection).list_all()

            if selected is None:
                ai_status = _build_ai_status(all_sessions, all_jobs, None, self._flags)
                alerts = _build_system_alerts(health_report.status, degraded_checks)
                shell = self._shell.build_shell(
                    active_screen=DashboardScreenId.HOME,
                    channel_id=None,
                    alerts_count=len(alerts),
                )
                return HomeScreenView(
                    channel_id=None,
                    load_state=ViewLoadState.EMPTY,
                    generated_at=now,
                    channel_health=None,
                    ai_status=ai_status,
                    production=None,
                    pending_approvals=(),
                    pending_approvals_count=0,
                    alerts=alerts,
                    shell=shell,
                )

            strategy = StrategyProfileRepository(connection).get_by_channel(selected.id)
            items = ContentItemRepository(connection).list_by_channel(selected.id)
            item_ids = {item.id for item in items}
            ai_status = _build_ai_status(all_sessions, all_jobs, item_ids, self._flags)

            missing_settings = strategy.missing_settings if strategy else ()
            strategy_configured = strategy is not None
            strategy_complete = bool(strategy and strategy.is_complete)
            health_tone = _channel_health_tone(
                selected.status,
                strategy_complete,
                health_report.status,
            )
            channel_health = ChannelHealthCard(
                channel_id=selected.id,
                channel_title=selected.title,
                youtube_channel_id=selected.youtube.channel_id,
                channel_status=selected.status.value,
                strategy_configured=strategy_configured,
                strategy_complete=strategy_complete,
                missing_settings=missing_settings,
                system_health_status=health_report.status.value,
                degraded_checks=degraded_checks,
                tone=health_tone,
            )

            tz_name = strategy_time_zone(strategy)
            day_start, day_end = day_window(now, tz_name)
            usage = DailyUsageRepository(connection)
            production = _build_production_summary(
                selected.id,
                items,
                strategy.cadence if strategy else None,
                tz_name,
                usage,
                day_start,
                day_end,
            )
            approvals = _collect_pending_approvals(
                ApprovalRequestRepository(connection), items
            )
        finally:
            connection.close()

        alerts = self._collect_alerts(
            selected.id,
            channel_health,
            ai_status,
            production,
            approvals,
            now,
        )
        shell = self._shell.build_shell(
            active_screen=DashboardScreenId.HOME,
            channel_id=selected.id,
            alerts_count=len(alerts),
        )
        return HomeScreenView(
            channel_id=selected.id,
            load_state=ViewLoadState.READY,
            generated_at=now,
            channel_health=channel_health,
            ai_status=ai_status,
            production=production,
            pending_approvals=approvals,
            pending_approvals_count=len(approvals),
            alerts=alerts,
            shell=shell,
        )

    def render_html(self, *, channel_id: str | None = None) -> str:
        view = self.build_view(channel_id=channel_id)
        if view.load_state is ViewLoadState.EMPTY or view.channel_health is None:
            return render_dashboard_shell_html(
                view.shell, page_title="Home — YouTube Command Center"
            )
        body = _render_home_body_html(view)
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title=f"Home — {view.channel_health.channel_title}",
        )

    def _collect_alerts(
        self,
        channel_id: str,
        channel_health: ChannelHealthCard,
        ai_status: AIStatusSummary,
        production: ProductionSummary,
        approvals: tuple[PendingApprovalSummaryItem, ...],
        now: datetime,
    ) -> tuple[HomeAlertPreview, ...]:
        alerts: list[HomeAlertPreview] = list(
            _build_system_alerts(
                HealthStatus(channel_health.system_health_status),
                channel_health.degraded_checks,
            )
        )
        if not channel_health.strategy_complete:
            missing_desc = (
                ", ".join(channel_health.missing_settings)
                if channel_health.strategy_configured
                else "all settings"
            )
            alerts.append(
                HomeAlertPreview(
                    alert_id=f"strategy:{channel_id}",
                    category="strategy",
                    severity=BadgeTone.WARNING,
                    title="Strategy Incomplete",
                    message=f"Configure missing strategy settings: {missing_desc}.",
                    action_href=f"/dashboard/settings?channel_id={channel_id}",
                )
            )
        if approvals:
            alerts.append(
                HomeAlertPreview(
                    alert_id=f"approvals:{channel_id}",
                    category="approval",
                    severity=BadgeTone.WARNING,
                    title="Pending Human Approvals",
                    message=f"{len(approvals)} content item(s) awaiting human approval.",
                    action_href=f"/dashboard/approvals?channel_id={channel_id}",
                )
            )
        if ai_status.failed_jobs > 0 or production.failed_items_count > 0:
            alerts.append(
                HomeAlertPreview(
                    alert_id=f"failures:{channel_id}",
                    category="failure",
                    severity=BadgeTone.DANGER,
                    title="Pipeline / Job Failures",
                    message=(
                        f"{ai_status.failed_jobs} failed AI job(s) and "
                        f"{production.failed_items_count} failed content item(s)."
                    ),
                    action_href=f"/dashboard/ai-center?channel_id={channel_id}",
                )
            )
        if self._budget_guard is not None:
            try:
                guard_status = self._budget_guard.evaluate(channel_id, at=now)
                for b_alert in guard_status.alerts:
                    tone = (
                        BadgeTone.DANGER
                        if b_alert.threshold_percent >= 100 or guard_status.blocked
                        else BadgeTone.WARNING
                    )
                    alerts.append(
                        HomeAlertPreview(
                            alert_id=(
                                f"budget:{channel_id}:{b_alert.window.value}:"
                                f"{b_alert.threshold_percent}"
                            ),
                            category="budget",
                            severity=tone,
                            title=f"{b_alert.window.value.title()} Budget Alert ({b_alert.threshold_percent}%)",
                            message=b_alert.message,
                            action_href=f"/dashboard/revenue?channel_id={channel_id}",
                        )
                    )
            except BudgetGuardError:
                pass
        return tuple(alerts)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _channel_health_tone(
    channel_status: ChannelStatus,
    strategy_complete: bool,
    system_status: HealthStatus,
) -> BadgeTone:
    if system_status is HealthStatus.DOWN or channel_status in (
        ChannelStatus.DISCONNECTED,
        ChannelStatus.ARCHIVED,
    ):
        return BadgeTone.DANGER
    if (
        system_status is HealthStatus.DEGRADED
        or channel_status is not ChannelStatus.ACTIVE
        or not strategy_complete
    ):
        return BadgeTone.WARNING
    return BadgeTone.SUCCESS


def _build_ai_status(
    sessions: list[Any],
    jobs: list[AIJob],
    channel_item_ids: set[str] | None,
    flags: FeatureFlags,
) -> AIStatusSummary:
    active_sessions = [s for s in sessions if s.status is SessionStatus.ACTIVE]
    active_session_id = active_sessions[-1].id if active_sessions else None
    scoped_jobs = [
        job
        for job in jobs
        if channel_item_ids is None
        or job.content_item_id is None
        or job.content_item_id in channel_item_ids
    ]
    running = sum(1 for j in scoped_jobs if j.status is AIJobStatus.RUNNING)
    queued = sum(1 for j in scoped_jobs if j.status is AIJobStatus.QUEUED)
    waiting = sum(1 for j in scoped_jobs if j.status is AIJobStatus.WAITING)
    failed = sum(1 for j in scoped_jobs if j.status is AIJobStatus.FAILED)

    if running > 0 or queued > 0:
        state = "running"
        tone = BadgeTone.ACCENT
    elif waiting > 0:
        state = "waiting"
        tone = BadgeTone.WARNING
    elif failed > 0:
        state = "attention"
        tone = BadgeTone.DANGER
    else:
        state = "idle"
        tone = BadgeTone.SUCCESS

    return AIStatusSummary(
        state=state,
        active_session_id=active_session_id,
        running_jobs=running,
        queued_jobs=queued,
        waiting_jobs=waiting,
        failed_jobs=failed,
        shorts_enabled=flags.shorts_enabled,
        longform_enabled=flags.longform_enabled,
        publish_enabled=flags.publish_enabled,
        auto_reply_enabled=flags.auto_reply_enabled,
        tone=tone,
    )


def _build_production_summary(
    channel_id: str,
    items: list[ContentItem],
    cadence: Any,
    tz_name: str,
    usage: DailyUsageRepository,
    day_start: datetime,
    day_end: datetime,
) -> ProductionSummary:
    shorts_in_progress = sum(
        1
        for item in items
        if item.content_type is ContentType.SHORTS
        and item.status in _IN_PROGRESS_STATUSES
    )
    longform_in_progress = sum(
        1
        for item in items
        if item.content_type is ContentType.LONGFORM
        and item.status in _IN_PROGRESS_STATUSES
    )
    shorts_published = sum(
        1
        for item in items
        if item.content_type is ContentType.SHORTS
        and item.status is ContentStatus.PUBLISHED
    )
    longform_published = sum(
        1
        for item in items
        if item.content_type is ContentType.LONGFORM
        and item.status is ContentStatus.PUBLISHED
    )
    failed_items = sum(1 for item in items if item.status is ContentStatus.FAILED)

    today_shorts_started = usage.count_production_starts(
        channel_id, ContentType.SHORTS, day_start, day_end
    )
    today_shorts_published = usage.count_publishes(
        channel_id, ContentType.SHORTS, day_start, day_end
    )
    today_longform_started = usage.count_production_starts(
        channel_id, ContentType.LONGFORM, day_start, day_end
    )
    today_longform_published = usage.count_publishes(
        channel_id, ContentType.LONGFORM, day_start, day_end
    )

    return ProductionSummary(
        shorts_in_progress=shorts_in_progress,
        longform_in_progress=longform_in_progress,
        total_in_progress=shorts_in_progress + longform_in_progress,
        shorts_published_total=shorts_published,
        longform_published_total=longform_published,
        failed_items_count=failed_items,
        today_shorts_started=today_shorts_started,
        today_shorts_published=today_shorts_published,
        today_shorts_limit=cadence.shorts_per_day if cadence else None,
        today_longform_started=today_longform_started,
        today_longform_published=today_longform_published,
        today_longform_limit=cadence.longform_per_day if cadence else None,
        time_zone=tz_name,
    )


def _collect_pending_approvals(
    approval_repo: ApprovalRequestRepository, items: list[ContentItem]
) -> tuple[PendingApprovalSummaryItem, ...]:
    pending: list[PendingApprovalSummaryItem] = []
    for item in items:
        requests = approval_repo.list_by_content_item(item.id)
        if not requests or requests[-1].status is not ApprovalStatus.PENDING:
            continue
        newest = requests[-1]
        video_binding = next(
            (b for b in newest.artifacts if b.kind is ArtifactKind.VIDEO), None
        )
        pending.append(
            PendingApprovalSummaryItem(
                approval_request_id=newest.id,
                content_item_id=item.id,
                title=item.title,
                content_type=item.content_type.value,
                video_artifact_id=(
                    video_binding.artifact_id if video_binding else None
                ),
                video_version=video_binding.version if video_binding else None,
                created_at=newest.created_at,
            )
        )
    return tuple(pending)


def _build_system_alerts(
    health_status: HealthStatus, degraded_checks: tuple[str, ...]
) -> tuple[HomeAlertPreview, ...]:
    if health_status is HealthStatus.OK:
        return ()
    tone = (
        BadgeTone.DANGER
        if health_status is HealthStatus.DOWN
        else BadgeTone.WARNING
    )
    checks_str = ", ".join(degraded_checks) if degraded_checks else "system"
    return (
        HomeAlertPreview(
            alert_id="system:health",
            category="health",
            severity=tone,
            title=f"System Health: {health_status.value.upper()}",
            message=f"Affected checks: {checks_str}.",
            action_href="/health",
        ),
    )


def _render_home_body_html(view: HomeScreenView) -> str:
    health = view.channel_health
    prod = view.production
    ai = view.ai_status
    assert health is not None and prod is not None

    approvals_html = (
        "".join(
            f"<li><strong>{html.escape(item.title)}</strong> "
            f"({html.escape(item.content_type)}, v{item.video_version or '?'})</li>"
            for item in view.pending_approvals
        )
        or "<li>No pending approvals</li>"
    )
    alerts_html = (
        "".join(
            f'<li data-category="{html.escape(alert.category, quote=True)}">'
            f'<span class="cc-badge cc-badge-{html.escape(alert.severity.value, quote=True)}">'
            f"{html.escape(alert.category)}</span> "
            f"<strong>{html.escape(alert.title)}</strong>: "
            f"{html.escape(alert.message)}</li>"
            for alert in view.alerts
        )
        or "<li>No active alerts</li>"
    )

    return f"""<div class="cc-grid" data-screen="home">
  <section class="cc-card" data-widget="channel-health">
    <h3>Channel Health</h3>
    <p><strong>{html.escape(health.channel_title)}</strong> ({html.escape(health.channel_status)})</p>
    <p>System: <span class="cc-badge cc-badge-{html.escape(health.tone.value, quote=True)}">{html.escape(health.system_health_status)}</span></p>
    <p>Strategy Complete: {"Yes" if health.strategy_complete else "No"}</p>
  </section>
  <section class="cc-card" data-widget="ai-status">
    <h3>AI Status</h3>
    <p>State: <span class="cc-badge cc-badge-{html.escape(ai.tone.value, quote=True)}">{html.escape(ai.state)}</span></p>
    <p>Running: {ai.running_jobs} | Queued: {ai.queued_jobs} | Waiting: {ai.waiting_jobs} | Failed: {ai.failed_jobs}</p>
  </section>
  <section class="cc-card" data-widget="production-count">
    <h3>Production Count</h3>
    <p>In Progress: {prod.total_in_progress} (Shorts: {prod.shorts_in_progress}, LongForm: {prod.longform_in_progress})</p>
    <p>Published: Shorts {prod.shorts_published_total}, LongForm {prod.longform_published_total}</p>
    <p>Today Shorts: {prod.today_shorts_started}/{prod.today_shorts_limit if prod.today_shorts_limit is not None else "-"}</p>
  </section>
</div>
<section class="cc-card" data-widget="pending-approvals">
  <h3>Pending Approvals ({view.pending_approvals_count})</h3>
  <ul>{approvals_html}</ul>
</section>
<section class="cc-card" data-widget="alerts">
  <h3>Active Alerts ({len(view.alerts)})</h3>
  <ul>{alerts_html}</ul>
</section>"""

