"""Analytics freshness model (Prompt Pack v8, prompt #167), context C13 Analytics.

Combines snapshot provenance (``source``, ``retrieved_at``, ``period_start``,
``period_end``) with a configurable ``AnalyticsFreshnessPolicy`` to classify every
``MetricSnapshot`` as ``FRESH``, ``LAGGING`` or ``STALE``:

- ``FreshnessStatus.STALE``: ``now - snapshot.retrieved_at > max_age``.
- ``FreshnessStatus.LAGGING``: retrieved within ``max_age``, but the source's data
  had not yet covered ``period_end`` when read (``data_through < period_end`` when
  ``data_through`` is known, or ``retrieved_at < period_end + policy.data_lag``
  otherwise).
- ``FreshnessStatus.FRESH``: retrieved within ``max_age`` and complete through
  ``period_end``.
- Historical periods that were already complete when read use
  ``policy.settled_period_max_age`` (default 24 hours); lagging periods use the
  scope's ``channel_max_age`` (default 6 hours) or ``video_max_age`` (default 12
  hours) so they are re-read sooner as YouTube's 48-hour reporting lag catches up.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]

DEFAULT_CHANNEL_MAX_AGE: Final[timedelta] = timedelta(hours=6)
DEFAULT_VIDEO_MAX_AGE: Final[timedelta] = timedelta(hours=12)
DEFAULT_SETTLED_MAX_AGE: Final[timedelta] = timedelta(hours=24)
DEFAULT_DATA_LAG: Final[timedelta] = timedelta(days=2)


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    LAGGING = "lagging"
    STALE = "stale"


class FreshnessErrorCode(StrEnum):
    SNAPSHOT_NOT_FOUND = "domain.analytics_snapshot_not_found"
    INVALID_POLICY = "domain.analytics_freshness_invalid_policy"
    INVALID_MOMENT = "domain.analytics_freshness_invalid_moment"


USER_MESSAGES: Final[dict[FreshnessErrorCode, str]] = {
    FreshnessErrorCode.SNAPSHOT_NOT_FOUND: "The metric snapshot was not found.",
    FreshnessErrorCode.INVALID_POLICY: "The analytics freshness policy is invalid.",
    FreshnessErrorCode.INVALID_MOMENT: (
        "The freshness evaluation timestamp is invalid."
    ),
}

_STATUS: Final[dict[FreshnessErrorCode, HTTPStatus]] = {
    FreshnessErrorCode.SNAPSHOT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    FreshnessErrorCode.INVALID_POLICY: HTTPStatus.UNPROCESSABLE_ENTITY,
    FreshnessErrorCode.INVALID_MOMENT: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class FreshnessError(DomainError):
    default_code = FreshnessErrorCode.INVALID_MOMENT.value
    default_user_message = USER_MESSAGES[FreshnessErrorCode.INVALID_MOMENT]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: FreshnessErrorCode, detail: str) -> None:
        if not isinstance(code, FreshnessErrorCode):
            raise TypeError("code must be a FreshnessErrorCode")
        self.freshness_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class AnalyticsFreshnessPolicy:
    channel_max_age: timedelta = DEFAULT_CHANNEL_MAX_AGE
    video_max_age: timedelta = DEFAULT_VIDEO_MAX_AGE
    settled_period_max_age: timedelta = DEFAULT_SETTLED_MAX_AGE
    data_lag: timedelta = DEFAULT_DATA_LAG

    def __post_init__(self) -> None:
        for name in (
            "channel_max_age",
            "video_max_age",
            "settled_period_max_age",
        ):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise FreshnessError(
                    FreshnessErrorCode.INVALID_POLICY,
                    f"{name} must be a positive timedelta",
                )
        if not isinstance(self.data_lag, timedelta) or self.data_lag < timedelta(0):
            raise FreshnessError(
                FreshnessErrorCode.INVALID_POLICY,
                "data_lag must be a non-negative timedelta",
            )

    def max_age_for(self, scope: MetricScope, *, is_complete: bool) -> timedelta:
        base = (
            self.channel_max_age
            if scope is MetricScope.CHANNEL
            else self.video_max_age
        )
        if is_complete and self.settled_period_max_age > base:
            return self.settled_period_max_age
        return base


DEFAULT_FRESHNESS_POLICY: Final[AnalyticsFreshnessPolicy] = AnalyticsFreshnessPolicy()


@dataclass(frozen=True)
class SnapshotFreshness:
    snapshot_id: str
    scope: MetricScope
    subject_id: str
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    evaluated_at: datetime
    data_through: datetime | None
    age: timedelta
    max_age: timedelta
    status: FreshnessStatus
    is_complete: bool

    @property
    def is_fresh(self) -> bool:
        return self.status is FreshnessStatus.FRESH

    @property
    def is_stale(self) -> bool:
        return self.status is FreshnessStatus.STALE

    @property
    def needs_refresh(self) -> bool:
        return self.status in (FreshnessStatus.STALE, FreshnessStatus.LAGGING)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "source": self.source,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat(),
            "data_through": (
                self.data_through.isoformat() if self.data_through else None
            ),
            "age_seconds": int(self.age.total_seconds()),
            "max_age_seconds": int(self.max_age.total_seconds()),
            "status": self.status.value,
            "is_fresh": self.is_fresh,
            "is_stale": self.is_stale,
            "is_complete": self.is_complete,
            "needs_refresh": self.needs_refresh,
        }


def evaluate_freshness(
    snapshot: MetricSnapshot,
    *,
    now: datetime,
    policy: AnalyticsFreshnessPolicy = DEFAULT_FRESHNESS_POLICY,
    data_through: datetime | None = None,
) -> SnapshotFreshness:
    """Evaluate the freshness and completeness of a ``MetricSnapshot`` at ``now``."""
    if not isinstance(snapshot, MetricSnapshot):
        raise TypeError("snapshot must be a MetricSnapshot")
    if not isinstance(policy, AnalyticsFreshnessPolicy):
        raise TypeError("policy must be an AnalyticsFreshnessPolicy")
    _require_utc(now, "now")
    if now < snapshot.retrieved_at:
        raise FreshnessError(
            FreshnessErrorCode.INVALID_MOMENT,
            f"now {now.isoformat()} must not be earlier than retrieved_at "
            f"{snapshot.retrieved_at.isoformat()}",
        )
    if data_through is not None:
        _require_utc(data_through, "data_through")
        is_complete = data_through >= snapshot.period_end
    else:
        is_complete = snapshot.retrieved_at >= snapshot.period_end + policy.data_lag

    max_age = policy.max_age_for(snapshot.scope, is_complete=is_complete)
    age = snapshot.age_at(now)
    stale = snapshot.is_stale(now, max_age)
    if stale:
        status = FreshnessStatus.STALE
    elif not is_complete:
        status = FreshnessStatus.LAGGING
    else:
        status = FreshnessStatus.FRESH

    return SnapshotFreshness(
        snapshot_id=snapshot.id,
        scope=snapshot.scope,
        subject_id=snapshot.subject_id,
        source=snapshot.source,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        retrieved_at=snapshot.retrieved_at,
        evaluated_at=now,
        data_through=data_through,
        age=age,
        max_age=max_age,
        status=status,
        is_complete=is_complete,
    )


class AnalyticsFreshnessService:
    def __init__(
        self,
        database: Database,
        *,
        policy: AnalyticsFreshnessPolicy = DEFAULT_FRESHNESS_POLICY,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._policy = policy
        self._clock = clock

    @property
    def policy(self) -> AnalyticsFreshnessPolicy:
        return self._policy

    def for_snapshot(
        self,
        snapshot_id: str,
        *,
        data_through: datetime | None = None,
        now: datetime | None = None,
    ) -> SnapshotFreshness:
        connection = self._database.connect()
        try:
            snapshot = MetricSnapshotRepository(connection).get(snapshot_id)
        finally:
            connection.close()
        if snapshot is None:
            raise FreshnessError(
                FreshnessErrorCode.SNAPSHOT_NOT_FOUND,
                f"snapshot {snapshot_id} was not found",
            )
        return evaluate_freshness(
            snapshot,
            now=now or self._now(),
            policy=self._policy,
            data_through=data_through,
        )

    def for_subject(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        now: datetime | None = None,
    ) -> list[SnapshotFreshness]:
        moment = now or self._now()
        connection = self._database.connect()
        try:
            snapshots = MetricSnapshotRepository(connection).list_by_subject(
                scope, subject_id
            )
        finally:
            connection.close()
        filtered = [
            snap
            for snap in snapshots
            if (period_start is None or snap.period_start >= period_start)
            and (period_end is None or snap.period_end <= period_end)
        ]
        return [
            evaluate_freshness(snap, now=moment, policy=self._policy)
            for snap in filtered
        ]

    def latest_for_period(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime,
        period_end: datetime,
        source: str | None = None,
        now: datetime | None = None,
    ) -> tuple[MetricSnapshot, SnapshotFreshness] | None:
        """Return the newest snapshot and its freshness for an exact period, or None."""
        moment = now or self._now()
        connection = self._database.connect()
        try:
            snapshots = MetricSnapshotRepository(connection).list_by_subject(
                scope, subject_id
            )
        finally:
            connection.close()
        matching = [
            snap
            for snap in snapshots
            if snap.period_start == period_start
            and snap.period_end == period_end
            and (source is None or snap.source == source)
        ]
        if not matching:
            return None
        latest = matching[-1]
        return latest, evaluate_freshness(latest, now=moment, policy=self._policy)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _require_utc(moment: object, name: str) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise FreshnessError(
            FreshnessErrorCode.INVALID_MOMENT,
            f"{name} must be timezone-aware UTC",
        )
