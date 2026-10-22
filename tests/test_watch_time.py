"""Tests for watch-time periods and units normalisation (N-163, prompt #163)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.watch_time import (
    WatchTimeError,
    WatchTimeErrorCode,
    WatchTimeMeasurement,
    WatchTimePeriodKind,
    WatchTimeService,
    WatchTimeUnit,
    aggregate_watch_time,
    convert_watch_time,
    normalize_snapshot_watch_time,
    normalize_watch_time_period,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository

T0 = datetime(2026, 10, 1, tzinfo=UTC)
RETRIEVED = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _snapshot(
    snap_id: str,
    period_start: datetime,
    period_end: datetime,
    *,
    scope: MetricScope = MetricScope.CHANNEL,
    subject_id: str = "channel-1",
    retrieved_at: datetime = RETRIEVED,
    **metrics: str | int,
) -> MetricSnapshot:
    return MetricSnapshot(
        id=snap_id,
        scope=scope,
        subject_id=subject_id,
        source="mock_analytics",
        period_start=period_start,
        period_end=period_end,
        retrieved_at=retrieved_at,
        metrics={name: Decimal(str(value)) for name, value in metrics.items()},
    )


def test_converts_watch_time_across_seconds_minutes_and_hours() -> None:
    assert convert_watch_time(
        150, from_unit=WatchTimeUnit.MINUTES, to_unit=WatchTimeUnit.HOURS
    ) == Decimal("2.50")
    assert convert_watch_time(
        150, from_unit=WatchTimeUnit.MINUTES, to_unit=WatchTimeUnit.SECONDS
    ) == Decimal("9000.00")
    assert convert_watch_time(
        Decimal("1.5"), from_unit=WatchTimeUnit.HOURS, to_unit=WatchTimeUnit.SECONDS
    ) == Decimal("5400.00")

    measurement = WatchTimeMeasurement.from_unit(Decimal("90"), WatchTimeUnit.MINUTES)
    assert measurement.seconds == Decimal("5400.00")
    assert measurement.minutes == Decimal("90.00")
    assert measurement.hours == Decimal("1.50")
    assert measurement.in_unit(WatchTimeUnit.HOURS) == Decimal("1.50")


def test_refuses_negative_or_non_finite_watch_time() -> None:
    for bad in (-1, Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity"), True):
        with pytest.raises(WatchTimeError) as caught:
            convert_watch_time(
                bad,  # type: ignore[arg-type]
                from_unit=WatchTimeUnit.MINUTES,
                to_unit=WatchTimeUnit.HOURS,
            )
        assert caught.value.watch_time_code is WatchTimeErrorCode.INVALID_WATCH_TIME


def test_normalises_daily_weekly_monthly_and_custom_periods() -> None:
    daily = normalize_watch_time_period(T0, T0 + timedelta(days=1))
    weekly = normalize_watch_time_period(T0, T0 + timedelta(days=7))
    monthly = normalize_watch_time_period(T0, datetime(2026, 11, 1, tzinfo=UTC))
    custom = normalize_watch_time_period(T0, T0 + timedelta(days=14))

    assert daily.kind is WatchTimePeriodKind.DAILY
    assert daily.duration_days == Decimal("1.00")
    assert weekly.kind is WatchTimePeriodKind.WEEKLY
    assert weekly.duration_days == Decimal("7.00")
    assert monthly.kind is WatchTimePeriodKind.MONTHLY
    assert monthly.duration_days == Decimal("31.00")
    assert custom.kind is WatchTimePeriodKind.CUSTOM
    assert custom.duration_days == Decimal("14.00")

    with pytest.raises(WatchTimeError) as caught:
        normalize_watch_time_period(T0, T0 + timedelta(days=367))
    assert caught.value.watch_time_code is WatchTimeErrorCode.INVALID_PERIOD


def test_normalises_snapshot_watch_time_and_preserves_absent_metrics() -> None:
    with_watch = _snapshot(
        "s1",
        T0,
        T0 + timedelta(days=10),
        estimated_minutes_watched=600,
        average_view_duration=120,
    )
    view = normalize_snapshot_watch_time(with_watch)

    assert view.watch_time is not None
    assert view.watch_time.hours == Decimal("10.00")
    assert view.watch_time.seconds == Decimal("36000.00")
    assert view.average_view_duration is not None
    assert view.average_view_duration.minutes == Decimal("2.00")
    assert view.daily_average_minutes == Decimal("60.00")
    assert view.daily_average_hours == Decimal("1.00")

    without_watch = _snapshot("s2", T0, T0 + timedelta(days=10), views=100)
    empty_view = normalize_snapshot_watch_time(without_watch)
    assert empty_view.watch_time is None
    assert empty_view.average_view_duration is None
    assert empty_view.daily_average_minutes is None
    assert empty_view.daily_average_hours is None


def test_aggregates_disjoint_periods_deduplicates_retries_and_blocks_overlaps(
    database: Database,
) -> None:
    w1_old = _snapshot(
        "w1-old",
        T0,
        T0 + timedelta(days=7),
        retrieved_at=RETRIEVED - timedelta(hours=2),
        estimated_minutes_watched=100,
    )
    w1_new = _snapshot(
        "w1-new",
        T0,
        T0 + timedelta(days=7),
        retrieved_at=RETRIEVED,
        estimated_minutes_watched=140,
    )
    w2 = _snapshot(
        "w2",
        T0 + timedelta(days=7),
        T0 + timedelta(days=14),
        estimated_minutes_watched=280,
    )
    w3_no_watch = _snapshot(
        "w3",
        T0 + timedelta(days=14),
        T0 + timedelta(days=21),
        views=50,
    )
    with database.transaction() as connection:
        repo = MetricSnapshotRepository(connection)
        for snap in (w1_old, w1_new, w2, w3_no_watch):
            repo.add(snap)

    service = WatchTimeService(database)
    agg = service.aggregate_for_subject(MetricScope.CHANNEL, "channel-1")

    assert agg.included_periods == 2
    assert agg.missing_period_count == 1
    assert agg.total_duration_days == Decimal("14.00")
    assert agg.total_watch_time is not None
    assert agg.total_watch_time.minutes == Decimal("420.00")
    assert agg.total_watch_time.hours == Decimal("7.00")
    assert agg.daily_average_minutes == Decimal("30.00")
    assert agg.snapshot_ids == ("w1-new", "w2", "w3")

    overlapping = _snapshot(
        "overlap",
        T0 + timedelta(days=3),
        T0 + timedelta(days=10),
        estimated_minutes_watched=50,
    )
    with pytest.raises(WatchTimeError) as caught:
        aggregate_watch_time([w1_new, overlapping])
    assert caught.value.watch_time_code is WatchTimeErrorCode.OVERLAPPING_PERIODS
