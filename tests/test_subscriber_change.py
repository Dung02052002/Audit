"""Tests for subscriber change tracking by period (N-166, prompt #166)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.subscriber_change import (
    SUBSCRIBERS_SYNCED,
    SubscriberChangeError,
    SubscriberChangeErrorCode,
    SubscriberChangeService,
    aggregate_subscriber_changes,
    extract_subscriber_change,
)
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from factories import USER, make_channel

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _snapshot(
    snap_id: str,
    period_start: datetime,
    period_end: datetime,
    *,
    scope: MetricScope = MetricScope.CHANNEL,
    subject_id: str = "channel-1",
    retrieved_at: datetime = NOW,
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


def test_extract_subscriber_change_computes_net_only_when_both_gained_and_lost_exist() -> None:
    both = _snapshot(
        "s1",
        T0,
        T0 + timedelta(days=7),
        subscribers_gained=42,
        subscribers_lost=10,
    )
    view = extract_subscriber_change(both)
    assert view.complete_pair is True
    assert view.subscribers_gained == Decimal("42")
    assert view.subscribers_lost == Decimal("10")
    assert view.net_subscribers == Decimal("32")

    only_gained = _snapshot(
        "s2", T0, T0 + timedelta(days=7), subscribers_gained=15
    )
    partial = extract_subscriber_change(only_gained)
    assert partial.complete_pair is False
    assert partial.subscribers_gained == Decimal("15")
    assert partial.subscribers_lost is None
    assert partial.net_subscribers is None


def test_refuses_negative_or_fractional_subscriber_counts() -> None:
    for bad in (
        {"subscribers_gained": Decimal("-1")},
        {"subscribers_lost": Decimal("1.5")},
    ):
        snap = MetricSnapshot(
            id="bad",
            scope=MetricScope.CHANNEL,
            subject_id="channel-1",
            source="mock_analytics",
            period_start=T0,
            period_end=T0 + timedelta(days=7),
            retrieved_at=NOW,
            metrics=bad,
        )
        with pytest.raises(SubscriberChangeError) as caught:
            extract_subscriber_change(snap)
        assert (
            caught.value.subscriber_code
            is SubscriberChangeErrorCode.INVALID_SUBSCRIBER_VALUE
        )


def test_service_syncs_and_aggregates_disjoint_periods_and_refuses_overlaps(
    database: Database,
) -> None:
    now = NOW
    channel = make_channel(status=ChannelStatus.ACTIVE)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)

    provider = MockAnalyticsProvider(clock=lambda: now)
    provider.set_channel_metrics(
        channel.youtube.channel_id,
        {"subscribers_gained": Decimal("30"), "subscribers_lost": Decimal("5")},
    )
    sink = InMemoryAuditSink()
    service = SubscriberChangeService(
        database,
        provider,
        MockYouTubeOAuthProvider(clock=lambda: now),
        AuditLog(sink),
        clock=lambda: now,
    )

    first = service.sync_channel(
        channel.id,
        period_start=T0,
        period_end=T0 + timedelta(days=7),
        actor=USER,
    )
    assert first.created is True
    assert first.subscriber_change is not None
    assert first.subscriber_change.net_subscribers == Decimal("25")

    provider.set_channel_metrics(
        channel.youtube.channel_id,
        {"subscribers_gained": Decimal("12"), "subscribers_lost": Decimal("20")},
    )
    second = service.sync_channel(
        channel.id,
        period_start=T0 + timedelta(days=7),
        period_end=T0 + timedelta(days=14),
        actor=USER,
    )
    assert second.created is True
    assert second.subscriber_change is not None
    assert second.subscriber_change.net_subscribers == Decimal("-8")

    agg = service.aggregate_for_subject(MetricScope.CHANNEL, channel.id)
    assert agg.included_periods == 2
    assert agg.incomplete_period_count == 0
    assert agg.subscribers_gained == Decimal("42")
    assert agg.subscribers_lost == Decimal("25")
    assert agg.net_subscribers == Decimal("17")
    assert sink.events()[-1].action == SUBSCRIBERS_SYNCED

    overlap = _snapshot(
        "overlap",
        T0 + timedelta(days=3),
        T0 + timedelta(days=10),
        subject_id=channel.id,
        subscribers_gained=5,
        subscribers_lost=1,
    )
    with pytest.raises(SubscriberChangeError) as caught:
        aggregate_subscriber_changes([first.snapshot, overlap])  # type: ignore[list-item]
    assert (
        caught.value.subscriber_code
        is SubscriberChangeErrorCode.OVERLAPPING_PERIODS
    )
