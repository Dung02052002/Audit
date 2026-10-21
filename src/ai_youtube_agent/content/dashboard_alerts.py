"""Actionable Alerts Screen view and service (Prompt Pack v8, prompt Q-202), context X3 Command Center.

Builds the Command Center Alerts Screen view model and iOS-style HTML rendering,
implementing actionable alert cards across:
- **Failures**: Failed content items, failed AI jobs, and failed publishing jobs with
  direct links to inspect errors and retry.
- **Approvals**: Pending human approvals, stale approvals invalidated by artifact changes,
  and expiring approval requests with direct action links to the Approval Center.
- **Budget**: Daily and monthly budget threshold alerts (50%, 80%, 100%), spend overruns,
  and currency mismatches from ``BudgetGuard``.
- **Health**: Degraded or down application and provider health checks from ``HealthRegistry``
  with remediation guidance.
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.budget_guard import BudgetGuard
from ai_youtube_agent.content.channel import Channel
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.core.health import CheckResult, HealthRegistry, HealthStatus
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.pipeline.publish import PublishStatus

Clock = Callable[[], datetime]


class AlertCategory(StrEnum):
    FAILURE = "failure"
    APPROVAL = "approval"
    BUDGET = "budget"
    HEALTH = "health"
    STRATEGY = "strategy"


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


_SEVERITY_TONES: Final[dict[AlertSeverity, BadgeTone]] = {
    AlertSeverity.CRITICAL: BadgeTone.DANGER,
    AlertSeverity.WARNING: BadgeTone.WARNING,
    AlertSeverity.INFO: BadgeTone.INFO,
}


@dataclass(frozen=True)
class ActionableAlertCard:
    alert_id: str
    category: AlertCategory
    severity: AlertSeverity
    tone: BadgeTone
    title: str
    message: str
    details: tuple[str, ...]
    primary_action_label: str
    primary_action_href: str
    secondary_action_label: str | None = None
    secondary_action_href: str | None = None
    timestamp: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "tone": self.tone.value,
            "title": self.title,
            "message": self.message,
            "details": list(self.details),
            "primary_action_label": self.primary_action_label,
            "primary_action_href": self.primary_action_href,
            "secondary_action_label": self.secondary_action_label,
            "secondary_action_href": self.secondary_action_href,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass(frozen=True)
class AlertsScreenView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    total_alerts: int
    critical_count: int
    warning_count: int
    info_count: int
    failure_alerts: tuple[ActionableAlertCard, ...]
    approval_alerts: tuple[ActionableAlertCard, ...]
    budget_alerts: tuple[ActionableAlertCard, ...]
    health_alerts: tuple[ActionableAlertCard, ...]
    strategy_alerts: tuple[ActionableAlertCard, ...]
    all_alerts: tuple[ActionableAlertCard, ...]
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "screen": DashboardScreenId.ALERTS.value,
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "total_alerts": self.total_alerts,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "failure_alerts": [a.as_dict() for a in self.failure_alerts],
            "approval_alerts": [a.as_dict() for a in self.approval_alerts],
            "budget_alerts": [a.as_dict() for a in self.budget_alerts],
            "health_alerts": [a.as_dict() for a in self.health_alerts],
            "strategy_alerts": [a.as_dict() for a in self.strategy_alerts],
            "empty_message": self.empty_message,
        }


class AlertsScreenService:
    """Evaluates and renders actionable alert cards across Command Center domains."""

    def __init__(
        self,
        database: Database,
        *,
        budget_guard: BudgetGuard | None = None,
        health_registry: HealthRegistry | None = None,
        shell_service: DashboardShellService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._budget_guard = budget_guard
        self._health_registry = health_registry
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )
        self._clock = clock

    def load(
        self,
        channel_id: str | None = None,
        *,
        category_filter: AlertCategory | str | None = None,
    ) -> AlertsScreenView:
        parsed_category = _parse_category(category_filter)
        now = self._now()

        shell = self._shell_service.build_shell(
            DashboardScreenId.ALERTS,
            channel_id=channel_id,
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)

            if selected_channel is None:
                # System-wide health checks can still be evaluated even without a channel
                health_alerts = self._collect_health_alerts()
                return AlertsScreenView(
                    shell=shell,
                    load_state=ViewLoadState.EMPTY,
                    channel_id=None,
                    total_alerts=len(health_alerts),
                    critical_count=sum(
                        1
                        for a in health_alerts
                        if a.severity is AlertSeverity.CRITICAL
                    ),
                    warning_count=sum(
                        1
                        for a in health_alerts
                        if a.severity is AlertSeverity.WARNING
                    ),
                    info_count=sum(
                        1 for a in health_alerts if a.severity is AlertSeverity.INFO
                    ),
                    failure_alerts=(),
                    approval_alerts=(),
                    budget_alerts=(),
                    health_alerts=health_alerts,
                    strategy_alerts=(),
                    all_alerts=health_alerts,
                    empty_message="No channel selected or configured. System health alerts are shown above.",
                )

            # Query channel data
            items = ContentItemRepository(connection).list_by_channel(
                selected_channel.id
            )
            item_map: dict[str, ContentItem] = {i.id: i for i in items}
            failed_items = [i for i in items if i.status is ContentStatus.FAILED]

            # AI Jobs
            ai_jobs_repo = AIJobRepository(connection)
            failed_ai_jobs = [
                j
                for j in ai_jobs_repo.list_all()
                if j.status is AIJobStatus.FAILED
                and j.content_item_id in item_map
            ]

            # Publish Jobs
            publish_repo = PublishJobRepository(connection)
            failed_publish_jobs = [
                pj
                for pj in publish_repo.list_all()
                if pj.status is PublishStatus.FAILED
                and pj.content_item_id in item_map
            ]

            # Approvals
            approvals_repo = ApprovalRequestRepository(connection)
            pending_approvals = []
            for item in items:
                for req in approvals_repo.list_by_content_item(item.id):
                    if req.status is ApprovalStatus.PENDING:
                        pending_approvals.append((item, req))

            # Strategy
            strategy_profile = StrategyProfileRepository(
                connection
            ).get_by_channel(selected_channel.id)

        finally:
            connection.close()

        # Collect alert categories
        failure_alerts = self._collect_failure_alerts(
            selected_channel.id,
            failed_items,
            failed_ai_jobs,
            failed_publish_jobs,
            item_map,
        )
        approval_alerts = self._collect_approval_alerts(
            selected_channel.id, pending_approvals, now
        )
        budget_alerts = self._collect_budget_alerts(selected_channel.id, now)
        health_alerts = self._collect_health_alerts()
        strategy_alerts = self._collect_strategy_alerts(
            selected_channel.id, strategy_profile
        )

        all_alerts_list = [
            *failure_alerts,
            *approval_alerts,
            *budget_alerts,
            *health_alerts,
            *strategy_alerts,
        ]

        if parsed_category is not None:
            filtered_alerts = tuple(
                a for a in all_alerts_list if a.category is parsed_category
            )
        else:
            filtered_alerts = tuple(all_alerts_list)

        crit_count = sum(
            1 for a in all_alerts_list if a.severity is AlertSeverity.CRITICAL
        )
        warn_count = sum(
            1 for a in all_alerts_list if a.severity is AlertSeverity.WARNING
        )
        info_count = sum(
            1 for a in all_alerts_list if a.severity is AlertSeverity.INFO
        )

        return AlertsScreenView(
            shell=shell,
            load_state=ViewLoadState.READY,
            channel_id=selected_channel.id,
            total_alerts=len(all_alerts_list),
            critical_count=crit_count,
            warning_count=warn_count,
            info_count=info_count,
            failure_alerts=failure_alerts,
            approval_alerts=approval_alerts,
            budget_alerts=budget_alerts,
            health_alerts=health_alerts,
            strategy_alerts=strategy_alerts,
            all_alerts=filtered_alerts,
        )

    def _collect_failure_alerts(
        self,
        channel_id: str,
        failed_items: list[ContentItem],
        failed_ai_jobs: list[Any],
        failed_publish_jobs: list[Any],
        item_map: dict[str, ContentItem],
    ) -> tuple[ActionableAlertCard, ...]:
        cards: list[ActionableAlertCard] = []

        for item in failed_items:
            cards.append(
                ActionableAlertCard(
                    alert_id=f"failure:item:{item.id}",
                    category=AlertCategory.FAILURE,
                    severity=AlertSeverity.CRITICAL,
                    tone=BadgeTone.DANGER,
                    title=f"Content Item Failed: {item.title}",
                    message=f"Lifecycle transition failed for {item.content_type.value} item.",
                    details=(
                        f"Item ID: {item.id}",
                        f"Type: {item.content_type.value.upper()}",
                        f"Last Updated: {item.updated_at.isoformat()}",
                    ),
                    primary_action_label="Inspect Item",
                    primary_action_href=f"/content/{item.id}",
                    secondary_action_label="View Content Screen",
                    secondary_action_href=f"/dashboard/content?channel_id={channel_id}",
                    timestamp=item.updated_at,
                )
            )

        for job in failed_ai_jobs:
            item_title = (
                item_map[job.content_item_id].title
                if job.content_item_id in item_map
                else "Standalone"
            )
            cards.append(
                ActionableAlertCard(
                    alert_id=f"failure:job:{job.id}",
                    category=AlertCategory.FAILURE,
                    severity=AlertSeverity.CRITICAL,
                    tone=BadgeTone.DANGER,
                    title=f"AI Pipeline Job Failed: {job.kind}",
                    message=f"Job failed after {job.attempts} attempt(s): {job.last_error or 'Unknown failure'}",
                    details=(
                        f"Job ID: {job.id}",
                        f"Kind: {job.kind}",
                        f"Target: {item_title}",
                    ),
                    primary_action_label="Open AI Center",
                    primary_action_href=f"/dashboard/ai-center?channel_id={channel_id}",
                    timestamp=job.updated_at,
                )
            )

        for pjob in failed_publish_jobs:
            item_title = (
                item_map[pjob.content_item_id].title
                if pjob.content_item_id in item_map
                else pjob.content_item_id
            )
            cards.append(
                ActionableAlertCard(
                    alert_id=f"failure:publish:{pjob.id}",
                    category=AlertCategory.FAILURE,
                    severity=AlertSeverity.CRITICAL,
                    tone=BadgeTone.DANGER,
                    title=f"Publishing Failed: {item_title}",
                    message=f"Upload error: {pjob.last_error or 'Non-resumable upload failure'}",
                    details=(
                        f"Job Key: {pjob.idempotency_key}",
                        f"Attempts: {pjob.attempts}",
                    ),
                    primary_action_label="Review Publishing",
                    primary_action_href=f"/content/{pjob.content_item_id}",
                    timestamp=pjob.updated_at,
                )
            )

        return tuple(cards)

    def _collect_approval_alerts(
        self,
        channel_id: str,
        pending: list[tuple[ContentItem, Any]],
        now: datetime,
    ) -> tuple[ActionableAlertCard, ...]:
        cards: list[ActionableAlertCard] = []

        for item, req in pending:
            # Check expiry status (default 72h)
            expires_at = req.created_at + timedelta(hours=72)
            is_urgent = now >= (expires_at - timedelta(hours=12))
            severity = (
                AlertSeverity.CRITICAL if is_urgent else AlertSeverity.WARNING
            )
            tone = BadgeTone.DANGER if is_urgent else BadgeTone.WARNING

            video_b = req.binding_for("video")
            video_hash = (
                video_b.sha256[:10] if video_b and video_b.sha256 else "pending"
            )

            cards.append(
                ActionableAlertCard(
                    alert_id=f"approval:pending:{req.id}",
                    category=AlertCategory.APPROVAL,
                    severity=severity,
                    tone=tone,
                    title=f"Approval Required: {item.title}",
                    message=(
                        f"Urgent approval needed! Expires soon."
                        if is_urgent
                        else "Video rendered and QC passed. Ready for human gate decision."
                    ),
                    details=(
                        f"Item: {item.title}",
                        f"Type: {item.content_type.value.upper()}",
                        f"Video SHA: {video_hash}",
                        f"Requested By: {req.requested_by.kind.value}:{req.requested_by.id}",
                    ),
                    primary_action_label="Review & Approve",
                    primary_action_href=f"/dashboard/approvals?channel_id={channel_id}",
                    secondary_action_label="Preview Video",
                    secondary_action_href=f"/content/{item.id}",
                    timestamp=req.created_at,
                )
            )

        return tuple(cards)

    def _collect_budget_alerts(
        self, channel_id: str, now: datetime
    ) -> tuple[ActionableAlertCard, ...]:
        if self._budget_guard is None:
            return ()

        cards: list[ActionableAlertCard] = []
        try:
            status = self._budget_guard.evaluate(channel_id, at=now)
            if status.blocked:
                cards.append(
                    ActionableAlertCard(
                        alert_id=f"budget:{channel_id}:blocked",
                        category=AlertCategory.BUDGET,
                        severity=AlertSeverity.CRITICAL,
                        tone=BadgeTone.DANGER,
                        title="Budget Guard Active — Production Blocked",
                        message=(
                            f"Channel spend has reached or exceeded limits. "
                            f"Daily: {status.daily_spend}/{status.daily_limit} {status.currency}, "
                            f"Monthly: {status.monthly_spend}/{status.monthly_limit} {status.currency}."
                        ),
                        details=(
                            f"Daily: {status.daily_spend} / {status.daily_limit} {status.currency}",
                            f"Monthly: {status.monthly_spend} / {status.monthly_limit} {status.currency}",
                        ),
                        primary_action_label="Inspect Revenue & Costs",
                        primary_action_href=f"/dashboard/revenue?channel_id={channel_id}",
                        secondary_action_label="Adjust Limits",
                        secondary_action_href=f"/dashboard/settings?channel_id={channel_id}",
                    )
                )

            for alert in status.alerts:
                severity = (
                    AlertSeverity.CRITICAL
                    if alert.threshold_percent >= 100
                    else AlertSeverity.WARNING
                )
                tone = (
                    BadgeTone.DANGER
                    if alert.threshold_percent >= 100
                    else BadgeTone.WARNING
                )
                cards.append(
                    ActionableAlertCard(
                        alert_id=f"budget:{channel_id}:{alert.window.value}:{alert.threshold_percent}",
                        category=AlertCategory.BUDGET,
                        severity=severity,
                        tone=tone,
                        title=f"{alert.window.value.title()} Budget Threshold ({alert.threshold_percent}%)",
                        message=alert.message,
                        details=(
                            f"Window: {alert.window.value}",
                            f"Threshold: {alert.threshold_percent}%",
                        ),
                        primary_action_label="View Cost Ledger",
                        primary_action_href=f"/dashboard/revenue?channel_id={channel_id}",
                    )
                )
        except Exception:
            pass

        return tuple(cards)

    def _collect_health_alerts(self) -> tuple[ActionableAlertCard, ...]:
        if self._health_registry is None:
            return ()

        cards: list[ActionableAlertCard] = []
        report = self._health_registry.report()

        if report.status is HealthStatus.DOWN:
            cards.append(
                ActionableAlertCard(
                    alert_id="health:system:down",
                    category=AlertCategory.HEALTH,
                    severity=AlertSeverity.CRITICAL,
                    tone=BadgeTone.DANGER,
                    title="System Health: Service DOWN",
                    message="One or more critical application health checks have failed.",
                    details=tuple(
                        f"{c.name}: {c.detail or 'Down'}"
                        for c in report.checks
                        if c.status is HealthStatus.DOWN
                    ),
                    primary_action_label="Inspect System Settings",
                    primary_action_href="/dashboard/settings",
                )
            )
        elif report.status is HealthStatus.DEGRADED:
            cards.append(
                ActionableAlertCard(
                    alert_id="health:system:degraded",
                    category=AlertCategory.HEALTH,
                    severity=AlertSeverity.WARNING,
                    tone=BadgeTone.WARNING,
                    title="System Health: Providers Degraded",
                    message="One or more external or AI providers are degraded or unresponsive.",
                    details=tuple(
                        f"{c.name}: {c.detail or 'Degraded'}"
                        for c in report.checks
                        if c.status is not HealthStatus.OK
                    ),
                    primary_action_label="Check Provider Settings",
                    primary_action_href="/dashboard/settings",
                )
            )

        return tuple(cards)

    def _collect_strategy_alerts(
        self, channel_id: str, profile: Any
    ) -> tuple[ActionableAlertCard, ...]:
        cards: list[ActionableAlertCard] = []
        if profile is None or not profile.is_complete:
            missing = (
                list(profile.missing_settings)
                if profile is not None
                else ["all settings"]
            )
            cards.append(
                ActionableAlertCard(
                    alert_id=f"strategy:{channel_id}:incomplete",
                    category=AlertCategory.STRATEGY,
                    severity=AlertSeverity.WARNING,
                    tone=BadgeTone.WARNING,
                    title="Strategy Configuration Incomplete",
                    message=f"Missing settings: {', '.join(missing)}. Channel production will be blocked until configured.",
                    details=tuple(f"Missing: {m}" for m in missing),
                    primary_action_label="Configure Strategy",
                    primary_action_href=f"/dashboard/settings?channel_id={channel_id}",
                )
            )
        return tuple(cards)

    def render_html(self, view: AlertsScreenView) -> str:
        # Category tabs/filter pills
        tabs_html = f"""<div style="display:flex;gap:8px;margin-bottom:16px;overflow-x:auto;padding-bottom:4px;">
  <span class="yt-pill yt-pill--accent">All Alerts ({view.total_alerts})</span>
  <span class="yt-pill yt-pill--danger">Critical ({view.critical_count})</span>
  <span class="yt-pill yt-pill--warning">Warnings ({view.warning_count})</span>
  <span class="yt-pill yt-pill--neutral">Info ({view.info_count})</span>
</div>"""

        if not view.all_alerts:
            cards_html = """<div class="yt-empty" style="padding:48px 24px;text-align:center;">
  <p style="font-size:24px;margin-bottom:8px;">Everything Operational</p>
  <p style="color:rgba(255,255,255,0.7);">No critical failures, pending approvals, budget alerts, or health issues detected.</p>
</div>"""
        else:
            cards_list = []
            for a in view.all_alerts:
                details_items = "".join(
                    f'<li style="font-size:12px;color:rgba(255,255,255,0.7);">{html.escape(d)}</li>'
                    for d in a.details
                )
                sec_btn = (
                    f'<a href="{html.escape(a.secondary_action_href)}" class="yt-button yt-button--secondary" style="font-size:13px;padding:6px 12px;text-decoration:none;">{html.escape(a.secondary_action_label)}</a>'
                    if a.secondary_action_label and a.secondary_action_href
                    else ""
                )

                card = f"""<div class="yt-card" style="border-left:4px solid var(--yt-{a.tone.value});margin-bottom:12px;" data-testid="alert-card-{html.escape(a.alert_id)}">
  <div class="yt-card__header">
    <div>
      <span class="yt-pill yt-pill--{a.tone.value}" style="font-size:11px;text-transform:uppercase;">{html.escape(a.category.value)} · {html.escape(a.severity.value)}</span>
      <h3 style="font-size:16px;font-weight:600;margin-top:6px;">{html.escape(a.title)}</h3>
    </div>
  </div>
  <p style="font-size:14px;color:rgba(255,255,255,0.9);margin:8px 0;">{html.escape(a.message)}</p>
  {f'<ul style="padding-left:18px;margin:8px 0;">{details_items}</ul>' if details_items else ''}
  <div style="display:flex;gap:8px;margin-top:12px;">
    <a href="{html.escape(a.primary_action_href)}" class="yt-button yt-button--primary" style="font-size:13px;padding:6px 14px;text-decoration:none;">{html.escape(a.primary_action_label)}</a>
    {sec_btn}
  </div>
</div>"""
                cards_list.append(card)
            cards_html = "".join(cards_list)

        body = f"""<div class="yt-grid" data-testid="alerts-screen">
  <section class="yt-card yt-grid__span-12">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Command Center Monitoring</p>
        <h2 class="yt-card__title">Actionable Alerts &amp; Triage</h2>
      </div>
      <span class="yt-pill yt-pill--{'danger' if view.critical_count > 0 else 'success'}">{view.total_alerts} active</span>
    </div>
    {tabs_html}
    {cards_html}
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="Alerts — YouTube Command Center",
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _parse_category(value: AlertCategory | str | None) -> AlertCategory | None:
    if value is None:
        return None
    if isinstance(value, AlertCategory):
        return value
    return AlertCategory(str(value).strip().lower())
