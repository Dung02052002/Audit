"""Budget Gate (Prompt Pack v8, prompt #037), context C3 Control Gates.

``BudgetGate`` blocks cost-incurring jobs when budget thresholds are exceeded.
The rules were approved by the user on 2026-09-30:

- Every move into ``GENERATING`` is a cost-incurring job: a new production
  (from draft) and a regeneration. Any other move passes.
- The budget is the channel's ``StrategyProfile.budget``: a daily and a monthly
  limit in one currency, set by the user (R-09). Spend is the sum of the
  channel's ``CostRecord`` amounts, as ``Decimal``.
- A limit is exceeded when spend >= limit, so a limit of 0 blocks every
  cost-incurring move. The daily and monthly limits are both checked, and each
  one that is exceeded gives its own reason.
- A day is a UTC day and a month a UTC calendar month.
- A cost in another currency this month blocks, because there is no exchange
  rate to sum it with (``budget.currency_mismatch``). A channel without a
  strategy profile blocks too.

Only actual spend is counted. Estimating a job's cost before it runs belongs to
the budget guard (#180). The gate reads through ``StrategySource`` and
``CostSource``, which ``StrategyProfileRepository`` and
``CostRecordRepository`` satisfy.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from ai_youtube_agent.content.cost import CostRecord
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.daily_limit_gate import StrategySource, day_window
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult


class CostSource(Protocol):
    def list_by_channel(
        self,
        channel_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[CostRecord]: ...


def month_window(moment: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month containing ``moment``, as ``[start, end)``."""
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError("moment must be timezone-aware UTC")
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


class BudgetGate:
    name = GateName.BUDGET

    def __init__(self, strategies: StrategySource, costs: CostSource) -> None:
        self._strategies = strategies
        self._costs = costs

    def evaluate(self, context: GateContext) -> GateResult:
        if context.target_status is not ContentStatus.GENERATING:
            return GateResult.passed(self.name, context.at)
        reasons = self._check(context.item.channel_id, context.at)
        if not reasons:
            return GateResult.passed(self.name, context.at)
        return GateResult.blocked(self.name, context.at, *reasons)

    def _check(self, channel_id: str, now: datetime) -> list[GateReason]:
        strategy = self._strategies.get_by_channel(channel_id)
        if strategy is None:
            return [
                GateReason(
                    "budget.no_strategy",
                    "This channel has no strategy, so it has no budget.",
                )
            ]
        budget = strategy.budget
        month_start, month_end = month_window(now)
        records = self._costs.list_by_channel(
            channel_id, start=month_start, end=month_end
        )
        others = sorted({r.currency for r in records} - {budget.currency})
        if others:
            return [
                GateReason(
                    "budget.currency_mismatch",
                    f"Costs in {', '.join(others)} cannot be counted against the "
                    f"{budget.currency} budget.",
                )
            ]
        day_start, day_end = day_window(now)
        daily = sum(
            (r.amount for r in records if day_start <= r.incurred_at < day_end),
            Decimal(0),
        )
        monthly = sum((r.amount for r in records), Decimal(0))
        reasons = []
        if daily >= budget.daily_limit:
            reasons.append(
                GateReason(
                    "budget.daily_exceeded",
                    f"Today's spend has reached the daily budget of "
                    f"{budget.daily_limit} {budget.currency}.",
                )
            )
        if monthly >= budget.monthly_limit:
            reasons.append(
                GateReason(
                    "budget.monthly_exceeded",
                    f"This month's spend has reached the monthly budget of "
                    f"{budget.monthly_limit} {budget.currency}.",
                )
            )
        return reasons
