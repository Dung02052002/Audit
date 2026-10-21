"""Contribution margin calculator (Prompt Pack v8, prompt #179), context C14 Economics.

Calculates **contribution margin** (``revenue_total - tracked_contribution_costs_total``)
for a channel or a content item over a reporting period:

- Strictly labeled ``contribution_margin`` with ``is_net_profit = False`` and
  ``NOT_NET_PROFIT_DISCLAIMER``. It is **never** labeled or exposed as ``net_profit``
  because untracked overhead, fixed costs, and taxes are outside the cost ledger.
- Never mixes ``RevenueStage.ESTIMATED`` and ``RevenueStage.FINAL`` (#171, #172):
  every calculation requires an explicit ``stage: RevenueStage`` and marks whether
  the revenue side is estimated or finalized.
- Enforces single-currency arithmetic: if recorded revenue and tracked contribution
  costs use different currencies, ``ContributionMarginError(CURRENCY_MISMATCH)`` is
  raised.
- Computes ``contribution_margin_ratio`` (percentage of revenue) only when
  ``revenue_total > 0``; when ``revenue_total == 0``, the ratio is ``None``.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.cost_ledger import (
    CostLedger,
    CostLedgerError,
    CostLedgerErrorCode,
    CostLedgerSummary,
)
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.revenue_sources import (
    RevenueSourceBreakdown,
    RevenueSourceError,
    RevenueSourceErrorCode,
    RevenueSourceService,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish_result import (
    PublishResultNotFoundError,
    PublishResultService,
)

METRIC_LABEL: Final[str] = "contribution_margin"
NOT_NET_PROFIT_DISCLAIMER: Final[str] = (
    "Contribution margin equals recorded revenue minus tracked contribution costs "
    "only; it excludes untracked overhead, fixed costs, and taxes and is never net profit."
)
HUNDRED: Final[Decimal] = Decimal("100")
RATIO_STEP: Final[Decimal] = Decimal("0.01")


class ContributionMarginErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.contribution_margin_channel_not_found"
    ITEM_NOT_FOUND = "domain.contribution_margin_item_not_found"
    CURRENCY_MISMATCH = "domain.contribution_margin_currency_mismatch"
    INVALID_PERIOD = "domain.contribution_margin_invalid_period"


USER_MESSAGES: Final[dict[ContributionMarginErrorCode, str]] = {
    ContributionMarginErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    ContributionMarginErrorCode.ITEM_NOT_FOUND: "The content item does not exist.",
    ContributionMarginErrorCode.CURRENCY_MISMATCH: (
        "Revenue and tracked contribution costs use different currencies."
    ),
    ContributionMarginErrorCode.INVALID_PERIOD: (
        "The contribution margin reporting period is invalid."
    ),
}

_STATUS: Final[dict[ContributionMarginErrorCode, HTTPStatus]] = {
    ContributionMarginErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ContributionMarginErrorCode.ITEM_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ContributionMarginErrorCode.CURRENCY_MISMATCH: HTTPStatus.CONFLICT,
    ContributionMarginErrorCode.INVALID_PERIOD: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class ContributionMarginError(DomainError):
    default_code = ContributionMarginErrorCode.CURRENCY_MISMATCH.value
    default_user_message = USER_MESSAGES[ContributionMarginErrorCode.CURRENCY_MISMATCH]
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, code: ContributionMarginErrorCode, detail: str) -> None:
        if not isinstance(code, ContributionMarginErrorCode):
            raise TypeError("code must be a ContributionMarginErrorCode")
        self.margin_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class ContributionMarginReport:
    """Revenue minus tracked contribution costs (never net profit)."""

    scope: MetricScope
    channel_id: str
    content_item_id: str | None
    youtube_video_id: str | None
    revenue_stage: RevenueStage
    period_start: datetime
    period_end: datetime
    currency: str
    revenue_total: Decimal
    tracked_contribution_costs_total: Decimal
    contribution_margin: Decimal
    contribution_margin_ratio: Decimal | None
    revenue_by_type: dict[str, Decimal]
    costs_by_category: dict[str, Decimal]

    @property
    def margin(self) -> Decimal:
        return self.contribution_margin

    @property
    def metric_label(self) -> str:
        return METRIC_LABEL

    @property
    def is_net_profit(self) -> bool:
        return False

    @property
    def is_final_revenue(self) -> bool:
        return self.revenue_stage is RevenueStage.FINAL

    @property
    def disclaimer(self) -> str:
        return NOT_NET_PROFIT_DISCLAIMER

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_label": METRIC_LABEL,
            "is_net_profit": False,
            "disclaimer": NOT_NET_PROFIT_DISCLAIMER,
            "scope": self.scope.value,
            "channel_id": self.channel_id,
            "content_item_id": self.content_item_id,
            "youtube_video_id": self.youtube_video_id,
            "revenue_stage": self.revenue_stage.value,
            "is_final_revenue": self.is_final_revenue,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "currency": self.currency,
            "revenue_total": str(self.revenue_total),
            "tracked_contribution_costs_total": str(
                self.tracked_contribution_costs_total
            ),
            "contribution_margin": str(self.contribution_margin),
            "contribution_margin_ratio": (
                str(self.contribution_margin_ratio)
                if self.contribution_margin_ratio is not None
                else None
            ),
            "revenue_by_type": {
                k: str(v) for k, v in self.revenue_by_type.items()
            },
            "costs_by_category": {
                k: str(v) for k, v in self.costs_by_category.items()
            },
        }


class ContributionMarginCalculator:
    """Computes channel and per-item contribution margin without ever labeling as net profit."""

    def __init__(
        self,
        database: Database,
        revenue_sources: RevenueSourceService,
        cost_ledger: CostLedger,
        *,
        publish_results: PublishResultService | None = None,
    ) -> None:
        self._database = database
        self._revenue = revenue_sources
        self._costs = cost_ledger
        self._results = publish_results or PublishResultService(database)

    def for_channel(
        self,
        channel_id: str,
        *,
        stage: RevenueStage,
        period_start: datetime,
        period_end: datetime,
        default_currency: str = "USD",
    ) -> ContributionMarginReport:
        """Calculate channel contribution margin over ``[period_start, period_end)``."""
        if not isinstance(stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        if period_start >= period_end:
            raise ContributionMarginError(
                ContributionMarginErrorCode.INVALID_PERIOD,
                "period_start must be earlier than period_end",
            )
        self._require_channel(channel_id)
        rev_breakdown = self._safe_revenue_breakdown(
            MetricScope.CHANNEL,
            channel_id,
            stage=stage,
            period_start=period_start,
            period_end=period_end,
        )
        cost_summary = self._safe_cost_summary(
            channel_id,
            start=period_start,
            end=period_end,
            content_item_id=None,
        )
        currency = self._resolve_currency(
            channel_id,
            revenue_currency=(
                rev_breakdown.currency if rev_breakdown.records else None
            ),
            cost_currency=(
                cost_summary.currency if cost_summary.records else None
            ),
            fallback=default_currency,
        )
        return _build_report(
            scope=MetricScope.CHANNEL,
            channel_id=channel_id,
            content_item_id=None,
            youtube_video_id=None,
            stage=stage,
            period_start=period_start,
            period_end=period_end,
            currency=currency,
            rev_breakdown=rev_breakdown,
            cost_summary=cost_summary,
        )

    def for_content_item(
        self,
        content_item_id: str,
        *,
        stage: RevenueStage,
        period_start: datetime,
        period_end: datetime,
        window_item_costs: bool = False,
        default_currency: str = "USD",
    ) -> ContributionMarginReport:
        """Calculate per-item contribution margin over ``[period_start, period_end)``."""
        if not isinstance(stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        if period_start >= period_end:
            raise ContributionMarginError(
                ContributionMarginErrorCode.INVALID_PERIOD,
                "period_start must be earlier than period_end",
            )
        connection = self._database.connect()
        try:
            item = ContentItemRepository(connection).get(content_item_id)
        finally:
            connection.close()
        if item is None:
            raise ContributionMarginError(
                ContributionMarginErrorCode.ITEM_NOT_FOUND,
                f"content item {content_item_id} does not exist",
            )

        youtube_video_id: str | None = None
        try:
            pub_record = self._results.for_content_item(item.id)
            youtube_video_id = pub_record.youtube_video_id
        except PublishResultNotFoundError:
            youtube_video_id = None

        if youtube_video_id is not None:
            rev_breakdown = self._safe_revenue_breakdown(
                MetricScope.VIDEO,
                youtube_video_id,
                stage=stage,
                period_start=period_start,
                period_end=period_end,
            )
        else:
            rev_breakdown = RevenueSourceBreakdown(
                scope=MetricScope.VIDEO,
                subject_id=item.id,
                stage=stage,
                period_start=period_start,
                period_end=period_end,
                currency=None,
                total_amount=Decimal("0"),
                by_revenue_type={},
                by_category={},
                records=(),
                goal_progress=(),
            )

        cost_summary = self._safe_cost_summary(
            item.channel_id,
            start=period_start if window_item_costs else None,
            end=period_end if window_item_costs else None,
            content_item_id=item.id,
        )
        currency = self._resolve_currency(
            item.channel_id,
            revenue_currency=(
                rev_breakdown.currency if rev_breakdown.records else None
            ),
            cost_currency=(
                cost_summary.currency if cost_summary.records else None
            ),
            fallback=default_currency,
        )
        return _build_report(
            scope=MetricScope.VIDEO,
            channel_id=item.channel_id,
            content_item_id=item.id,
            youtube_video_id=youtube_video_id,
            stage=stage,
            period_start=period_start,
            period_end=period_end,
            currency=currency,
            rev_breakdown=rev_breakdown,
            cost_summary=cost_summary,
        )

    def _require_channel(self, channel_id: str) -> None:
        connection = self._database.connect()
        try:
            channel = ChannelRepository(connection).get(channel_id)
        finally:
            connection.close()
        if channel is None:
            raise ContributionMarginError(
                ContributionMarginErrorCode.CHANNEL_NOT_FOUND,
                f"channel {channel_id} does not exist",
            )

    def _safe_revenue_breakdown(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        stage: RevenueStage,
        period_start: datetime,
        period_end: datetime,
    ) -> RevenueSourceBreakdown:
        try:
            return self._revenue.breakdown(
                scope,
                subject_id,
                stage=stage,
                period_start=period_start,
                period_end=period_end,
            )
        except RevenueSourceError as exc:
            if exc.source_code is RevenueSourceErrorCode.CURRENCY_MISMATCH:
                raise ContributionMarginError(
                    ContributionMarginErrorCode.CURRENCY_MISMATCH, str(exc)
                ) from exc
            raise

    def _safe_cost_summary(
        self,
        channel_id: str,
        *,
        start: datetime | None,
        end: datetime | None,
        content_item_id: str | None,
    ) -> CostLedgerSummary:
        try:
            return self._costs.summarize(
                channel_id,
                start=start,
                end=end,
                content_item_id=content_item_id,
            )
        except CostLedgerError as exc:
            if exc.ledger_code is CostLedgerErrorCode.CURRENCY_MISMATCH:
                raise ContributionMarginError(
                    ContributionMarginErrorCode.CURRENCY_MISMATCH, str(exc)
                ) from exc
            raise

    def _resolve_currency(
        self,
        channel_id: str,
        *,
        revenue_currency: str | None,
        cost_currency: str | None,
        fallback: str,
    ) -> str:
        if (
            revenue_currency is not None
            and cost_currency is not None
            and revenue_currency != cost_currency
        ):
            raise ContributionMarginError(
                ContributionMarginErrorCode.CURRENCY_MISMATCH,
                f"revenue currency {revenue_currency} != cost currency {cost_currency}",
            )
        if revenue_currency is not None:
            return revenue_currency
        if cost_currency is not None:
            return cost_currency
        connection = self._database.connect()
        try:
            strategy = StrategyProfileRepository(connection).get_by_channel(
                channel_id
            )
        finally:
            connection.close()
        if strategy:
            if strategy.budget:
                return strategy.budget.currency
            if strategy.monetization and strategy.monetization.currency:
                return strategy.monetization.currency
        return fallback


def _build_report(
    *,
    scope: MetricScope,
    channel_id: str,
    content_item_id: str | None,
    youtube_video_id: str | None,
    stage: RevenueStage,
    period_start: datetime,
    period_end: datetime,
    currency: str,
    rev_breakdown: RevenueSourceBreakdown,
    cost_summary: CostLedgerSummary,
) -> ContributionMarginReport:
    revenue_total = rev_breakdown.total_amount
    costs_total = cost_summary.total_amount
    margin = revenue_total - costs_total
    ratio: Decimal | None = None
    if revenue_total > Decimal("0"):
        ratio = (margin / revenue_total * HUNDRED).quantize(
            RATIO_STEP, ROUND_HALF_UP
        )
    return ContributionMarginReport(
        scope=scope,
        channel_id=channel_id,
        content_item_id=content_item_id,
        youtube_video_id=youtube_video_id,
        revenue_stage=stage,
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        revenue_total=revenue_total,
        tracked_contribution_costs_total=costs_total,
        contribution_margin=margin,
        contribution_margin_ratio=ratio,
        revenue_by_type=dict(rev_breakdown.by_revenue_type),
        costs_by_category=dict(cost_summary.by_category),
    )
