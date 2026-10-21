"""Budget guard and threshold alert service (Prompt Pack v8, prompt #180), contexts C3 and C14.

``BudgetGuard`` enforces daily and monthly channel spend limits and evaluates
configured ``Budget.alert_thresholds`` (#051), extending ``BudgetGate`` (#037)
with pre-execution cost projection (``estimated_additional_cost``) and structured
alerts:

- Reads the channel's ``StrategyProfile.budget`` and evaluates ``day_window`` and
  ``month_window`` in ``strategy_time_zone(strategy)``.
- Refuses cross-currency spend records in the active month with
  ``BudgetGuardError(CURRENCY_MISMATCH)``.
- Blocks when actual spend already reaches/exceeds the limit
  (``budget.daily_exceeded``, ``budget.monthly_exceeded``) or when adding a
  positive ``estimated_additional_cost`` would push spend above the limit
  (``budget.projected_daily_exceeded``, ``budget.projected_monthly_exceeded``).
- Evaluates every percentage in ``budget.alert_thresholds`` (e.g. ``50``, ``80``,
  ``100``) for both daily and monthly windows and returns crossed thresholds as
  ``BudgetAlert`` objects.
- ``enforce`` audits ``budget.alert_triggered`` when thresholds are crossed, and
  audits ``budget.guard_blocked`` (``AuditResult.DENIED``) and raises
  ``BudgetGuardBlockedError`` when any limit is exceeded.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.channel_metrics import CHANNEL_ENTITY
from ai_youtube_agent.content.cost_ledger import quantize_cost_amount
from ai_youtube_agent.content.strategy import Budget
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.budget_gate import month_window
from ai_youtube_agent.core.daily_limit_gate import (
    day_window,
    strategy_time_zone,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.economics import CostRecordRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.gates import GateReason

Clock = Callable[[], datetime]

BUDGET_ALERT_TRIGGERED: Final[str] = "budget.alert_triggered"
BUDGET_GUARD_BLOCKED: Final[str] = "budget.guard_blocked"
BUDGET_GUARD_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "budget_guard")
HUNDRED: Final[Decimal] = Decimal(100)
TWO_PLACES: Final[Decimal] = Decimal("0.01")


class BudgetWindowKind(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"


class BudgetGuardErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.budget_guard_channel_not_found"
    NO_STRATEGY = "budget.no_strategy"
    NO_BUDGET = "budget.no_budget"
    CURRENCY_MISMATCH = "budget.currency_mismatch"
    INVALID_ESTIMATE = "domain.budget_guard_invalid_estimate"
    BUDGET_EXCEEDED = "domain.budget_guard_exceeded"


USER_MESSAGES: Final[dict[BudgetGuardErrorCode, str]] = {
    BudgetGuardErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    BudgetGuardErrorCode.NO_STRATEGY: (
        "This channel has no strategy, so it has no budget."
    ),
    BudgetGuardErrorCode.NO_BUDGET: "This channel has no budget configured.",
    BudgetGuardErrorCode.CURRENCY_MISMATCH: (
        "Recorded costs use a different currency than the channel budget."
    ),
    BudgetGuardErrorCode.INVALID_ESTIMATE: (
        "Estimated additional cost must be a non-negative finite Decimal."
    ),
    BudgetGuardErrorCode.BUDGET_EXCEEDED: (
        "The operation is blocked because the channel budget limit has been reached."
    ),
}

_STATUS: Final[dict[BudgetGuardErrorCode, HTTPStatus]] = {
    BudgetGuardErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    BudgetGuardErrorCode.NO_STRATEGY: HTTPStatus.CONFLICT,
    BudgetGuardErrorCode.NO_BUDGET: HTTPStatus.CONFLICT,
    BudgetGuardErrorCode.CURRENCY_MISMATCH: HTTPStatus.CONFLICT,
    BudgetGuardErrorCode.INVALID_ESTIMATE: HTTPStatus.UNPROCESSABLE_ENTITY,
    BudgetGuardErrorCode.BUDGET_EXCEEDED: HTTPStatus.CONFLICT,
}


class BudgetGuardError(DomainError):
    default_code = BudgetGuardErrorCode.BUDGET_EXCEEDED.value
    default_user_message = USER_MESSAGES[BudgetGuardErrorCode.BUDGET_EXCEEDED]
    default_http_status = HTTPStatus.CONFLICT

    def __init__(self, code: BudgetGuardErrorCode, detail: str) -> None:
        if not isinstance(code, BudgetGuardErrorCode):
            raise TypeError("code must be a BudgetGuardErrorCode")
        self.guard_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class BudgetAlert:
    window: BudgetWindowKind
    threshold_percent: int
    limit_amount: Decimal
    threshold_amount: Decimal
    current_spend: Decimal
    projected_spend: Decimal
    currency: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.value,
            "threshold_percent": self.threshold_percent,
            "limit_amount": str(self.limit_amount),
            "threshold_amount": str(self.threshold_amount),
            "current_spend": str(self.current_spend),
            "projected_spend": str(self.projected_spend),
            "currency": self.currency,
            "message": self.message,
        }


@dataclass(frozen=True)
class BudgetGuardStatus:
    channel_id: str
    evaluated_at: datetime
    time_zone: str
    currency: str
    day_start: datetime
    day_end: datetime
    month_start: datetime
    month_end: datetime
    daily_limit: Decimal
    monthly_limit: Decimal
    daily_spend: Decimal
    monthly_spend: Decimal
    estimated_additional_cost: Decimal
    projected_daily_spend: Decimal
    projected_monthly_spend: Decimal
    daily_remaining: Decimal
    monthly_remaining: Decimal
    allowed: bool
    blocking_reasons: tuple[GateReason, ...]
    alerts: tuple[BudgetAlert, ...]

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "time_zone": self.time_zone,
            "currency": self.currency,
            "day_start": self.day_start.isoformat(),
            "day_end": self.day_end.isoformat(),
            "month_start": self.month_start.isoformat(),
            "month_end": self.month_end.isoformat(),
            "daily_limit": str(self.daily_limit),
            "monthly_limit": str(self.monthly_limit),
            "daily_spend": str(self.daily_spend),
            "monthly_spend": str(self.monthly_spend),
            "estimated_additional_cost": str(self.estimated_additional_cost),
            "projected_daily_spend": str(self.projected_daily_spend),
            "projected_monthly_spend": str(self.projected_monthly_spend),
            "daily_remaining": str(self.daily_remaining),
            "monthly_remaining": str(self.monthly_remaining),
            "allowed": self.allowed,
            "blocking_reasons": [
                {"code": reason.code, "message": reason.message}
                for reason in self.blocking_reasons
            ],
            "alerts": [alert.as_dict() for alert in self.alerts],
        }


class BudgetGuardBlockedError(BudgetGuardError):
    """Raised by ``BudgetGuard.enforce`` when daily or monthly budget limits are exceeded."""

    def __init__(self, status: BudgetGuardStatus) -> None:
        self.status = status
        codes = ", ".join(r.code for r in status.blocking_reasons)
        super().__init__(
            BudgetGuardErrorCode.BUDGET_EXCEEDED,
            f"channel {status.channel_id} blocked by budget guard: {codes}",
        )


class BudgetGuard:
    """Evaluates and enforces daily/monthly channel budgets and alert thresholds."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._clock = clock

    def evaluate(
        self,
        channel_id: str,
        *,
        estimated_additional_cost: Decimal = Decimal("0"),
        at: datetime | None = None,
        audit_alerts: bool = False,
        actor: Actor = BUDGET_GUARD_ACTOR,
    ) -> BudgetGuardStatus:
        """Evaluate current and projected daily/monthly spend and threshold alerts."""
        try:
            additional = quantize_cost_amount(estimated_additional_cost)
        except ValueError as exc:
            raise BudgetGuardError(
                BudgetGuardErrorCode.INVALID_ESTIMATE, str(exc)
            ) from exc

        now = at if at is not None else self._now()
        connection = self._database.connect()
        try:
            channel = ChannelRepository(connection).get(channel_id)
            if channel is None:
                raise BudgetGuardError(
                    BudgetGuardErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} does not exist",
                )
            strategy = StrategyProfileRepository(connection).get_by_channel(
                channel_id
            )
            if strategy is None:
                raise BudgetGuardError(
                    BudgetGuardErrorCode.NO_STRATEGY,
                    f"channel {channel_id} has no strategy profile",
                )
            budget = strategy.budget
            if budget is None:
                raise BudgetGuardError(
                    BudgetGuardErrorCode.NO_BUDGET,
                    f"channel {channel_id} has no budget configured",
                )
            time_zone = strategy_time_zone(strategy)
            month_start, month_end = month_window(now, time_zone)
            day_start, day_end = day_window(now, time_zone)
            records = CostRecordRepository(connection).list_by_channel(
                channel_id,
                start=min(month_start, day_start),
                end=max(month_end, day_end),
            )
        finally:
            connection.close()

        others = sorted({r.currency for r in records} - {budget.currency})
        if others:
            raise BudgetGuardError(
                BudgetGuardErrorCode.CURRENCY_MISMATCH,
                f"costs in {', '.join(others)} cannot be counted against {budget.currency} budget",
            )

        daily_spend = sum(
            (r.amount for r in records if day_start <= r.incurred_at < day_end),
            Decimal("0"),
        )
        monthly_spend = sum(
            (
                r.amount
                for r in records
                if month_start <= r.incurred_at < month_end
            ),
            Decimal("0"),
        )
        projected_daily = daily_spend + additional
        projected_monthly = monthly_spend + additional

        reasons = _evaluate_blocking_reasons(
            budget,
            daily_spend=daily_spend,
            monthly_spend=monthly_spend,
            additional=additional,
            projected_daily=projected_daily,
            projected_monthly=projected_monthly,
        )
        alerts = _evaluate_alerts(
            budget,
            daily_spend=daily_spend,
            monthly_spend=monthly_spend,
            projected_daily=projected_daily,
            projected_monthly=projected_monthly,
        )
        daily_remaining = max(Decimal("0"), budget.daily_limit - projected_daily)
        monthly_remaining = max(
            Decimal("0"), budget.monthly_limit - projected_monthly
        )
        status = BudgetGuardStatus(
            channel_id=channel_id,
            evaluated_at=now,
            time_zone=time_zone,
            currency=budget.currency,
            day_start=day_start,
            day_end=day_end,
            month_start=month_start,
            month_end=month_end,
            daily_limit=budget.daily_limit,
            monthly_limit=budget.monthly_limit,
            daily_spend=daily_spend,
            monthly_spend=monthly_spend,
            estimated_additional_cost=additional,
            projected_daily_spend=projected_daily,
            projected_monthly_spend=projected_monthly,
            daily_remaining=daily_remaining,
            monthly_remaining=monthly_remaining,
            allowed=not reasons,
            blocking_reasons=reasons,
            alerts=alerts,
        )
        if audit_alerts and alerts:
            highest_daily = max(
                (
                    a.threshold_percent
                    for a in alerts
                    if a.window is BudgetWindowKind.DAILY
                ),
                default=0,
            )
            highest_monthly = max(
                (
                    a.threshold_percent
                    for a in alerts
                    if a.window is BudgetWindowKind.MONTHLY
                ),
                default=0,
            )
            self._audit.record(
                BUDGET_ALERT_TRIGGERED,
                actor,
                EntityRef(CHANNEL_ENTITY, channel_id),
                AuditResult.SUCCESS,
                metadata={
                    "channel_id": channel_id,
                    "currency": budget.currency,
                    "alert_count": len(alerts),
                    "highest_daily_percent": highest_daily,
                    "highest_monthly_percent": highest_monthly,
                    "daily_spend": str(daily_spend),
                    "monthly_spend": str(monthly_spend),
                    "projected_daily_spend": str(projected_daily),
                    "projected_monthly_spend": str(projected_monthly),
                },
            )
        return status

    def enforce(
        self,
        channel_id: str,
        *,
        estimated_additional_cost: Decimal = Decimal("0"),
        at: datetime | None = None,
        actor: Actor = BUDGET_GUARD_ACTOR,
    ) -> BudgetGuardStatus:
        """Evaluate budget and raise ``BudgetGuardBlockedError`` when any limit is exceeded."""
        status = self.evaluate(
            channel_id,
            estimated_additional_cost=estimated_additional_cost,
            at=at,
            audit_alerts=True,
            actor=actor,
        )
        if not status.allowed:
            self._audit.record(
                BUDGET_GUARD_BLOCKED,
                actor,
                EntityRef(CHANNEL_ENTITY, channel_id),
                AuditResult.DENIED,
                metadata={
                    "channel_id": channel_id,
                    "currency": status.currency,
                    "reasons": ",".join(
                        r.code for r in status.blocking_reasons
                    ),
                    "daily_spend": str(status.daily_spend),
                    "monthly_spend": str(status.monthly_spend),
                    "estimated_additional_cost": str(
                        status.estimated_additional_cost
                    ),
                },
            )
            raise BudgetGuardBlockedError(status)
        return status

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _evaluate_blocking_reasons(
    budget: Budget,
    *,
    daily_spend: Decimal,
    monthly_spend: Decimal,
    additional: Decimal,
    projected_daily: Decimal,
    projected_monthly: Decimal,
) -> tuple[GateReason, ...]:
    reasons: list[GateReason] = []
    if daily_spend >= budget.daily_limit:
        reasons.append(
            GateReason(
                "budget.daily_exceeded",
                f"Today's spend has reached the daily budget of "
                f"{budget.daily_limit} {budget.currency}.",
            )
        )
    elif additional > Decimal("0") and projected_daily > budget.daily_limit:
        reasons.append(
            GateReason(
                "budget.projected_daily_exceeded",
                f"Projected daily spend ({projected_daily} {budget.currency}) "
                f"would exceed the daily budget of {budget.daily_limit} {budget.currency}.",
            )
        )

    if monthly_spend >= budget.monthly_limit:
        reasons.append(
            GateReason(
                "budget.monthly_exceeded",
                f"This month's spend has reached the monthly budget of "
                f"{budget.monthly_limit} {budget.currency}.",
            )
        )
    elif additional > Decimal("0") and projected_monthly > budget.monthly_limit:
        reasons.append(
            GateReason(
                "budget.projected_monthly_exceeded",
                f"Projected monthly spend ({projected_monthly} {budget.currency}) "
                f"would exceed the monthly budget of {budget.monthly_limit} {budget.currency}.",
            )
        )
    return tuple(reasons)


def _evaluate_alerts(
    budget: Budget,
    *,
    daily_spend: Decimal,
    monthly_spend: Decimal,
    projected_daily: Decimal,
    projected_monthly: Decimal,
) -> tuple[BudgetAlert, ...]:
    alerts: list[BudgetAlert] = []
    for window, limit, current, projected in (
        (
            BudgetWindowKind.DAILY,
            budget.daily_limit,
            daily_spend,
            projected_daily,
        ),
        (
            BudgetWindowKind.MONTHLY,
            budget.monthly_limit,
            monthly_spend,
            projected_monthly,
        ),
    ):
        for pct in budget.alert_thresholds:
            threshold_amount = (limit * Decimal(pct) / HUNDRED).quantize(
                TWO_PLACES, ROUND_HALF_UP
            )
            if projected * HUNDRED >= limit * Decimal(pct):
                alerts.append(
                    BudgetAlert(
                        window=window,
                        threshold_percent=pct,
                        limit_amount=limit,
                        threshold_amount=threshold_amount,
                        current_spend=current,
                        projected_spend=projected,
                        currency=budget.currency,
                        message=(
                            f"{window.value.capitalize()} spend ({projected} {budget.currency}) "
                            f"has reached {pct}% of the {limit} {budget.currency} {window.value} budget."
                        ),
                    )
                )
    return tuple(alerts)
