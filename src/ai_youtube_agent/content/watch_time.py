"""Watch-time periods and units normalisation.

(Prompt Pack v8, prompt #163), context C13.

Normalises watch-time units (seconds, minutes, hours) and reporting periods across
channel and video ``MetricSnapshot`` records without ever fabricating missing data
or double-counting overlapping windows:

- ``WatchTimeUnit`` and ``convert_watch_time`` convert non-negative ``Decimal``
  watch-time figures between ``SECONDS``, ``MINUTES`` and ``HOURS``.
- ``WatchTimeMeasurement`` exposes one watch-time figure in ``seconds``, ``minutes``
  and ``hours``.
- ``normalize_watch_time_period`` validates half-open UTC periods (up to 366 days)
  and classifies them as ``DAILY``, ``WEEKLY``, ``MONTHLY`` or ``CUSTOM`` with exact
  ``duration_seconds`` and ``duration_days``.
- ``normalize_snapshot_watch_time`` extracts ``estimated_minutes_watched`` (in
  minutes) and ``average_view_duration`` (in seconds) from a ``MetricSnapshot`` when
  present, keeping absent metrics ``None`` (never ``0``) and deriving daily averages.
- ``aggregate_watch_time`` combines non-overlapping periods for one subject (taking
  the newest snapshot when the same period was synced more than once) and refuses
  overlapping periods (``OVERLAPPING_PERIODS``) so watch time is never double-counted.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.providers.analytics import MAX_PERIOD

SECONDS_PER_MINUTE: Final[Decimal] = Decimal("60")
MINUTES_PER_HOUR: Final[Decimal] = Decimal("60")
SECONDS_PER_HOUR: Final[Decimal] = Decimal("3600")
SECONDS_PER_DAY: Final[Decimal] = Decimal("86400")
TWO_PLACES: Final[Decimal] = Decimal("0.01")


class WatchTimeUnit(StrEnum):
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"


class WatchTimePeriodKind(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"


class WatchTimeErrorCode(StrEnum):
    INVALID_WATCH_TIME = "domain.watch_time_invalid_value"
    INVALID_PERIOD = "domain.watch_time_invalid_period"
    MIXED_SUBJECTS = "domain.watch_time_mixed_subjects"
    OVERLAPPING_PERIODS = "domain.watch_time_overlapping_periods"


USER_MESSAGES: Final[dict[WatchTimeErrorCode, str]] = {
    WatchTimeErrorCode.INVALID_WATCH_TIME: (
        "Watch time must be a finite, non-negative number."
    ),
    WatchTimeErrorCode.INVALID_PERIOD: (
        "Watch-time period must be a valid UTC range of at most 366 days."
    ),
    WatchTimeErrorCode.MIXED_SUBJECTS: (
        "Watch time can only be aggregated across snapshots of the same subject."
    ),
    WatchTimeErrorCode.OVERLAPPING_PERIODS: (
        "Watch-time snapshots with overlapping periods cannot be summed."
    ),
}

_STATUS: Final[dict[WatchTimeErrorCode, HTTPStatus]] = {
    WatchTimeErrorCode.INVALID_WATCH_TIME: HTTPStatus.UNPROCESSABLE_ENTITY,
    WatchTimeErrorCode.INVALID_PERIOD: HTTPStatus.UNPROCESSABLE_ENTITY,
    WatchTimeErrorCode.MIXED_SUBJECTS: HTTPStatus.UNPROCESSABLE_ENTITY,
    WatchTimeErrorCode.OVERLAPPING_PERIODS: HTTPStatus.CONFLICT,
}


class WatchTimeError(DomainError):
    default_code = WatchTimeErrorCode.INVALID_WATCH_TIME.value
    default_user_message = USER_MESSAGES[WatchTimeErrorCode.INVALID_WATCH_TIME]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: WatchTimeErrorCode, detail: str) -> None:
        if not isinstance(code, WatchTimeErrorCode):
            raise TypeError("code must be a WatchTimeErrorCode")
        self.watch_time_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


def convert_watch_time(
    value: Decimal | int,
    *,
    from_unit: WatchTimeUnit,
    to_unit: WatchTimeUnit,
    places: Decimal = TWO_PLACES,
) -> Decimal:
    """Convert a non-negative watch-time value between units.

    The units are ``SECONDS``, ``MINUTES`` and ``HOURS``.
    """
    if not isinstance(from_unit, WatchTimeUnit) or not isinstance(
        to_unit, WatchTimeUnit
    ):
        raise TypeError("from_unit and to_unit must be WatchTimeUnit values")
    dec = _require_non_negative_decimal(value, "value")
    if from_unit is to_unit:
        return dec.quantize(places, rounding=ROUND_HALF_UP)

    if from_unit is WatchTimeUnit.SECONDS:
        seconds = dec
    elif from_unit is WatchTimeUnit.MINUTES:
        seconds = dec * SECONDS_PER_MINUTE
    else:
        seconds = dec * SECONDS_PER_HOUR

    if to_unit is WatchTimeUnit.SECONDS:
        result = seconds
    elif to_unit is WatchTimeUnit.MINUTES:
        result = seconds / SECONDS_PER_MINUTE
    else:
        result = seconds / SECONDS_PER_HOUR

    return result.quantize(places, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class WatchTimeMeasurement:
    seconds: Decimal
    minutes: Decimal
    hours: Decimal

    def __post_init__(self) -> None:
        for field in ("seconds", "minutes", "hours"):
            _require_non_negative_decimal(getattr(self, field), field)

    @classmethod
    def from_unit(
        cls, value: Decimal | int, unit: WatchTimeUnit
    ) -> "WatchTimeMeasurement":
        if not isinstance(unit, WatchTimeUnit):
            raise TypeError("unit must be a WatchTimeUnit")
        dec = _require_non_negative_decimal(value, "value")
        return cls(
            seconds=convert_watch_time(
                dec, from_unit=unit, to_unit=WatchTimeUnit.SECONDS
            ),
            minutes=convert_watch_time(
                dec, from_unit=unit, to_unit=WatchTimeUnit.MINUTES
            ),
            hours=convert_watch_time(dec, from_unit=unit, to_unit=WatchTimeUnit.HOURS),
        )

    def in_unit(self, unit: WatchTimeUnit) -> Decimal:
        if unit is WatchTimeUnit.SECONDS:
            return self.seconds
        if unit is WatchTimeUnit.MINUTES:
            return self.minutes
        if unit is WatchTimeUnit.HOURS:
            return self.hours
        raise TypeError("unit must be a WatchTimeUnit")

    def as_dict(self) -> dict[str, str]:
        return {
            "seconds": str(self.seconds),
            "minutes": str(self.minutes),
            "hours": str(self.hours),
        }


@dataclass(frozen=True)
class NormalizedWatchTimePeriod:
    period_start: datetime
    period_end: datetime
    kind: WatchTimePeriodKind
    duration_seconds: Decimal
    duration_days: Decimal

    def overlaps(self, other: "NormalizedWatchTimePeriod") -> bool:
        return (
            self.period_start < other.period_end
            and other.period_start < self.period_end
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "kind": self.kind.value,
            "duration_seconds": str(self.duration_seconds),
            "duration_days": str(self.duration_days),
        }


def normalize_watch_time_period(
    period_start: datetime, period_end: datetime
) -> NormalizedWatchTimePeriod:
    """Validate and classify a half-open UTC watch-time period."""
    _require_utc(period_start, "period_start")
    _require_utc(period_end, "period_end")
    if period_start >= period_end:
        raise WatchTimeError(
            WatchTimeErrorCode.INVALID_PERIOD,
            "period_start must be earlier than period_end",
        )
    delta = period_end - period_start
    if delta > MAX_PERIOD:
        raise WatchTimeError(
            WatchTimeErrorCode.INVALID_PERIOD,
            "watch-time period must not exceed 366 days",
        )
    total_seconds = Decimal(str(delta.total_seconds())).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
    days = (total_seconds / SECONDS_PER_DAY).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
    kind = _classify_period(period_start, period_end, delta)
    return NormalizedWatchTimePeriod(
        period_start=period_start,
        period_end=period_end,
        kind=kind,
        duration_seconds=total_seconds,
        duration_days=days,
    )


@dataclass(frozen=True)
class SnapshotWatchTime:
    snapshot_id: str
    scope: MetricScope
    subject_id: str
    source: str
    period: NormalizedWatchTimePeriod
    retrieved_at: datetime
    watch_time: WatchTimeMeasurement | None
    average_view_duration: WatchTimeMeasurement | None
    daily_average_minutes: Decimal | None
    daily_average_hours: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "source": self.source,
            "period": self.period.as_dict(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "watch_time": self.watch_time.as_dict() if self.watch_time else None,
            "average_view_duration": (
                self.average_view_duration.as_dict()
                if self.average_view_duration
                else None
            ),
            "daily_average_minutes": (
                str(self.daily_average_minutes)
                if self.daily_average_minutes is not None
                else None
            ),
            "daily_average_hours": (
                str(self.daily_average_hours)
                if self.daily_average_hours is not None
                else None
            ),
        }


def normalize_snapshot_watch_time(snapshot: MetricSnapshot) -> SnapshotWatchTime:
    """Normalise watch-time metrics and period for one ``MetricSnapshot``."""
    if not isinstance(snapshot, MetricSnapshot):
        raise TypeError("snapshot must be a MetricSnapshot")
    period = normalize_watch_time_period(snapshot.period_start, snapshot.period_end)
    raw_minutes = snapshot.metrics.get("estimated_minutes_watched")
    raw_avg_seconds = snapshot.metrics.get("average_view_duration")

    watch_time = (
        WatchTimeMeasurement.from_unit(raw_minutes, WatchTimeUnit.MINUTES)
        if raw_minutes is not None
        else None
    )
    avg_duration = (
        WatchTimeMeasurement.from_unit(raw_avg_seconds, WatchTimeUnit.SECONDS)
        if raw_avg_seconds is not None
        else None
    )
    daily_minutes: Decimal | None = None
    daily_hours: Decimal | None = None
    if watch_time is not None and period.duration_days > 0:
        daily_minutes = (watch_time.minutes / period.duration_days).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )
        daily_hours = (watch_time.hours / period.duration_days).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )

    return SnapshotWatchTime(
        snapshot_id=snapshot.id,
        scope=snapshot.scope,
        subject_id=snapshot.subject_id,
        source=snapshot.source,
        period=period,
        retrieved_at=snapshot.retrieved_at,
        watch_time=watch_time,
        average_view_duration=avg_duration,
        daily_average_minutes=daily_minutes,
        daily_average_hours=daily_hours,
    )


@dataclass(frozen=True)
class AggregatedWatchTime:
    scope: MetricScope
    subject_id: str
    period_start: datetime | None
    period_end: datetime | None
    total_duration_days: Decimal
    total_watch_time: WatchTimeMeasurement | None
    daily_average_minutes: Decimal | None
    included_periods: int
    missing_period_count: int
    snapshot_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "period_start": (
                self.period_start.isoformat() if self.period_start else None
            ),
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "total_duration_days": str(self.total_duration_days),
            "total_watch_time": (
                self.total_watch_time.as_dict() if self.total_watch_time else None
            ),
            "daily_average_minutes": (
                str(self.daily_average_minutes)
                if self.daily_average_minutes is not None
                else None
            ),
            "included_periods": self.included_periods,
            "missing_period_count": self.missing_period_count,
            "snapshot_ids": list(self.snapshot_ids),
        }


def aggregate_watch_time(
    snapshots: Sequence[MetricSnapshot],
    *,
    scope: MetricScope | None = None,
    subject_id: str | None = None,
) -> AggregatedWatchTime:
    """Aggregate watch time across non-overlapping periods for one subject."""
    if not snapshots:
        if scope is None or not subject_id:
            raise ValueError(
                "scope and subject_id are required when snapshots is empty"
            )
        return AggregatedWatchTime(
            scope=scope,
            subject_id=subject_id,
            period_start=None,
            period_end=None,
            total_duration_days=Decimal("0.00"),
            total_watch_time=None,
            daily_average_minutes=None,
            included_periods=0,
            missing_period_count=0,
            snapshot_ids=(),
        )

    first = snapshots[0]
    target_scope = scope or first.scope
    target_subject = subject_id or first.subject_id
    by_window: dict[tuple[datetime, datetime], MetricSnapshot] = {}
    for snap in snapshots:
        if snap.scope is not target_scope or snap.subject_id != target_subject:
            raise WatchTimeError(
                WatchTimeErrorCode.MIXED_SUBJECTS,
                "all snapshots in aggregate_watch_time must share scope and subject_id",
            )
        window = (snap.period_start, snap.period_end)
        prior = by_window.get(window)
        if prior is None or snap.retrieved_at >= prior.retrieved_at:
            by_window[window] = snap

    ordered = [
        normalize_snapshot_watch_time(by_window[window]) for window in sorted(by_window)
    ]
    for index in range(1, len(ordered)):
        if ordered[index - 1].period.overlaps(ordered[index].period):
            raise WatchTimeError(
                WatchTimeErrorCode.OVERLAPPING_PERIODS,
                f"period {ordered[index - 1].period.period_start.isoformat()}.."
                f"{ordered[index - 1].period.period_end.isoformat()} overlaps "
                f"{ordered[index].period.period_start.isoformat()}.."
                f"{ordered[index].period.period_end.isoformat()}",
            )

    total_minutes = Decimal("0")
    total_days = Decimal("0")
    included = 0
    missing = 0
    for item in ordered:
        if item.watch_time is not None:
            total_minutes += item.watch_time.minutes
            total_days += item.period.duration_days
            included += 1
        else:
            missing += 1

    measurement = (
        WatchTimeMeasurement.from_unit(total_minutes, WatchTimeUnit.MINUTES)
        if included > 0
        else None
    )
    daily_avg = (
        (measurement.minutes / total_days).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if measurement is not None and total_days > 0
        else None
    )
    return AggregatedWatchTime(
        scope=target_scope,
        subject_id=target_subject,
        period_start=ordered[0].period.period_start,
        period_end=ordered[-1].period.period_end,
        total_duration_days=total_days.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        total_watch_time=measurement,
        daily_average_minutes=daily_avg,
        included_periods=included,
        missing_period_count=missing,
        snapshot_ids=tuple(item.snapshot_id for item in ordered),
    )


class WatchTimeService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def for_subject(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> list[SnapshotWatchTime]:
        snapshots = self._load(
            scope, subject_id, period_start=period_start, period_end=period_end
        )
        return [normalize_snapshot_watch_time(snapshot) for snapshot in snapshots]

    def aggregate_for_subject(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> AggregatedWatchTime:
        snapshots = self._load(
            scope, subject_id, period_start=period_start, period_end=period_end
        )
        return aggregate_watch_time(snapshots, scope=scope, subject_id=subject_id)

    def _load(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime | None,
        period_end: datetime | None,
    ) -> list[MetricSnapshot]:
        if period_start is not None:
            _require_utc(period_start, "period_start")
        if period_end is not None:
            _require_utc(period_end, "period_end")
        if (
            period_start is not None
            and period_end is not None
            and period_start >= period_end
        ):
            raise WatchTimeError(
                WatchTimeErrorCode.INVALID_PERIOD,
                "period_start must be earlier than period_end",
            )
        connection = self._database.connect()
        try:
            stored = MetricSnapshotRepository(connection).list_by_subject(
                scope, subject_id
            )
        finally:
            connection.close()
        return [
            snap
            for snap in stored
            if (period_start is None or snap.period_start >= period_start)
            and (period_end is None or snap.period_end <= period_end)
        ]


def _classify_period(
    period_start: datetime, period_end: datetime, delta: timedelta
) -> WatchTimePeriodKind:
    if delta == timedelta(days=1):
        return WatchTimePeriodKind.DAILY
    if delta == timedelta(days=7):
        return WatchTimePeriodKind.WEEKLY
    if timedelta(days=28) <= delta <= timedelta(days=31) or (
        period_start.day == 1
        and period_end.day == 1
        and (
            (
                period_end.year == period_start.year
                and period_end.month == period_start.month + 1
            )
            or (
                period_end.year == period_start.year + 1
                and period_start.month == 12
                and period_end.month == 1
            )
        )
    ):
        return WatchTimePeriodKind.MONTHLY
    return WatchTimePeriodKind.CUSTOM


def _require_non_negative_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise WatchTimeError(
            WatchTimeErrorCode.INVALID_WATCH_TIME,
            f"{name} must be an int or Decimal",
        )
    dec = Decimal(value)
    if not dec.is_finite() or dec < 0:
        raise WatchTimeError(
            WatchTimeErrorCode.INVALID_WATCH_TIME,
            f"{name} must be a finite, non-negative Decimal",
        )
    return dec


def _require_utc(moment: object, name: str) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise WatchTimeError(
            WatchTimeErrorCode.INVALID_PERIOD,
            f"{name} must be timezone-aware UTC",
        )
