"""Revenue Screen view and service.

(Prompt Pack v8, prompt Q-197), context X3 Command Center.

Builds the Command Center Revenue Screen view model and iOS-style HTML rendering,
presenting:
- Strictly separated ``RevenueStage.ESTIMATED`` and ``RevenueStage.FINAL`` revenue
  breakdowns via ``RevenueSourceService`` (never summing estimated and final figures
  together, and always keeping monetization targets marked ``is_guaranteed=False``).
- Tracked contribution costs across ``CostCategory`` (``production``, ``api``,
  ``tts``, ``render``, ``storage``, ``llm``) and by provider via ``CostLedger``.
- ``ContributionMarginReport`` for both ``ESTIMATED`` and ``FINAL`` revenue stages,
  strictly labeled ``contribution_margin`` with ``is_net_profit = False`` and
  ``NOT_NET_PROFIT_DISCLAIMER`` (never labeled as net profit).
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.contribution_margin import (
    METRIC_LABEL,
    NOT_NET_PROFIT_DISCLAIMER,
    ContributionMarginCalculator,
    ContributionMarginReport,
)
from ai_youtube_agent.content.cost_ledger import CostLedger, CostLedgerSummary
from ai_youtube_agent.content.dashboard_shell import (
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.revenue_sources import (
    RevenueSourceBreakdown,
    RevenueSourceService,
)
from ai_youtube_agent.core.audit import AuditLog
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.audit import SqliteAuditSink
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository

Clock = Callable[[], datetime]

DEFAULT_REVENUE_WINDOW_DAYS: Final[int] = 30


@dataclass(frozen=True)
class RevenueScreenView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    period_start: datetime
    period_end: datetime
    metric_label: str
    is_net_profit: bool
    disclaimer: str
    estimated_revenue: RevenueSourceBreakdown | None
    final_revenue: RevenueSourceBreakdown | None
    costs: CostLedgerSummary | None
    estimated_margin: ContributionMarginReport | None
    final_margin: ContributionMarginReport | None
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "metric_label": self.metric_label,
            "is_net_profit": self.is_net_profit,
            "disclaimer": self.disclaimer,
            "estimated_revenue": (
                self.estimated_revenue.as_dict() if self.estimated_revenue else None
            ),
            "final_revenue": (
                self.final_revenue.as_dict() if self.final_revenue else None
            ),
            "costs": self.costs.as_dict() if self.costs else None,
            "estimated_margin": (
                self.estimated_margin.as_dict() if self.estimated_margin else None
            ),
            "final_margin": (
                self.final_margin.as_dict() if self.final_margin else None
            ),
            "empty_message": self.empty_message,
        }


class RevenueScreenService:
    """Builds the Revenue Screen view model and renders its HTML."""

    def __init__(
        self,
        database: Database,
        *,
        audit_log: AuditLog | None = None,
        revenue_sources: RevenueSourceService | None = None,
        cost_ledger: CostLedger | None = None,
        margin_calculator: ContributionMarginCalculator | None = None,
        shell_service: DashboardShellService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        audit = audit_log or AuditLog(SqliteAuditSink(database), clock=clock)
        self._revenue_sources = revenue_sources or RevenueSourceService(
            database, audit, clock=clock
        )
        self._cost_ledger = cost_ledger or CostLedger(database, audit, clock=clock)
        self._margin_calculator = margin_calculator or ContributionMarginCalculator(
            database,
            self._revenue_sources,
            self._cost_ledger,
        )
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )

    def load(
        self,
        channel_id: str | None = None,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        default_currency: str = "USD",
    ) -> RevenueScreenView:
        now = self._now()
        end = period_end or now
        start = period_start or (end - timedelta(days=DEFAULT_REVENUE_WINDOW_DAYS))
        if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0):
            raise ValueError("period_start and period_end must be timezone-aware UTC")
        if start >= end:
            raise ValueError("period_start must be earlier than period_end")

        shell = self._shell_service.build_shell(
            DashboardScreenId.REVENUE,
            channel_id=channel_id,
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
        finally:
            connection.close()

        if selected_channel is None:
            return RevenueScreenView(
                shell=shell,
                load_state=ViewLoadState.EMPTY,
                channel_id=None,
                period_start=start,
                period_end=end,
                metric_label=METRIC_LABEL,
                is_net_profit=False,
                disclaimer=NOT_NET_PROFIT_DISCLAIMER,
                estimated_revenue=None,
                final_revenue=None,
                costs=None,
                estimated_margin=None,
                final_margin=None,
                empty_message=(
                    "No revenue records or channel configured yet. "
                    "Create a channel to view revenue, costs, and contribution margin."
                ),
            )

        est_rev = self._revenue_sources.breakdown(
            MetricScope.CHANNEL,
            selected_channel.id,
            stage=RevenueStage.ESTIMATED,
            period_start=start,
            period_end=end,
        )
        fin_rev = self._revenue_sources.breakdown(
            MetricScope.CHANNEL,
            selected_channel.id,
            stage=RevenueStage.FINAL,
            period_start=start,
            period_end=end,
        )
        costs = self._cost_ledger.summarize(
            selected_channel.id,
            start=start,
            end=end,
        )
        est_margin = self._margin_calculator.for_channel(
            selected_channel.id,
            stage=RevenueStage.ESTIMATED,
            period_start=start,
            period_end=end,
            default_currency=default_currency,
        )
        fin_margin = self._margin_calculator.for_channel(
            selected_channel.id,
            stage=RevenueStage.FINAL,
            period_start=start,
            period_end=end,
            default_currency=default_currency,
        )

        has_activity = bool(est_rev.records or fin_rev.records or costs.records)

        return RevenueScreenView(
            shell=shell,
            load_state=ViewLoadState.READY if has_activity else ViewLoadState.EMPTY,
            channel_id=selected_channel.id,
            period_start=start,
            period_end=end,
            metric_label=METRIC_LABEL,
            is_net_profit=False,
            disclaimer=NOT_NET_PROFIT_DISCLAIMER,
            estimated_revenue=est_rev,
            final_revenue=fin_rev,
            costs=costs,
            estimated_margin=est_margin,
            final_margin=fin_margin,
            empty_message=(
                "No revenue or cost records logged for this period yet."
                if not has_activity
                else None
            ),
        )

    def render_html(self, view: RevenueScreenView) -> str:
        if view.channel_id is None:
            msg = html.escape(view.empty_message or "No channel selected.")
            body = (
                '<section class="yt-card" data-testid="revenue-empty-state">'
                '<div class="yt-card__header">'
                '<h2 class="yt-card__title">Revenue &amp; Economics</h2>'
                "</div>"
                f'<p class="yt-empty">{msg}</p>'
                "</section>"
            )
            return render_dashboard_shell_html(
                view.shell,
                body_html=body,
                page_title="Revenue — YouTube Command Center",
            )

        est_m = view.estimated_margin
        fin_m = view.final_margin
        costs = view.costs
        currency = fin_m.currency if fin_m else (est_m.currency if est_m else "USD")

        est_rev_str = f"{est_m.revenue_total} {currency}" if est_m else f"0 {currency}"
        fin_rev_str = f"{fin_m.revenue_total} {currency}" if fin_m else f"0 {currency}"
        cost_str = f"{costs.total_amount} {currency}" if costs else f"0 {currency}"
        est_cm_str = (
            f"{est_m.contribution_margin} {currency}" if est_m else f"0 {currency}"
        )
        fin_cm_str = (
            f"{fin_m.contribution_margin} {currency}" if fin_m else f"0 {currency}"
        )

        cost_rows = ""
        if costs and costs.by_category:
            cost_rows = "".join(
                f"<tr><td>{html.escape(cat)}</td>"
                f"<td>{html.escape(str(amt))} {html.escape(currency)}</td></tr>"
                for cat, amt in sorted(costs.by_category.items())
            )

        body = f"""<div class="yt-grid" data-testid="revenue-screen">
  <section class="yt-card yt-grid__span-6" data-testid="revenue-estimated-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Estimated Stage (Subject to Adjustment)</p>
        <h2 class="yt-card__title">Estimated Revenue</h2>
      </div>
      <span class="yt-pill yt-pill--warning">estimated</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(est_rev_str)}</span>
      <span class="yt-kpi__unit">estimated revenue</span>
    </div>
    <p class="yt-card__subtitle">Estimated contribution margin: <strong>{
            html.escape(est_cm_str)
        }</strong></p>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="revenue-final-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Final Stage (Settled)</p>
        <h2 class="yt-card__title">Final Revenue</h2>
      </div>
      <span class="yt-pill yt-pill--success">final</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(fin_rev_str)}</span>
      <span class="yt-kpi__unit">finalized revenue</span>
    </div>
    <p class="yt-card__subtitle">Final contribution margin: <strong>{
            html.escape(fin_cm_str)
        }</strong></p>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="revenue-costs-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Tracked Contribution Costs</p>
        <h2 class="yt-card__title">Cost Ledger</h2>
      </div>
      <span class="yt-pill yt-pill--info">{
            len(costs.records) if costs else 0
        } entries</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(cost_str)}</span>
      <span class="yt-kpi__unit">tracked costs</span>
    </div>
    <table class="yt-table" data-testid="revenue-costs-table">
      <thead><tr><th>Category</th><th>Amount</th></tr></thead>
      <tbody>{cost_rows}</tbody>
    </table>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="revenue-margin-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Contribution Margin (Not Net Profit)</p>
        <h2 class="yt-card__title">{html.escape(view.metric_label)}</h2>
      </div>
      <span class="yt-pill yt-pill--neutral">is_net_profit=false</span>
    </div>
    <p><strong>Final Contribution Margin:</strong> {html.escape(fin_cm_str)}</p>
    <p><strong>Estimated Contribution Margin:</strong> {html.escape(est_cm_str)}</p>
    <p class="yt-card__subtitle" data-testid="revenue-margin-disclaimer">{
            html.escape(view.disclaimer)
        }</p>
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="Revenue & Contribution Margin — YouTube Command Center",
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
