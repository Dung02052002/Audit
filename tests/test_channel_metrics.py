"""Tests for the channel metrics sync (N-159, prompt #159)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.channel_metrics import (
    CHANNEL_SYNCED,
    DEFAULT_CHANNEL_METRICS,
    ChannelMetricsSync,
    MetricsSyncError,
    MetricsSyncErrorCode,
)
from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy
from ai_youtube_agent.providers.analytics import (
    AnalyticsErrorCode,
    AnalyticsProviderError,
)
from ai_youtube_agent.providers.mock_analytics import MockAnalyticsProvider
from ai_youtube_agent.providers.mock_youtube_oauth import MockYouTubeOAuthProvider
from ai_youtube_agent.providers.youtube_oauth import YouTubeOAuthError
from factories import USER, make_channel

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)


class Fixture:
    def __init__(self, database: Database, waits: list[float]) -> None:
        self.now = NOW
        self.database = database
        self.channel = make_channel(status=ChannelStatus.ACTIVE)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
        self.provider = MockAnalyticsProvider(clock=self.clock)
        self.oauth = MockYouTubeOAuthProvider(clock=self.clock)
        self.sink = InMemoryAuditSink()
        self.waits = waits
        self.sync = ChannelMetricsSync(
            database,
            self.provider,
            self.oauth,
            AuditLog(self.sink),
            retry_policy=UploadRetryPolicy(max_attempts=3, base_delay_seconds=2),
            sleep=waits.append,
            clock=self.clock,
        )

    def clock(self) -> datetime:
        return self.now

    @property
    def youtube_id(self) -> str:
        return self.channel.youtube.channel_id

    def run(self, **overrides):
        values = dict(period_start=START, period_end=END, actor=USER)
        values.update(overrides)
        return self.sync.sync(self.channel.id, **values)

    def stored(self):
        with self.database.transaction() as connection:
            return MetricSnapshotRepository(connection).list_by_subject(
                MetricScope.CHANNEL, self.channel.id
            )


@pytest.fixture
def fx(database: Database) -> Fixture:
    return Fixture(database, [])


def test_only_the_metrics_the_source_returned_are_stored(fx: Fixture) -> None:
    fx.provider.set_channel_metrics(
        fx.youtube_id,
        {
            "views": Decimal("1200"),
            "likes": Decimal("30"),
            "subscribers_gained": Decimal("5"),
        },
    )

    result = fx.run()

    assert result.created is True
    snapshot = result.snapshot
    assert snapshot is not None
    assert dict(snapshot.metrics) == {
        "views": Decimal("1200"),
        "likes": Decimal("30"),
        "subscribers_gained": Decimal("5"),
    }
    assert snapshot.subject_id == fx.channel.id
    assert snapshot.scope is MetricScope.CHANNEL
    assert snapshot.source == "mock_analytics"
    assert (snapshot.period_start, snapshot.period_end) == (START, END)
    assert set(result.missing) == set(DEFAULT_CHANNEL_METRICS) - set(snapshot.metrics)
    assert "estimated_minutes_watched" in result.missing
    assert result.complete is True
    assert [s.id for s in fx.stored()] == [snapshot.id]


def test_a_channel_with_none_of_the_metrics_gets_no_snapshot(fx: Fixture) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"impressions": Decimal("9")})

    result = fx.run()

    assert result.snapshot is None
    assert result.created is False
    assert result.missing == DEFAULT_CHANNEL_METRICS
    assert fx.stored() == []
    [event] = fx.sink.events()
    assert event.metadata["metric_count"] == 0
    assert event.metadata["snapshot_id"] is None


def test_a_repeated_sync_of_the_same_numbers_stores_nothing_new(fx: Fixture) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("10")})

    first = fx.run()
    fx.now += timedelta(minutes=5)
    again = fx.run()

    assert again.created is False
    assert again.snapshot is not None and first.snapshot is not None
    assert again.snapshot.id == first.snapshot.id
    assert len(fx.stored()) == 1

    fx.now += timedelta(minutes=5)
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("11")})
    changed = fx.run()

    assert changed.created is True
    assert changed.snapshot is not None and changed.snapshot.id != first.snapshot.id
    assert [dict(s.metrics)["views"] for s in fx.stored()] == [Decimal("10"), Decimal("11")]


def test_retryable_provider_errors_are_retried_then_the_sync_succeeds(
    fx: Fixture,
) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("10")})
    fx.provider.fail_next(AnalyticsErrorCode.RATE_LIMITED, times=2)

    result = fx.run()

    assert result.created is True
    assert fx.waits == [2.0, 4.0]
    assert fx.provider.calls == 3


def test_a_non_retryable_provider_error_is_raised_and_nothing_is_stored(
    fx: Fixture,
) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("10")})
    fx.provider.fail_next(AnalyticsErrorCode.QUOTA_EXCEEDED)

    with pytest.raises(AnalyticsProviderError) as caught:
        fx.run()

    assert caught.value.analytics_code is AnalyticsErrorCode.QUOTA_EXCEEDED
    assert fx.provider.calls == 1
    assert fx.stored() == []
    assert list(fx.sink.events()) == []


def test_a_channel_the_source_does_not_know_is_not_found(fx: Fixture) -> None:
    with pytest.raises(AnalyticsProviderError) as caught:
        fx.run()

    assert caught.value.analytics_code is AnalyticsErrorCode.NOT_FOUND


def test_the_period_must_have_ended(fx: Fixture) -> None:
    with pytest.raises(MetricsSyncError) as caught:
        fx.run(period_end=NOW + timedelta(days=1))

    assert caught.value.sync_code is MetricsSyncErrorCode.PERIOD_NOT_ENDED
    assert fx.provider.calls == 0


def test_an_unknown_or_unready_channel_is_refused(
    fx: Fixture, database: Database
) -> None:
    with pytest.raises(MetricsSyncError) as unknown:
        fx.sync.sync("unknown", period_start=START, period_end=END, actor=USER)
    assert unknown.value.sync_code is MetricsSyncErrorCode.CHANNEL_NOT_FOUND
    assert unknown.value.to_public().http_status == 404

    pending = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(pending)
    with pytest.raises(MetricsSyncError) as caught:
        fx.sync.sync(pending.id, period_start=START, period_end=END, actor=USER)
    assert caught.value.sync_code is MetricsSyncErrorCode.CHANNEL_NOT_SYNCABLE
    assert fx.provider.calls == 0


def test_recent_data_is_reported_as_incomplete(fx: Fixture) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("10")})

    result = fx.run(period_start=NOW - timedelta(days=7), period_end=NOW)

    assert result.created is True
    assert result.complete is False
    assert result.data_through == NOW - timedelta(days=2)


def test_an_unknown_metric_name_is_refused_before_any_request(fx: Fixture) -> None:
    with pytest.raises(ValueError):
        fx.run(metrics=("views", "not_a_metric"))

    assert fx.provider.calls == 0


def test_a_revoked_authorization_is_raised_unchanged(fx: Fixture) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("10")})
    fx.oauth.revoke_token(fx.channel.id)

    with pytest.raises(YouTubeOAuthError):
        fx.run()

    assert fx.provider.calls == 0
    assert fx.stored() == []


def test_the_audit_event_has_ids_and_counts_but_no_metric_values(fx: Fixture) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("123456")})

    result = fx.run()

    [event] = fx.sink.events()
    assert event.action == CHANNEL_SYNCED
    assert event.entity.id == fx.channel.id
    assert event.actor == USER
    assert event.metadata["snapshot_id"] == result.snapshot.id
    assert event.metadata["created"] is True
    assert event.metadata["metric_count"] == 1
    assert event.metadata["missing_count"] == len(DEFAULT_CHANNEL_METRICS) - 1
    assert "123456" not in {str(value) for value in event.metadata.values()}


def test_the_stored_snapshots_of_a_channel_can_be_listed(fx: Fixture) -> None:
    fx.provider.set_channel_metrics(fx.youtube_id, {"views": Decimal("10")})
    fx.run()

    assert [s.subject_id for s in fx.sync.snapshots(fx.channel.id)] == [fx.channel.id]
    with pytest.raises(MetricsSyncError):
        fx.sync.snapshots("unknown")
