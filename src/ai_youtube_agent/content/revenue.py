"""Revenue entity (Prompt Pack v8, prompt #024), context C14 Economics.

A ``RevenueRecord`` is one revenue figure read from a source:

- ``id``: a stable id for the record.
- ``stage``: ``RevenueStage`` estimated or final. A final figure is always a new
  record next to the estimate; nothing turns an estimate into a final, so an
  estimate is never overwritten (#171, #172). A final record is read only
  after its period has ended.
- ``scope`` and ``subject_id``: channel or video, as in analytics (B-023).
- ``revenue_type``: the revenue stream, a lowercase name such as ``ads`` or
  ``memberships``, like the B-014 monetization names. #173 formalises the list.
- ``source``: the provider the figure was read from.
- ``amount`` and ``currency``: a finite ``Decimal`` of 0 or more in an ISO 4217
  currency. Corrections are new final records.
- ``period_start`` and ``period_end``: the half-open UTC period.
- ``retrieved_at``: when the figure was read, UTC, not before the period starts.

Freshness is derived from ``retrieved_at`` with ``age_at`` and ``is_stale``.
Revenue is recorded, never promised: nothing here is a guaranteed outcome.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.analytics import SOURCE_PATTERN as DATA_SOURCE_PATTERN
from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.strategy import CURRENCY_PATTERN
from ai_youtube_agent.content.strategy import SOURCE_PATTERN as REVENUE_TYPE_PATTERN

Clock = Callable[[], datetime]


class RevenueStage(StrEnum):
    ESTIMATED = "estimated"
    FINAL = "final"


@dataclass(frozen=True)
class RevenueRecord:
    id: str
    stage: RevenueStage
    scope: MetricScope
    subject_id: str
    revenue_type: str
    source: str
    amount: Decimal
    currency: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "subject_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.stage, RevenueStage):
            raise TypeError("stage must be a RevenueStage")
        if not isinstance(self.scope, MetricScope):
            raise TypeError("scope must be a MetricScope")
        if not REVENUE_TYPE_PATTERN.match(self.revenue_type):
            raise ValueError(
                f"revenue_type {self.revenue_type!r} must be a lowercase name "
                "such as 'ads'"
            )
        if not DATA_SOURCE_PATTERN.match(self.source):
            raise ValueError(
                f"source {self.source!r} must be a lowercase name such as "
                "'youtube_analytics'"
            )
        if (
            not isinstance(self.amount, Decimal)
            or not self.amount.is_finite()
            or self.amount < 0
        ):
            raise ValueError("amount must be a finite Decimal of 0 or more")
        if not CURRENCY_PATTERN.match(self.currency):
            raise ValueError(
                f"currency {self.currency!r} must be an ISO 4217 code such as 'USD'"
            )
        for name in ("period_start", "period_end", "retrieved_at"):
            _require_utc(getattr(self, name), name)
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        if self.retrieved_at < self.period_start:
            raise ValueError("retrieved_at must not be earlier than period_start")
        if self.is_final and self.retrieved_at < self.period_end:
            raise ValueError("final revenue can only be read after the period ends")

    @classmethod
    def create(
        cls,
        stage: RevenueStage,
        scope: MetricScope,
        subject_id: str,
        *,
        revenue_type: str,
        source: str,
        amount: Decimal,
        currency: str,
        period_start: datetime,
        period_end: datetime,
        clock: Clock | None = None,
    ) -> "RevenueRecord":
        return cls(
            id=uuid.uuid4().hex,
            stage=stage,
            scope=scope,
            subject_id=subject_id,
            revenue_type=revenue_type,
            source=source,
            amount=amount,
            currency=currency,
            period_start=period_start,
            period_end=period_end,
            retrieved_at=clock() if clock else datetime.now(UTC),
        )

    @property
    def is_final(self) -> bool:
        return self.stage is RevenueStage.FINAL

    def age_at(self, now: datetime) -> timedelta:
        _require_utc(now, "now")
        if now < self.retrieved_at:
            raise ValueError("now must not be earlier than retrieved_at")
        return now - self.retrieved_at

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        return self.age_at(now) > max_age

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "revenue_type": self.revenue_type,
            "source": self.source,
            "amount": str(self.amount),
            "currency": self.currency,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
        }


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
