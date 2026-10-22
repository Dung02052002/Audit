"""Long-form metrics normalisation (Prompt Pack v8, prompt #162), context C13 Analytics.

Normalises per-video ``MetricSnapshot`` records for long-form YouTube videos without
ever inventing or zero-filling absent metrics:

- ``normalize_longform_metrics`` validates that a snapshot has ``MetricScope.VIDEO``
  and extracts long-form video metrics (``views``, ``impressions``,
  ``impressions_click_through_rate``, ``estimated_minutes_watched``,
  ``average_view_duration``, ``average_view_percentage``, ``likes``, ``comments``,
  ``shares``, ``subscribers_gained``, ``subscribers_lost``) when present.
- Absent metrics stay ``None`` (never ``0``) and are listed in ``missing_metrics``.
- Derived long-form units and rates are computed **only** when their inputs exist:
  - ``watch_time_hours`` and ``watch_time_seconds`` from ``estimated_minutes_watched``.
  - ``average_view_duration_minutes`` from ``average_view_duration``.
  - ``like_rate``, ``comment_rate``, ``share_rate`` and ``subscriber_conversion_rate``
    per 100 views when ``views > 0`` and the numerator metric is present.
- ``LongFormMetricsService`` loads stored snapshots for a published
  ``ContentType.LONGFORM`` item or video id, refusing ``ContentType.SHORTS`` items
  with ``LongFormMetricsError``.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.video_metrics import DEFAULT_VIDEO_METRICS
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish import VIDEO_ID_PATTERN, PublishStatus
from ai_youtube_agent.pipeline.publish_result import PublishResultService

LONGFORM_MIN_DURATION_SECONDS: Final[Decimal] = Decimal("181")
LONGFORM_MAX_DURATION_SECONDS: Final[Decimal] = Decimal("14400")
SECONDS_PER_MINUTE: Final[Decimal] = Decimal("60")
MINUTES_PER_HOUR: Final[Decimal] = Decimal("60")
HUNDRED: Final[Decimal] = Decimal("100")
TWO_PLACES: Final[Decimal] = Decimal("0.01")

LONGFORM_METRICS_CATALOGUE: Final[tuple[str, ...]] = (
    *DEFAULT_VIDEO_METRICS,
    "average_view_percentage",
    "impressions",
    "impressions_click_through_rate",
)
_NON_NEGATIVE_METRICS: Final[frozenset[str]] = frozenset(
    {
        "views",
        "estimated_minutes_watched",
        "average_view_duration",
        "average_view_percentage",
        "subscribers_gained",
        "subscribers_lost",
        "impressions",
        "impressions_click_through_rate",
    }
)


class LongFormMetricsErrorCode(StrEnum):
    CONTENT_NOT_FOUND = "domain.longform_metrics_content_not_found"
    NOT_LONGFORM = "domain.longform_metrics_not_longform"
    INVALID_SCOPE = "domain.longform_metrics_invalid_scope"
    INVALID_DURATION = "domain.longform_metrics_invalid_duration"
    INVALID_METRIC_VALUE = "domain.longform_metrics_invalid_value"


USER_MESSAGES: Final[dict[LongFormMetricsErrorCode, str]] = {
    LongFormMetricsErrorCode.CONTENT_NOT_FOUND: (
        "The requested long-form content was not found."
    ),
    LongFormMetricsErrorCode.NOT_LONGFORM: (
        "Long-form metrics normalisation only applies to long-form content."
    ),
    LongFormMetricsErrorCode.INVALID_SCOPE: (
        "Long-form metrics require a per-video metric snapshot."
    ),
    LongFormMetricsErrorCode.INVALID_DURATION: (
        "Long-form duration must be between 181 and 14400 seconds."
    ),
    LongFormMetricsErrorCode.INVALID_METRIC_VALUE: (
        "One or more metric values in the snapshot are outside valid bounds."
    ),
}

_STATUS: Final[dict[LongFormMetricsErrorCode, HTTPStatus]] = {
    LongFormMetricsErrorCode.CONTENT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    LongFormMetricsErrorCode.NOT_LONGFORM: HTTPStatus.UNPROCESSABLE_ENTITY,
    LongFormMetricsErrorCode.INVALID_SCOPE: HTTPStatus.UNPROCESSABLE_ENTITY,
    LongFormMetricsErrorCode.INVALID_DURATION: HTTPStatus.UNPROCESSABLE_ENTITY,
    LongFormMetricsErrorCode.INVALID_METRIC_VALUE: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class LongFormMetricsError(DomainError):
    default_code = LongFormMetricsErrorCode.INVALID_SCOPE.value
    default_user_message = USER_MESSAGES[LongFormMetricsErrorCode.INVALID_SCOPE]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: LongFormMetricsErrorCode, detail: str) -> None:
        if not isinstance(code, LongFormMetricsErrorCode):
            raise TypeError("code must be a LongFormMetricsErrorCode")
        self.metrics_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class LongFormMetricsView:
    snapshot_id: str
    youtube_video_id: str
    content_item_id: str | None
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    video_duration_seconds: Decimal | None
    views: Decimal | None
    impressions: Decimal | None
    impressions_click_through_rate: Decimal | None
    estimated_minutes_watched: Decimal | None
    watch_time_hours: Decimal | None
    watch_time_seconds: Decimal | None
    average_view_duration_seconds: Decimal | None
    average_view_duration_minutes: Decimal | None
    average_view_percentage: Decimal | None
    likes: Decimal | None
    comments: Decimal | None
    shares: Decimal | None
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    like_rate: Decimal | None
    comment_rate: Decimal | None
    share_rate: Decimal | None
    subscriber_conversion_rate: Decimal | None
    available_metrics: tuple[str, ...]
    missing_metrics: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "youtube_video_id": self.youtube_video_id,
            "content_item_id": self.content_item_id,
            "source": self.source,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "video_duration_seconds": _str_or_none(self.video_duration_seconds),
            "views": _str_or_none(self.views),
            "impressions": _str_or_none(self.impressions),
            "impressions_click_through_rate": _str_or_none(
                self.impressions_click_through_rate
            ),
            "estimated_minutes_watched": _str_or_none(self.estimated_minutes_watched),
            "watch_time_hours": _str_or_none(self.watch_time_hours),
            "watch_time_seconds": _str_or_none(self.watch_time_seconds),
            "average_view_duration_seconds": _str_or_none(
                self.average_view_duration_seconds
            ),
            "average_view_duration_minutes": _str_or_none(
                self.average_view_duration_minutes
            ),
            "average_view_percentage": _str_or_none(self.average_view_percentage),
            "likes": _str_or_none(self.likes),
            "comments": _str_or_none(self.comments),
            "shares": _str_or_none(self.shares),
            "subscribers_gained": _str_or_none(self.subscribers_gained),
            "subscribers_lost": _str_or_none(self.subscribers_lost),
            "like_rate": _str_or_none(self.like_rate),
            "comment_rate": _str_or_none(self.comment_rate),
            "share_rate": _str_or_none(self.share_rate),
            "subscriber_conversion_rate": _str_or_none(self.subscriber_conversion_rate),
            "available_metrics": list(self.available_metrics),
            "missing_metrics": list(self.missing_metrics),
        }


def normalize_longform_metrics(
    snapshot: MetricSnapshot,
    *,
    content_item_id: str | None = None,
    video_duration_seconds: Decimal | int | None = None,
) -> LongFormMetricsView:
    """Normalise one video ``MetricSnapshot`` into a ``LongFormMetricsView``."""
    if not isinstance(snapshot, MetricSnapshot):
        raise TypeError("snapshot must be a MetricSnapshot")
    if snapshot.scope is not MetricScope.VIDEO:
        raise LongFormMetricsError(
            LongFormMetricsErrorCode.INVALID_SCOPE,
            f"snapshot {snapshot.id} has scope {snapshot.scope.value}, expected video",
        )
    duration = _validate_longform_duration(video_duration_seconds)
    metrics = snapshot.metrics
    for name, value in metrics.items():
        if name in _NON_NEGATIVE_METRICS and value < 0:
            raise LongFormMetricsError(
                LongFormMetricsErrorCode.INVALID_METRIC_VALUE,
                f"metric {name!r} must not be negative (got {value})",
            )

    views = metrics.get("views")
    impressions = metrics.get("impressions")
    ctr = metrics.get("impressions_click_through_rate")
    minutes_watched = metrics.get("estimated_minutes_watched")
    avg_duration = metrics.get("average_view_duration")
    avg_percentage = metrics.get("average_view_percentage")
    likes = metrics.get("likes")
    comments = metrics.get("comments")
    shares = metrics.get("shares")
    subs_gained = metrics.get("subscribers_gained")
    subs_lost = metrics.get("subscribers_lost")

    watch_time_hours = (
        _quantize(minutes_watched / MINUTES_PER_HOUR)
        if minutes_watched is not None
        else None
    )
    watch_time_seconds = (
        _quantize(minutes_watched * SECONDS_PER_MINUTE)
        if minutes_watched is not None
        else None
    )
    avg_duration_minutes = (
        _quantize(avg_duration / SECONDS_PER_MINUTE)
        if avg_duration is not None
        else None
    )

    like_rate = _rate_per_100(likes, views)
    comment_rate = _rate_per_100(comments, views)
    share_rate = _rate_per_100(shares, views)
    sub_conversion_rate = _rate_per_100(subs_gained, views)

    available = tuple(name for name in LONGFORM_METRICS_CATALOGUE if name in metrics)
    missing = tuple(name for name in LONGFORM_METRICS_CATALOGUE if name not in metrics)

    return LongFormMetricsView(
        snapshot_id=snapshot.id,
        youtube_video_id=snapshot.subject_id,
        content_item_id=content_item_id,
        source=snapshot.source,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        retrieved_at=snapshot.retrieved_at,
        video_duration_seconds=duration,
        views=views,
        impressions=impressions,
        impressions_click_through_rate=ctr,
        estimated_minutes_watched=minutes_watched,
        watch_time_hours=watch_time_hours,
        watch_time_seconds=watch_time_seconds,
        average_view_duration_seconds=avg_duration,
        average_view_duration_minutes=avg_duration_minutes,
        average_view_percentage=avg_percentage,
        likes=likes,
        comments=comments,
        shares=shares,
        subscribers_gained=subs_gained,
        subscribers_lost=subs_lost,
        like_rate=like_rate,
        comment_rate=comment_rate,
        share_rate=share_rate,
        subscriber_conversion_rate=sub_conversion_rate,
        available_metrics=available,
        missing_metrics=missing,
    )


class LongFormMetricsService:
    def __init__(
        self,
        database: Database,
        results: PublishResultService,
    ) -> None:
        self._database = database
        self._results = results

    def normalize_for_item(
        self,
        content_item_id: str,
        *,
        video_duration_seconds: Decimal | int | None = None,
    ) -> list[LongFormMetricsView]:
        """Normalise all stored video snapshots of a published LongForm content item."""
        connection = self._database.connect()
        try:
            item = ContentItemRepository(connection).get(content_item_id)
        finally:
            connection.close()
        if item is None:
            raise LongFormMetricsError(
                LongFormMetricsErrorCode.CONTENT_NOT_FOUND,
                f"content item {content_item_id} was not found",
            )
        if item.content_type is not ContentType.LONGFORM:
            raise LongFormMetricsError(
                LongFormMetricsErrorCode.NOT_LONGFORM,
                f"content item {item.id} is {item.content_type.value}, not longform",
            )
        record = self._results.for_content_item(item.id)
        snapshots = self._list_video_snapshots(record.youtube_video_id)
        return [
            normalize_longform_metrics(
                snapshot,
                content_item_id=item.id,
                video_duration_seconds=video_duration_seconds,
            )
            for snapshot in snapshots
        ]

    def latest_for_item(
        self,
        content_item_id: str,
        *,
        video_duration_seconds: Decimal | int | None = None,
    ) -> LongFormMetricsView | None:
        views = self.normalize_for_item(
            content_item_id, video_duration_seconds=video_duration_seconds
        )
        return views[-1] if views else None

    def normalize_for_video(
        self,
        youtube_video_id: str,
        *,
        video_duration_seconds: Decimal | int | None = None,
    ) -> list[LongFormMetricsView]:
        """Normalise stored snapshots of one YouTube video id, verifying it is not
        Shorts.
        """
        if not isinstance(youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            youtube_video_id
        ):
            raise ValueError(f"{youtube_video_id!r} is not a YouTube video id")
        item_id = self._resolve_longform_item_id(youtube_video_id)
        snapshots = self._list_video_snapshots(youtube_video_id)
        return [
            normalize_longform_metrics(
                snapshot,
                content_item_id=item_id,
                video_duration_seconds=video_duration_seconds,
            )
            for snapshot in snapshots
        ]

    def _resolve_longform_item_id(self, youtube_video_id: str) -> str | None:
        connection = self._database.connect()
        try:
            jobs = PublishJobRepository(connection).list_by_youtube_video_id(
                youtube_video_id
            )
            succeeded = [job for job in jobs if job.status is PublishStatus.SUCCEEDED]
            if not succeeded:
                return None
            job = succeeded[-1]
            if job.content_type is not ContentType.LONGFORM:
                raise LongFormMetricsError(
                    LongFormMetricsErrorCode.NOT_LONGFORM,
                    f"video {youtube_video_id} was published as "
                    f"{job.content_type.value}",
                )
            return job.content_item_id
        finally:
            connection.close()

    def _list_video_snapshots(self, youtube_video_id: str) -> list[MetricSnapshot]:
        connection = self._database.connect()
        try:
            return MetricSnapshotRepository(connection).list_by_subject(
                MetricScope.VIDEO, youtube_video_id
            )
        finally:
            connection.close()


def _validate_longform_duration(
    duration: Decimal | int | None,
) -> Decimal | None:
    if duration is None:
        return None
    if isinstance(duration, bool) or not isinstance(duration, (int, Decimal)):
        raise TypeError("video_duration_seconds must be a Decimal, int or None")
    value = Decimal(duration)
    if (
        not value.is_finite()
        or value < LONGFORM_MIN_DURATION_SECONDS
        or value > LONGFORM_MAX_DURATION_SECONDS
    ):
        raise LongFormMetricsError(
            LongFormMetricsErrorCode.INVALID_DURATION,
            f"video_duration_seconds {value} must be in "
            f"[{LONGFORM_MIN_DURATION_SECONDS}, {LONGFORM_MAX_DURATION_SECONDS}]",
        )
    return value


def _rate_per_100(
    numerator: Decimal | None, denominator: Decimal | None
) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    if numerator < 0:
        return None
    return _quantize(numerator * HUNDRED / denominator)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _str_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
