"""Daily Limit Gate (Prompt Pack v8, prompt #036), context C3 Control Gates.

``DailyLimitGate`` enforces the configured daily production and publish limit.
The rules were approved by the user on 2026-09-30:

- The limit is the channel's ``StrategyProfile.cadence`` for the item's content
  type (``shorts_per_day`` or ``longform_per_day``). The user sets it (R-09).
- Production and publishing are counted separately, each against that limit.
  A production is a move from draft to generating, counted from the
  ``production_starts`` log. A publish is a move to publishing, counted from
  publish jobs created that day that have not failed, leaving out the item's
  own jobs.
- A day is 00:00-24:00 in the cadence time zone (UTC until #050 let the user
  choose one, user decision 2026-10-02). With daylight saving a day may be 23
  or 25 hours long.
- Any other move passes. A channel without a strategy profile is blocked, and a
  limit of 0 blocks every production or publish of that type.
- Since #044 a strategy may not have its cadence configured yet. That blocks
  too (``daily_limit.no_cadence``), because there is no limit to check.

The gate reads through ``StrategySource`` and ``UsageSource``, which
``StrategyProfileRepository`` and ``DailyUsageRepository`` satisfy.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.gates import GateContext, GateName, GateReason, GateResult

TYPE_LABELS = {ContentType.SHORTS: "Shorts", ContentType.LONGFORM: "LongForm"}


class StrategySource(Protocol):
    def get_by_channel(self, channel_id: str) -> StrategyProfile | None: ...


class UsageSource(Protocol):
    def count_production_starts(
        self,
        channel_id: str,
        content_type: ContentType,
        start: datetime,
        end: datetime,
    ) -> int: ...

    def count_publishes(
        self,
        channel_id: str,
        content_type: ContentType,
        start: datetime,
        end: datetime,
        *,
        exclude: str | None = None,
    ) -> int: ...


def day_window(moment: datetime, time_zone: str = "UTC") -> tuple[datetime, datetime]:
    """The calendar day in ``time_zone`` containing ``moment``, as ``[start, end)``.

    ``moment`` and the result are UTC. The day starts at local midnight and
    ends at the next local midnight.
    """
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError("moment must be timezone-aware UTC")
    zone = ZoneInfo(time_zone)
    day = moment.astimezone(zone).date()
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start.date() + timedelta(days=1)
    return (
        start.astimezone(UTC),
        datetime(end.year, end.month, end.day, tzinfo=zone).astimezone(UTC),
    )


def strategy_time_zone(strategy: StrategyProfile) -> str:
    """The cadence time zone of a strategy, or UTC without a cadence (#051)."""
    return strategy.cadence.time_zone if strategy.cadence else "UTC"


class DailyLimitGate:
    name = GateName.DAILY_LIMIT

    def __init__(self, strategies: StrategySource, usage: UsageSource) -> None:
        self._strategies = strategies
        self._usage = usage

    def evaluate(self, context: GateContext) -> GateResult:
        item = context.item
        production = (
            item.status is ContentStatus.DRAFT
            and context.target_status is ContentStatus.GENERATING
        )
        publish = context.target_status is ContentStatus.PUBLISHING
        if not (production or publish):
            return GateResult.passed(self.name, context.at)

        strategy = self._strategies.get_by_channel(item.channel_id)
        if strategy is None:
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason(
                    "daily_limit.no_strategy",
                    "This channel has no strategy, so it has no daily limit.",
                ),
            )
        if strategy.cadence is None:
            return GateResult.blocked(
                self.name,
                context.at,
                GateReason(
                    "daily_limit.no_cadence",
                    "This channel has no cadence configured, so it has no daily limit.",
                ),
            )
        limit = (
            strategy.cadence.shorts_per_day
            if item.content_type is ContentType.SHORTS
            else strategy.cadence.longform_per_day
        )
        start, end = day_window(context.at, strategy.cadence.time_zone)
        if production:
            used = self._usage.count_production_starts(
                item.channel_id, item.content_type, start, end
            )
        else:
            used = self._usage.count_publishes(
                item.channel_id, item.content_type, start, end, exclude=item.id
            )
        if used < limit:
            return GateResult.passed(self.name, context.at)

        action = "production" if production else "publish"
        label = TYPE_LABELS[item.content_type]
        return GateResult.blocked(
            self.name,
            context.at,
            GateReason(
                f"daily_limit.{action}_reached",
                f"The daily {label} {action} limit of {limit} has been reached "
                "for this channel.",
            ),
        )
