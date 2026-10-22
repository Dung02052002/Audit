"""Subscriber change tracking by period (Prompt Pack v8, prompt #166), context C13.

Tracks gained, lost and net subscribers by period for channels and videos without
fabricating missing numbers or double-counting overlapping windows:

- ``extract_subscriber_change`` inspects a ``MetricSnapshot`` and extracts
  ``subscribers_gained`` and ``subscribers_lost`` when present (validating that both
  are non-negative whole numbers). ``net_subscribers`` is ``subscribers_gained -
  subscribers_lost`` **only** when both figures are present; if either is absent,
  ``net_subscribers`` is ``None`` (never assuming a missing figure is ``0``).
- ``aggregate_subscriber_changes`` combines non-overlapping periods for one subject,
  taking the newest snapshot when a period was synced more than once and refusing
  overlapping periods (``OVERLAPPING_PERIODS``).
- ``SubscriberChangeService`` syncs channel subscriber changes from the
  ``AnalyticsProvider``, queries per-period subscriber changes, and aggregates them.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel_metrics import (
    CHANNEL_ENTITY,
    DEFAULT_SYNC_RETRY,
    load_syncable_channel,
    require_ended_period,
)
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy, call_with_retry
from ai_youtube_agent.providers.analytics import (
    AnalyticsPage,
    AnalyticsProvider,
    AnalyticsQuery,
)
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    YouTubeOAuthProvider,
    get_or_renew_token,
)

Clock = Callable[[], datetime]

SUBSCRIBERS_SYNCED: Final[str] = "analytics.subscribers_synced"
SUBSCRIBER_METRICS: Final[tuple[str, ...]] = (
    "subscribers_gained",
    "subscribers_lost",
)


class SubscriberChangeErrorCode(StrEnum):
    INVALID_SUBSCRIBER_VALUE = "domain.subscriber_change_invalid_value"
    INVALID_PERIOD = "domain.subscriber_change_invalid_period"
    MIXED_SUBJECTS = "domain.subscriber_change_mixed_subjects"
    OVERLAPPING_PERIODS = "domain.subscriber_change_overlapping_periods"


USER_MESSAGES: Final[dict[SubscriberChangeErrorCode, str]] = {
    SubscriberChangeErrorCode.INVALID_SUBSCRIBER_VALUE: (
        "Subscriber gained and lost counts must be non-negative whole numbers."
    ),
    SubscriberChangeErrorCode.INVALID_PERIOD: (
        "Subscriber change period must be a valid UTC range."
    ),
    SubscriberChangeErrorCode.MIXED_SUBJECTS: (
        "Subscriber changes can only be aggregated for one subject at a time."
    ),
    SubscriberChangeErrorCode.OVERLAPPING_PERIODS: (
        "Subscriber change periods that overlap cannot be summed."
    ),
}

_STATUS: Final[dict[SubscriberChangeErrorCode, HTTPStatus]] = {
    SubscriberChangeErrorCode.INVALID_SUBSCRIBER_VALUE: HTTPStatus.UNPROCESSABLE_ENTITY,
    SubscriberChangeErrorCode.INVALID_PERIOD: HTTPStatus.UNPROCESSABLE_ENTITY,
    SubscriberChangeErrorCode.MIXED_SUBJECTS: HTTPStatus.UNPROCESSABLE_ENTITY,
    SubscriberChangeErrorCode.OVERLAPPING_PERIODS: HTTPStatus.CONFLICT,
}


class SubscriberChangeError(DomainError):
    default_code = SubscriberChangeErrorCode.INVALID_SUBSCRIBER_VALUE.value
    default_user_message = USER_MESSAGES[
        SubscriberChangeErrorCode.INVALID_SUBSCRIBER_VALUE
    ]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: SubscriberChangeErrorCode, detail: str) -> None:
        if not isinstance(code, SubscriberChangeErrorCode):
            raise TypeError("code must be a SubscriberChangeErrorCode")
        self.subscriber_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


def validate_subscriber_count(value: Decimal | None, name: str) -> None:
    if value is None:
        return
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise SubscriberChangeError(
            SubscriberChangeErrorCode.INVALID_SUBSCRIBER_VALUE,
            f"{name} {value} must be a non-negative whole number",
        )


@dataclass(frozen=True)
class SubscriberChangeView:
    snapshot_id: str
    scope: MetricScope
    subject_id: str
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    net_subscribers: Decimal | None

    @property
    def complete_pair(self) -> bool:
        return self.subscribers_gained is not None and self.subscribers_lost is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "source": self.source,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "subscribers_gained": (
                str(self.subscribers_gained)
                if self.subscribers_gained is not None
                else None
            ),
            "subscribers_lost": (
                str(self.subscribers_lost)
                if self.subscribers_lost is not None
                else None
            ),
            "net_subscribers": (
                str(self.net_subscribers) if self.net_subscribers is not None else None
            ),
            "complete_pair": self.complete_pair,
        }


def extract_subscriber_change(snapshot: MetricSnapshot) -> SubscriberChangeView:
    """Extract gained, lost and net subscribers from a ``MetricSnapshot``."""
    if not isinstance(snapshot, MetricSnapshot):
        raise TypeError("snapshot must be a MetricSnapshot")
    gained = snapshot.metrics.get("subscribers_gained")
    lost = snapshot.metrics.get("subscribers_lost")
    validate_subscriber_count(gained, "subscribers_gained")
    validate_subscriber_count(lost, "subscribers_lost")
    net = gained - lost if gained is not None and lost is not None else None
    return SubscriberChangeView(
        snapshot_id=snapshot.id,
        scope=snapshot.scope,
        subject_id=snapshot.subject_id,
        source=snapshot.source,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        retrieved_at=snapshot.retrieved_at,
        subscribers_gained=gained,
        subscribers_lost=lost,
        net_subscribers=net,
    )


@dataclass(frozen=True)
class AggregatedSubscriberChange:
    scope: MetricScope
    subject_id: str
    period_start: datetime | None
    period_end: datetime | None
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    net_subscribers: Decimal | None
    included_periods: int
    incomplete_period_count: int
    snapshot_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "period_start": (
                self.period_start.isoformat() if self.period_start else None
            ),
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "subscribers_gained": (
                str(self.subscribers_gained)
                if self.subscribers_gained is not None
                else None
            ),
            "subscribers_lost": (
                str(self.subscribers_lost)
                if self.subscribers_lost is not None
                else None
            ),
            "net_subscribers": (
                str(self.net_subscribers) if self.net_subscribers is not None else None
            ),
            "included_periods": self.included_periods,
            "incomplete_period_count": self.incomplete_period_count,
            "snapshot_ids": list(self.snapshot_ids),
        }


def aggregate_subscriber_changes(
    snapshots: Sequence[MetricSnapshot],
    *,
    scope: MetricScope | None = None,
    subject_id: str | None = None,
) -> AggregatedSubscriberChange:
    """Aggregate gained, lost and net subscribers across non-overlapping periods."""
    if not snapshots:
        if scope is None or not subject_id:
            raise ValueError(
                "scope and subject_id are required when snapshots is empty"
            )
        return AggregatedSubscriberChange(
            scope=scope,
            subject_id=subject_id,
            period_start=None,
            period_end=None,
            subscribers_gained=None,
            subscribers_lost=None,
            net_subscribers=None,
            included_periods=0,
            incomplete_period_count=0,
            snapshot_ids=(),
        )

    first = snapshots[0]
    target_scope = scope or first.scope
    target_subject = subject_id or first.subject_id
    by_window: dict[tuple[datetime, datetime], MetricSnapshot] = {}
    for snap in snapshots:
        if snap.scope is not target_scope or snap.subject_id != target_subject:
            raise SubscriberChangeError(
                SubscriberChangeErrorCode.MIXED_SUBJECTS,
                "all snapshots must share scope and subject_id",
            )
        window = (snap.period_start, snap.period_end)
        prior = by_window.get(window)
        if prior is None or snap.retrieved_at >= prior.retrieved_at:
            by_window[window] = snap

    ordered = [
        extract_subscriber_change(by_window[window]) for window in sorted(by_window)
    ]
    for index in range(1, len(ordered)):
        prev = ordered[index - 1]
        curr = ordered[index]
        if prev.period_start < curr.period_end and curr.period_start < prev.period_end:
            raise SubscriberChangeError(
                SubscriberChangeErrorCode.OVERLAPPING_PERIODS,
                f"period "
                f"{prev.period_start.isoformat()}..{prev.period_end.isoformat()} "
                f"overlaps "
                f"{curr.period_start.isoformat()}..{curr.period_end.isoformat()}",
            )

    total_gained = Decimal("0")
    total_lost = Decimal("0")
    included = 0
    incomplete = 0
    for item in ordered:
        if item.subscribers_gained is not None and item.subscribers_lost is not None:
            total_gained += item.subscribers_gained
            total_lost += item.subscribers_lost
            included += 1
        else:
            incomplete += 1

    return AggregatedSubscriberChange(
        scope=target_scope,
        subject_id=target_subject,
        period_start=ordered[0].period_start,
        period_end=ordered[-1].period_end,
        subscribers_gained=total_gained if included > 0 else None,
        subscribers_lost=total_lost if included > 0 else None,
        net_subscribers=(total_gained - total_lost) if included > 0 else None,
        included_periods=included,
        incomplete_period_count=incomplete,
        snapshot_ids=tuple(item.snapshot_id for item in ordered),
    )


@dataclass(frozen=True)
class ChannelSubscriberSyncResult:
    channel_id: str
    snapshot: MetricSnapshot | None
    subscriber_change: SubscriberChangeView | None
    missing: tuple[str, ...]
    created: bool
    data_through: datetime | None
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "snapshot": self.snapshot.as_dict() if self.snapshot else None,
            "subscriber_change": (
                self.subscriber_change.as_dict() if self.subscriber_change else None
            ),
            "missing": list(self.missing),
            "created": self.created,
            "data_through": (
                self.data_through.isoformat() if self.data_through else None
            ),
            "complete": self.complete,
        }


class SubscriberChangeService:
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

    def sync_channel(
        self,
        channel_id: str,
        *,
        period_start: datetime,
        period_end: datetime,
        actor: Actor,
    ) -> ChannelSubscriberSyncResult:
        now = self._now()
        require_ended_period(period_end, now)
        channel = load_syncable_channel(self._database, channel_id)
        query = AnalyticsQuery(
            youtube_channel_id=channel.youtube.channel_id,
            scope=MetricScope.CHANNEL,
            period_start=period_start,
            period_end=period_end,
            metrics=SUBSCRIBER_METRICS,
        )
        token = get_or_renew_token(
            self._oauth, channel.id, required_scopes=(OAuthScope.YT_ANALYTICS_READONLY,)
        )
        page = call_with_retry(
            lambda: self._provider.query(query, token=token),
            self._retry,
            sleep=self._sleep,
        )
        values: dict[str, Decimal] = {}
        for row in page.rows:
            if row.subject_id == channel.youtube.channel_id:
                values = dict(row.metrics)
                break
        missing = tuple(name for name in SUBSCRIBER_METRICS if name not in values)
        validate_subscriber_count(
            values.get("subscribers_gained"), "subscribers_gained"
        )
        validate_subscriber_count(values.get("subscribers_lost"), "subscribers_lost")

        snapshot: MetricSnapshot | None = None
        view: SubscriberChangeView | None = None
        created = False
        if values:
            snapshot, created = self._store_channel(channel.id, page, values)
            view = extract_subscriber_change(snapshot)

        complete = page.data_through is not None and page.data_through >= period_end
        self._audit.record(
            SUBSCRIBERS_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "source": page.source,
                "snapshot_id": snapshot.id if snapshot else None,
                "created": created,
                "complete_pair": view.complete_pair if view else False,
                "missing_count": len(missing),
                "complete": complete,
            },
        )
        return ChannelSubscriberSyncResult(
            channel_id=channel.id,
            snapshot=snapshot,
            subscriber_change=view,
            missing=missing,
            created=created,
            data_through=page.data_through,
            complete=complete,
        )

    def for_subject(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> list[SubscriberChangeView]:
        snapshots = self._load(
            scope, subject_id, period_start=period_start, period_end=period_end
        )
        return [extract_subscriber_change(snapshot) for snapshot in snapshots]

    def aggregate_for_subject(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> AggregatedSubscriberChange:
        snapshots = self._load(
            scope, subject_id, period_start=period_start, period_end=period_end
        )
        return aggregate_subscriber_changes(
            snapshots, scope=scope, subject_id=subject_id
        )

    def _store_channel(
        self,
        channel_id: str,
        page: AnalyticsPage,
        values: dict[str, Decimal],
    ) -> tuple[MetricSnapshot, bool]:
        with self._database.transaction() as connection:
            repository = MetricSnapshotRepository(connection)
            same_period = [
                stored
                for stored in repository.list_by_subject(
                    MetricScope.CHANNEL, channel_id
                )
                if stored.source == page.source
                and stored.period_start == page.period_start
                and stored.period_end == page.period_end
            ]
            merged = (
                {**dict(same_period[-1].metrics), **values}
                if same_period
                else dict(values)
            )
            if same_period and dict(same_period[-1].metrics) == merged:
                return same_period[-1], False
            snapshot = MetricSnapshot.create(
                MetricScope.CHANNEL,
                channel_id,
                source=page.source,
                period_start=page.period_start,
                period_end=page.period_end,
                metrics=merged,
                clock=lambda: page.retrieved_at,
            )
            repository.add(snapshot)
            return snapshot, True

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
            raise SubscriberChangeError(
                SubscriberChangeErrorCode.INVALID_PERIOD,
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

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _require_utc(moment: object, name: str) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise SubscriberChangeError(
            SubscriberChangeErrorCode.INVALID_PERIOD,
            f"{name} must be timezone-aware UTC",
        )
