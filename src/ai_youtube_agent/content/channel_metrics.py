"""Channel Metrics sync (Prompt Pack v8, prompt #159), context C13 Analytics.

``ChannelMetricsSync.sync`` reads the metrics of one channel for one period from the
``AnalyticsProvider`` (#158) and stores them as one ``MetricSnapshot`` (#023):

- The channel must exist and be ``ACTIVE`` or ``PAUSED`` (a pending, disconnected or
  archived channel has nothing to read). The period must be a half-open UTC period
  that has ended (``period_end`` is not in the future).
- The snapshot subject is the internal ``Channel.id`` and the source is the provider's
  name. The metrics are exactly the ones the source returned: a requested metric the
  source did not have is absent from the snapshot and listed in ``missing`` (never
  stored as 0, #164, #165). When the source has none of them there is no row and
  **no snapshot**: the result says so.
- Retryable provider errors are retried with ``call_with_retry`` (bounded, injected
  sleep); other provider errors are raised. The OAuth token needs the read-only
  analytics scope; its errors are raised unchanged.
- Idempotent: when the newest stored snapshot of the same channel, source and period
  holds the same metrics, nothing is written and that snapshot is returned
  (``created`` is false). Changed numbers make a new snapshot: history is kept.
- The result carries ``data_through`` and ``complete`` (the source's data covers the
  whole period), so freshness can be judged later (#167).
- ``analytics.channel_synced`` is audited after the commit with ids, counts and
  flags, never with the metric values.

Only channel scope is read here; per-video metrics are #160.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy, call_with_retry
from ai_youtube_agent.providers.analytics import (
    AnalyticsPage,
    AnalyticsProvider,
    AnalyticsQuery,
)
from ai_youtube_agent.providers.youtube_oauth import OAuthScope, YouTubeOAuthProvider

Clock = Callable[[], datetime]

CHANNEL_SYNCED: Final[str] = "analytics.channel_synced"
CHANNEL_ENTITY: Final[str] = "channel"
DEFAULT_CHANNEL_METRICS: Final[tuple[str, ...]] = (
    "views",
    "estimated_minutes_watched",
    "average_view_duration",
    "subscribers_gained",
    "subscribers_lost",
    "likes",
    "comments",
    "shares",
)
"""What a channel sync asks for by default. Retention and click-through rate have
their own tasks (#164, #165) and are not asked for here."""
DEFAULT_SYNC_RETRY: Final[UploadRetryPolicy] = UploadRetryPolicy(
    max_attempts=3, base_delay_seconds=2
)


class MetricsSyncErrorCode(StrEnum):
    CHANNEL_NOT_FOUND = "domain.metrics_channel_not_found"
    CHANNEL_NOT_SYNCABLE = "domain.metrics_channel_not_syncable"
    PERIOD_NOT_ENDED = "domain.metrics_period_not_ended"
    VIDEO_NOT_PUBLISHED = "domain.metrics_video_not_published"
    TOO_MANY_PAGES = "domain.metrics_too_many_pages"


USER_MESSAGES: Final[dict[MetricsSyncErrorCode, str]] = {
    MetricsSyncErrorCode.CHANNEL_NOT_FOUND: "The channel does not exist.",
    MetricsSyncErrorCode.CHANNEL_NOT_SYNCABLE: (
        "Metrics can only be read for an active or paused channel."
    ),
    MetricsSyncErrorCode.PERIOD_NOT_ENDED: (
        "Metrics can only be read for a period that has ended."
    ),
    MetricsSyncErrorCode.VIDEO_NOT_PUBLISHED: (
        "Metrics can only be read for videos this channel published."
    ),
    MetricsSyncErrorCode.TOO_MANY_PAGES: (
        "The analytics source returned more pages than expected."
    ),
}
_STATUS: Final[dict[MetricsSyncErrorCode, HTTPStatus]] = {
    MetricsSyncErrorCode.CHANNEL_NOT_FOUND: HTTPStatus.NOT_FOUND,
    MetricsSyncErrorCode.CHANNEL_NOT_SYNCABLE: HTTPStatus.CONFLICT,
    MetricsSyncErrorCode.PERIOD_NOT_ENDED: HTTPStatus.UNPROCESSABLE_ENTITY,
    MetricsSyncErrorCode.VIDEO_NOT_PUBLISHED: HTTPStatus.UNPROCESSABLE_ENTITY,
    MetricsSyncErrorCode.TOO_MANY_PAGES: HTTPStatus.BAD_GATEWAY,
}


class MetricsSyncError(DomainError):
    default_code = MetricsSyncErrorCode.CHANNEL_NOT_FOUND.value
    default_user_message = USER_MESSAGES[MetricsSyncErrorCode.CHANNEL_NOT_FOUND]
    default_http_status = HTTPStatus.NOT_FOUND

    def __init__(self, code: MetricsSyncErrorCode, detail: str) -> None:
        if not isinstance(code, MetricsSyncErrorCode):
            raise TypeError("code must be a MetricsSyncErrorCode")
        self.sync_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class ChannelMetricsResult:
    channel_id: str
    snapshot: MetricSnapshot | None
    requested: tuple[str, ...]
    missing: tuple[str, ...]
    created: bool
    data_through: datetime | None
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "snapshot": self.snapshot.as_dict() if self.snapshot else None,
            "requested": list(self.requested),
            "missing": list(self.missing),
            "created": self.created,
            "data_through": self.data_through.isoformat() if self.data_through else None,
            "complete": self.complete,
        }


class ChannelMetricsSync:
    def __init__(
        self,
        database: Database,
        provider: AnalyticsProvider,
        oauth_provider: YouTubeOAuthProvider,
        audit_log: AuditLog,
        *,
        retry_policy: UploadRetryPolicy = DEFAULT_SYNC_RETRY,
        sleep: Callable[[float], None] = time.sleep,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._provider = provider
        self._oauth = oauth_provider
        self._audit = audit_log
        self._retry = retry_policy
        self._sleep = sleep
        self._clock = clock

    def sync(
        self,
        channel_id: str,
        *,
        period_start: datetime,
        period_end: datetime,
        actor: Actor,
        metrics: Sequence[str] = DEFAULT_CHANNEL_METRICS,
    ) -> ChannelMetricsResult:
        now = self._now()
        require_ended_period(period_end, now)
        channel = load_syncable_channel(self._database, channel_id)
        requested = tuple(metrics)
        query = AnalyticsQuery(
            youtube_channel_id=channel.youtube.channel_id,
            scope=MetricScope.CHANNEL,
            period_start=period_start,
            period_end=period_end,
            metrics=requested,
        )
        token = self._oauth.get_token(
            channel.id, required_scopes=(OAuthScope.YT_ANALYTICS_READONLY,)
        )
        page = call_with_retry(
            lambda: self._provider.query(query, token=token),
            self._retry,
            sleep=self._sleep,
        )
        values = _channel_values(page, channel.youtube.channel_id)
        missing = tuple(name for name in requested if name not in values)

        snapshot: MetricSnapshot | None = None
        created = False
        if values:
            snapshot, created = self._store(channel, page, values)
        complete = page.data_through is not None and page.data_through >= period_end
        self._audit.record(
            CHANNEL_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "source": page.source,
                "snapshot_id": snapshot.id if snapshot else None,
                "created": created,
                "metric_count": len(values),
                "missing_count": len(missing),
                "complete": complete,
                "data_through": page.data_through.isoformat() if page.data_through else None,
            },
        )
        return ChannelMetricsResult(
            channel_id=channel.id,
            snapshot=snapshot,
            requested=requested,
            missing=missing,
            created=created,
            data_through=page.data_through,
            complete=complete,
        )

    def snapshots(self, channel_id: str) -> list[MetricSnapshot]:
        """The stored snapshots of a channel, oldest period first."""
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise MetricsSyncError(
                    MetricsSyncErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} was not found",
                )
            return MetricSnapshotRepository(connection).list_by_subject(
                MetricScope.CHANNEL, channel_id
            )
        finally:
            connection.close()

    def _store(
        self, channel: Channel, page: AnalyticsPage, values: dict[str, Decimal]
    ) -> tuple[MetricSnapshot, bool]:
        with self._database.transaction() as connection:
            repository = MetricSnapshotRepository(connection)
            same_period = [
                stored
                for stored in repository.list_by_subject(MetricScope.CHANNEL, channel.id)
                if stored.source == page.source
                and stored.period_start == page.period_start
                and stored.period_end == page.period_end
            ]
            if same_period and dict(same_period[-1].metrics) == values:
                return same_period[-1], False
            snapshot = MetricSnapshot.create(
                MetricScope.CHANNEL,
                channel.id,
                source=page.source,
                period_start=page.period_start,
                period_end=page.period_end,
                metrics=values,
                clock=lambda: page.retrieved_at,
            )
            repository.add(snapshot)
            return snapshot, True

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _channel_values(page: AnalyticsPage, youtube_channel_id: str) -> dict[str, Decimal]:
    """The metrics of the channel row, or none when the source returned no row."""
    for row in page.rows:
        if row.subject_id == youtube_channel_id:
            return dict(row.metrics)
    return {}


def require_ended_period(period_end: datetime, now: datetime) -> None:
    if period_end > now:
        raise MetricsSyncError(
            MetricsSyncErrorCode.PERIOD_NOT_ENDED,
            f"period_end {period_end.isoformat()} is after now {now.isoformat()}",
        )


def load_syncable_channel(database: Database, channel_id: str) -> Channel:
    """The channel to read metrics of: it must exist and be ACTIVE or PAUSED."""
    connection = database.connect()
    try:
        channel = ChannelRepository(connection).get(channel_id)
    finally:
        connection.close()
    if channel is None:
        raise MetricsSyncError(
            MetricsSyncErrorCode.CHANNEL_NOT_FOUND,
            f"channel {channel_id} was not found",
        )
    if channel.status not in (ChannelStatus.ACTIVE, ChannelStatus.PAUSED):
        raise MetricsSyncError(
            MetricsSyncErrorCode.CHANNEL_NOT_SYNCABLE,
            f"channel {channel.id} is {channel.status.value}",
        )
    return channel
