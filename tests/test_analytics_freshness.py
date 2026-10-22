"""Tests for analytics freshness model (N-167, prompt #167)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.analytics_freshness import (
    AnalyticsFreshnessPolicy,
    AnalyticsFreshnessService,
    FreshnessError,
    FreshnessErrorCode,
    FreshnessStatus,
    evaluate_freshness,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)


def _snapshot(
    snap_id: str,
    *,
    scope: MetricScope = MetricScope.CHANNEL,
    subject_id: str = "channel-1",
    retrieved_at: datetime,
) -> MetricSnapshot:
    return MetricSnapshot(
        id=snap_id,
        scope=scope,
        subject_id=subject_id,
        source="mock_analytics",
        period_start=START,
        period_end=END,
        retrieved_at=retrieved_at,
        metrics={"views": Decimal("100")},
    )


def test_classifies_fresh_lagging_and_stale_snapshots() -> None:
    # Retrieved 3 days after period_end (past the 2-day data lag) -> complete & settled
    # (24h max_age).
    settled_read = END + timedelta(days=3)
    snap = _snapshot("s-settled", retrieved_at=settled_read)

    fresh = evaluate_freshness(snap, now=settled_read + timedelta(hours=10))
    assert fresh.status is FreshnessStatus.FRESH
    assert fresh.is_fresh is True and fresh.is_stale is False
    assert fresh.is_complete is True and fresh.needs_refresh is False
    assert fresh.max_age == timedelta(hours=24)

    stale = evaluate_freshness(snap, now=settled_read + timedelta(hours=25))
    assert stale.status is FreshnessStatus.STALE
    assert stale.is_stale is True and stale.needs_refresh is True

    # Retrieved 12 hours after period_end (within the 2-day data lag) -> LAGGING.
    early_read = END + timedelta(hours=12)
    early_snap = _snapshot("s-early", retrieved_at=early_read)
    lagging = evaluate_freshness(early_snap, now=early_read + timedelta(hours=1))
    assert lagging.status is FreshnessStatus.LAGGING
    assert lagging.is_complete is False
    assert lagging.is_fresh is False and lagging.is_stale is False
    assert lagging.needs_refresh is True
    assert lagging.max_age == timedelta(hours=6)

    # Explicit data_through >= period_end marks even an early read as complete.
    explicit_complete = evaluate_freshness(
        early_snap,
        now=early_read + timedelta(hours=1),
        data_through=END,
    )
    assert explicit_complete.status is FreshnessStatus.FRESH
    assert explicit_complete.is_complete is True


def test_refuses_invalid_policy_or_evaluation_time_before_retrieval() -> None:
    with pytest.raises(FreshnessError) as policy_err:
        AnalyticsFreshnessPolicy(channel_max_age=timedelta(0))
    assert policy_err.value.freshness_code is FreshnessErrorCode.INVALID_POLICY

    snap = _snapshot("s1", retrieved_at=END + timedelta(days=3))
    with pytest.raises(FreshnessError) as moment_err:
        evaluate_freshness(snap, now=END + timedelta(days=2))
    assert moment_err.value.freshness_code is FreshnessErrorCode.INVALID_MOMENT


def test_service_reads_stored_snapshots_and_picks_latest_for_period(
    database: Database,
) -> None:
    older = _snapshot("s-old", retrieved_at=END + timedelta(hours=6))
    newer = _snapshot("s-new", retrieved_at=END + timedelta(days=3))
    now = END + timedelta(days=3, hours=2)
    with database.transaction() as connection:
        repo = MetricSnapshotRepository(connection)
        repo.add(older)
        repo.add(newer)

    service = AnalyticsFreshnessService(database, clock=lambda: now)
    assert service.for_snapshot("s-old").status is FreshnessStatus.STALE
    assert service.for_snapshot("s-new").status is FreshnessStatus.FRESH

    all_views = service.for_subject(MetricScope.CHANNEL, "channel-1")
    assert [v.snapshot_id for v in all_views] == ["s-old", "s-new"]

    latest = service.latest_for_period(
        MetricScope.CHANNEL,
        "channel-1",
        period_start=START,
        period_end=END,
    )
    assert latest is not None
    latest_snap, latest_freshness = latest
    assert latest_snap.id == "s-new"
    assert latest_freshness.is_fresh is True

    with pytest.raises(FreshnessError) as not_found:
        service.for_snapshot("missing-id")
    assert not_found.value.freshness_code is FreshnessErrorCode.SNAPSHOT_NOT_FOUND
