"""Shorts metrics normalisation (Prompt Pack v8, prompt #161), context C13 Analytics.

Normalises per-video ``MetricSnapshot`` records for YouTube Shorts without ever
inventing or zero-filling absent metrics:

- ``normalize_shorts_metrics`` validates that a snapshot has ``MetricScope.VIDEO``
  and extracts standard video metrics alongside Shorts-specific metrics
  (``engaged_views``, ``swiped_away``, ``shorts_feed_impressions``) when present.
- Absent metrics stay ``None`` (never ``0``) and are listed in ``missing_metrics``.
- Derived Shorts ratios are calculated **only** when their required inputs are
  present and positive:
  - ``stay_to_watch_rate``: ``engaged_views / (engaged_views + swiped_away) * 100``
    when both ``engaged_views`` and ``swiped_away`` are present and their sum > 0.
  - ``loop_rate``: ``average_view_duration_seconds / video_duration_seconds`` when
    both ``average_view_duration`` and a valid Shorts ``video_duration_seconds``
    (``0 < duration <= 180``) are available.
  - ``like_rate`` and ``share_rate``: interactions per 100 views when ``views > 0``
    and the corresponding interaction metric is present.
- ``ShortsMetricsService`` loads stored snapshots for a published ``ContentType.SHORTS``
  item or video id, refusing ``ContentType.LONGFORM`` items with ``ShortsMetricsError``.
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

SHORTS_MAX_DURATION_SECONDS: Final[Decimal] = Decimal("180")
SECONDS_PER_MINUTE: Final[Decimal] = Decimal("60")
HUNDRED: Final[Decimal] = Decimal("100")
TWO_PLACES: Final[Decimal] = Decimal("0.01")

SHORTS_SPECIFIC_METRICS: Final[tuple[str, ...]] = (
    "engaged_views",
    "swiped_away",
    "shorts_feed_impressions",
)
SHORTS_METRICS_CATALOGUE: Final[tuple[str, ...]] = (
    *DEFAULT_VIDEO_METRICS,
    "average_view_percentage",
    *SHORTS_SPECIFIC_METRICS,
)
_NON_NEGATIVE_METRICS: Final[frozenset[str]] = frozenset(
    {
        "views",
        "engaged_views",
        "swiped_away",
        "shorts_feed_impressions",
        "estimated_minutes_watched",
        "average_view_duration",
        "average_view_percentage",
        "subscribers_gained",
        "subscribers_lost",
        "impressions",
        "impressions_click_through_rate",
    }
)


class ShortsMetricsErrorCode(StrEnum):
    CONTENT_NOT_FOUND = "domain.shorts_metrics_content_not_found"
    NOT_A_SHORT = "domain.shorts_metrics_not_a_short"
    INVALID_SCOPE = "domain.shorts_metrics_invalid_scope"
    INVALID_DURATION = "domain.shorts_metrics_invalid_duration"
    INVALID_METRIC_VALUE = "domain.shorts_metrics_invalid_value"


USER_MESSAGES: Final[dict[ShortsMetricsErrorCode, str]] = {
    ShortsMetricsErrorCode.CONTENT_NOT_FOUND: (
        "The requested Shorts content was not found."
    ),
    ShortsMetricsErrorCode.NOT_A_SHORT: (
        "Shorts metrics normalisation only applies to Shorts content."
    ),
    ShortsMetricsErrorCode.INVALID_SCOPE: (
        "Shorts metrics require a per-video metric snapshot."
    ),
    ShortsMetricsErrorCode.INVALID_DURATION: (
        "Shorts duration must be above 0 and at most 180 seconds."
    ),
    ShortsMetricsErrorCode.INVALID_METRIC_VALUE: (
        "One or more metric values in the snapshot are outside valid bounds."
    ),
}

_STATUS: Final[dict[ShortsMetricsErrorCode, HTTPStatus]] = {
    ShortsMetricsErrorCode.CONTENT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ShortsMetricsErrorCode.NOT_A_SHORT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ShortsMetricsErrorCode.INVALID_SCOPE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ShortsMetricsErrorCode.INVALID_DURATION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ShortsMetricsErrorCode.INVALID_METRIC_VALUE: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class ShortsMetricsError(DomainError):
    default_code = ShortsMetricsErrorCode.INVALID_SCOPE.value
    default_user_message = USER_MESSAGES[ShortsMetricsErrorCode.INVALID_SCOPE]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: ShortsMetricsErrorCode, detail: str) -> None:
        if not isinstance(code, ShortsMetricsErrorCode):
            raise TypeError("code must be a ShortsMetricsErrorCode")
        self.metrics_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class ShortsMetricsView:
    snapshot_id: str
    youtube_video_id: str
    content_item_id: str | None
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    video_duration_seconds: Decimal | None
    views: Decimal | None
    engaged_views: Decimal | None
    swiped_away: Decimal | None
    shorts_feed_impressions: Decimal | None
    estimated_minutes_watched: Decimal | None
    watch_time_seconds: Decimal | None
    average_view_duration_seconds: Decimal | None
    average_view_percentage: Decimal | None
    likes: Decimal | None
    comments: Decimal | None
    shares: Decimal | None
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    stay_to_watch_rate: Decimal | None
    loop_rate: Decimal | None
    like_rate: Decimal | None
    share_rate: Decimal | None
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
            "engaged_views": _str_or_none(self.engaged_views),
            "swiped_away": _str_or_none(self.swiped_away),
            "shorts_feed_impressions": _str_or_none(self.shorts_feed_impressions),
            "estimated_minutes_watched": _str_or_none(self.estimated_minutes_watched),
            "watch_time_seconds": _str_or_none(self.watch_time_seconds),
            "average_view_duration_seconds": _str_or_none(
                self.average_view_duration_seconds
            ),
            "average_view_percentage": _str_or_none(self.average_view_percentage),
            "likes": _str_or_none(self.likes),
            "comments": _str_or_none(self.comments),
            "shares": _str_or_none(self.shares),
            "subscribers_gained": _str_or_none(self.subscribers_gained),
            "subscribers_lost": _str_or_none(self.subscribers_lost),
            "stay_to_watch_rate": _str_or_none(self.stay_to_watch_rate),
            "loop_rate": _str_or_none(self.loop_rate),
            "like_rate": _str_or_none(self.like_rate),
            "share_rate": _str_or_none(self.share_rate),
            "available_metrics": list(self.available_metrics),
            "missing_metrics": list(self.missing_metrics),
        }


def normalize_shorts_metrics(
    snapshot: MetricSnapshot,
    *,
    content_item_id: str | None = None,
    video_duration_seconds: Decimal | int | None = None,
) -> ShortsMetricsView:
    """Normalise one video ``MetricSnapshot`` into a ``ShortsMetricsView``."""
    if not isinstance(snapshot, MetricSnapshot):
        raise TypeError("snapshot must be a MetricSnapshot")
    if snapshot.scope is not MetricScope.VIDEO:
        raise ShortsMetricsError(
            ShortsMetricsErrorCode.INVALID_SCOPE,
            f"snapshot {snapshot.id} has scope {snapshot.scope.value}, expected video",
        )
    duration = _validate_shorts_duration(video_duration_seconds)
    metrics = snapshot.metrics
    for name, value in metrics.items():
        if name in _NON_NEGATIVE_METRICS and value < 0:
            raise ShortsMetricsError(
                ShortsMetricsErrorCode.INVALID_METRIC_VALUE,
                f"metric {name!r} must not be negative (got {value})",
            )

    views = metrics.get("views")
    engaged_views = metrics.get("engaged_views")
    swiped_away = metrics.get("swiped_away")
    shorts_feed_impressions = metrics.get("shorts_feed_impressions")
    minutes_watched = metrics.get("estimated_minutes_watched")
    avg_duration = metrics.get("average_view_duration")
    avg_percentage = metrics.get("average_view_percentage")
    likes = metrics.get("likes")
    comments = metrics.get("comments")
    shares = metrics.get("shares")
    subs_gained = metrics.get("subscribers_gained")
    subs_lost = metrics.get("subscribers_lost")

    watch_time_seconds = (
        _quantize(minutes_watched * SECONDS_PER_MINUTE)
        if minutes_watched is not None
        else None
    )
    stay_to_watch_rate: Decimal | None = None
    if engaged_views is not None and swiped_away is not None:
        feed_total = engaged_views + swiped_away
        if feed_total > 0:
            stay_to_watch_rate = _quantize(engaged_views * HUNDRED / feed_total)

    loop_rate: Decimal | None = None
    if avg_duration is not None and duration is not None and duration > 0:
        loop_rate = _quantize(avg_duration / duration)

    like_rate = _rate_per_100(likes, views)
    share_rate = _rate_per_100(shares, views)

    available = tuple(name for name in SHORTS_METRICS_CATALOGUE if name in metrics)
    missing = tuple(name for name in SHORTS_METRICS_CATALOGUE if name not in metrics)

    return ShortsMetricsView(
        snapshot_id=snapshot.id,
        youtube_video_id=snapshot.subject_id,
        content_item_id=content_item_id,
        source=snapshot.source,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        retrieved_at=snapshot.retrieved_at,
        video_duration_seconds=duration,
        views=views,
        engaged_views=engaged_views,
        swiped_away=swiped_away,
        shorts_feed_impressions=shorts_feed_impressions,
        estimated_minutes_watched=minutes_watched,
        watch_time_seconds=watch_time_seconds,
        average_view_duration_seconds=avg_duration,
        average_view_percentage=avg_percentage,
        likes=likes,
        comments=comments,
        shares=shares,
        subscribers_gained=subs_gained,
        subscribers_lost=subs_lost,
        stay_to_watch_rate=stay_to_watch_rate,
        loop_rate=loop_rate,
        like_rate=like_rate,
        share_rate=share_rate,
        available_metrics=available,
        missing_metrics=missing,
    )


class ShortsMetricsService:
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
    ) -> list[ShortsMetricsView]:
        """Normalise all stored video snapshots of a published Shorts content item."""
        connection = self._database.connect()
        try:
            item = ContentItemRepository(connection).get(content_item_id)
        finally:
            connection.close()
        if item is None:
            raise ShortsMetricsError(
                ShortsMetricsErrorCode.CONTENT_NOT_FOUND,
                f"content item {content_item_id} was not found",
            )
        if item.content_type is not ContentType.SHORTS:
            raise ShortsMetricsError(
                ShortsMetricsErrorCode.NOT_A_SHORT,
                f"content item {item.id} is {item.content_type.value}, not shorts",
            )
        record = self._results.for_content_item(item.id)
        snapshots = self._list_video_snapshots(record.youtube_video_id)
        return [
            normalize_shorts_metrics(
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
    ) -> ShortsMetricsView | None:
        views = self.normalize_for_item(
            content_item_id, video_duration_seconds=video_duration_seconds
        )
        return views[-1] if views else None

    def normalize_for_video(
        self,
        youtube_video_id: str,
        *,
        video_duration_seconds: Decimal | int | None = None,
    ) -> list[ShortsMetricsView]:
        """Normalise stored snapshots of one YouTube video id.

        Verifies it is not LongForm.
        """
        if not isinstance(youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            youtube_video_id
        ):
            raise ValueError(f"{youtube_video_id!r} is not a YouTube video id")
        item_id = self._resolve_shorts_item_id(youtube_video_id)
        snapshots = self._list_video_snapshots(youtube_video_id)
        return [
            normalize_shorts_metrics(
                snapshot,
                content_item_id=item_id,
                video_duration_seconds=video_duration_seconds,
            )
            for snapshot in snapshots
        ]

    def _resolve_shorts_item_id(self, youtube_video_id: str) -> str | None:
        connection = self._database.connect()
        try:
            jobs = PublishJobRepository(connection).list_by_youtube_video_id(
                youtube_video_id
            )
            succeeded = [job for job in jobs if job.status is PublishStatus.SUCCEEDED]
            if not succeeded:
                return None
            job = succeeded[-1]
            if job.content_type is not ContentType.SHORTS:
                raise ShortsMetricsError(
                    ShortsMetricsErrorCode.NOT_A_SHORT,
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


def _validate_shorts_duration(
    duration: Decimal | int | None,
) -> Decimal | None:
    if duration is None:
        return None
    if isinstance(duration, bool) or not isinstance(duration, (int, Decimal)):
        raise TypeError("video_duration_seconds must be a Decimal, int or None")
    value = Decimal(duration)
    if not value.is_finite() or value <= 0 or value > SHORTS_MAX_DURATION_SECONDS:
        raise ShortsMetricsError(
            ShortsMetricsErrorCode.INVALID_DURATION,
            f"video_duration_seconds {value} must be in "
            f"(0, {SHORTS_MAX_DURATION_SECONDS}]",
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
