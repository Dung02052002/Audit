"""Analytics entity (Prompt Pack v8, prompt #023), context C13 Analytics.

A ``MetricSnapshot`` is a set of metrics read from one source at one time:

- ``id``: a stable id for the snapshot.
- ``scope`` and ``subject_id``: ``MetricScope`` channel or video, and the
  opaque id of that channel or video. The sync tasks (#159, #160) decide which
  ids they use.
- ``source``: the name of the analytics provider, such as ``youtube_analytics``.
- ``period_start`` and ``period_end``: the half-open UTC period the metrics
  cover.
- ``retrieved_at``: when the metrics were read, UTC, not before the period
  starts.
- ``metrics``: a non-empty, read-only mapping from a lowercase metric name to a
  finite ``Decimal``. A metric the source did not provide is absent, never 0
  (#164, #165). Units and normalisation come with #163.

Freshness is derived, not stored: ``age_at`` and ``is_stale`` compare
``retrieved_at`` with a given time. The max-age policy comes with #167 and
#168. A snapshot is frozen; a new read makes a new snapshot.
"""

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
METRIC_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
Clock = Callable[[], datetime]


class MetricScope(StrEnum):
    CHANNEL = "channel"
    VIDEO = "video"


@dataclass(frozen=True)
class MetricSnapshot:
    id: str
    scope: MetricScope
    subject_id: str
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    metrics: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        for name in ("id", "subject_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.scope, MetricScope):
            raise TypeError("scope must be a MetricScope")
        if not SOURCE_PATTERN.match(self.source):
            raise ValueError(
                f"source {self.source!r} must be a lowercase name such as "
                "'youtube_analytics'"
            )
        for name in ("period_start", "period_end", "retrieved_at"):
            _require_utc(getattr(self, name), name)
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be earlier than period_end")
        if self.retrieved_at < self.period_start:
            raise ValueError("retrieved_at must not be earlier than period_start")
        object.__setattr__(self, "metrics", _freeze(self.metrics))

    @classmethod
    def create(
        cls,
        scope: MetricScope,
        subject_id: str,
        *,
        source: str,
        period_start: datetime,
        period_end: datetime,
        metrics: Mapping[str, Decimal],
        clock: Clock | None = None,
    ) -> "MetricSnapshot":
        return cls(
            id=uuid.uuid4().hex,
            scope=scope,
            subject_id=subject_id,
            source=source,
            period_start=period_start,
            period_end=period_end,
            retrieved_at=_now(clock),
            metrics=metrics,
        )

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
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "source": self.source,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "metrics": {name: str(value) for name, value in self.metrics.items()},
        }


def _freeze(metrics: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    if not metrics:
        raise ValueError("a snapshot needs at least one metric")
    for name, value in metrics.items():
        if not isinstance(name, str) or not METRIC_NAME_PATTERN.match(name):
            raise ValueError(
                f"metric name {name!r} must be a lowercase name such as 'views'"
            )
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"metric {name!r} must be a finite Decimal")
    return MappingProxyType(dict(metrics))


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
