"""Retention metrics sync and extraction (Prompt Pack v8, prompt #164), context C13.

Stores and reads audience retention metrics **only when actually available** from the
analytics source, never defaulting to ``0`` or fabricating numbers from duration:

- ``RETENTION_METRICS`` asks the ``AnalyticsProvider`` for ``average_view_percentage``
  and ``average_view_duration``.
- ``extract_retention`` inspects a ``MetricSnapshot`` and returns a ``RetentionView``
  whose ``average_view_percentage`` and ``average_view_duration_seconds`` are ``None``
  whenever the snapshot lacks them. ``available`` is ``True`` only when
  ``average_view_percentage`` is present.
- ``RetentionSync.sync`` reads retention for a channel's published videos over an
  ended UTC period:
  - A video for which the source does not return ``average_view_percentage`` gets **no
    new snapshot** and is listed in ``unavailable_video_ids``.
  - When ``average_view_percentage`` is present (and within ``[0, 1000]``), a
    ``MetricSnapshot`` is stored (merging with the newest snapshot of the same video,
    source and period when one exists, or returning it unchanged when identical).
  - ``analytics.retention_synced`` is audited with counts only.
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
from ai_youtube_agent.content.channel_metrics import (
    CHANNEL_ENTITY,
    DEFAULT_SYNC_RETRY,
    MetricsSyncError,
    MetricsSyncErrorCode,
    load_syncable_channel,
    require_ended_period,
)
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish import VIDEO_ID_PATTERN
from ai_youtube_agent.pipeline.publish_result import (
    PublishResultRecord,
    PublishResultService,
)
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy, call_with_retry
from ai_youtube_agent.providers.analytics import (
    MAX_VIDEO_IDS,
    AnalyticsPage,
    AnalyticsProvider,
    AnalyticsQuery,
)
from ai_youtube_agent.providers.youtube_oauth import OAuthScope, YouTubeOAuthProvider

Clock = Callable[[], datetime]

RETENTION_SYNCED: Final[str] = "analytics.retention_synced"
MAX_PAGES_PER_CHUNK: Final[int] = 20
MAX_RETENTION_PERCENTAGE: Final[Decimal] = Decimal("1000")
RETENTION_METRICS: Final[tuple[str, ...]] = (
    "average_view_percentage",
    "average_view_duration",
)


class RetentionErrorCode(StrEnum):
    INVALID_RETENTION_VALUE = "domain.retention_invalid_value"


USER_MESSAGES: Final[dict[RetentionErrorCode, str]] = {
    RetentionErrorCode.INVALID_RETENTION_VALUE: (
        "The retention metric value is outside the valid range."
    ),
}


class RetentionError(DomainError):
    default_code = RetentionErrorCode.INVALID_RETENTION_VALUE.value
    default_user_message = USER_MESSAGES[RetentionErrorCode.INVALID_RETENTION_VALUE]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: RetentionErrorCode, detail: str) -> None:
        if not isinstance(code, RetentionErrorCode):
            raise TypeError("code must be a RetentionErrorCode")
        self.retention_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])


@dataclass(frozen=True)
class RetentionView:
    snapshot_id: str
    scope: MetricScope
    subject_id: str
    source: str
    period_start: datetime
    period_end: datetime
    retrieved_at: datetime
    average_view_percentage: Decimal | None
    average_view_duration_seconds: Decimal | None

    @property
    def available(self) -> bool:
        return self.average_view_percentage is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "source": self.source,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "available": self.available,
            "average_view_percentage": (
                str(self.average_view_percentage)
                if self.average_view_percentage is not None
                else None
            ),
            "average_view_duration_seconds": (
                str(self.average_view_duration_seconds)
                if self.average_view_duration_seconds is not None
                else None
            ),
        }


def validate_retention_values(
    percentage: Decimal | None, duration_seconds: Decimal | None
) -> None:
    """Ensure retention numbers are within valid bounds when present."""
    if percentage is not None and (
        percentage < 0 or percentage > MAX_RETENTION_PERCENTAGE
    ):
        raise RetentionError(
            RetentionErrorCode.INVALID_RETENTION_VALUE,
            f"average_view_percentage {percentage} must be in [0, {MAX_RETENTION_PERCENTAGE}]",
        )
    if duration_seconds is not None and duration_seconds < 0:
        raise RetentionError(
            RetentionErrorCode.INVALID_RETENTION_VALUE,
            f"average_view_duration {duration_seconds} must not be negative",
        )


def extract_retention(snapshot: MetricSnapshot) -> RetentionView:
    """Extract retention metrics from a ``MetricSnapshot`` without inventing values."""
    if not isinstance(snapshot, MetricSnapshot):
        raise TypeError("snapshot must be a MetricSnapshot")
    pct = snapshot.metrics.get("average_view_percentage")
    dur = snapshot.metrics.get("average_view_duration")
    validate_retention_values(pct, dur)
    return RetentionView(
        snapshot_id=snapshot.id,
        scope=snapshot.scope,
        subject_id=snapshot.subject_id,
        source=snapshot.source,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        retrieved_at=snapshot.retrieved_at,
        average_view_percentage=pct,
        average_view_duration_seconds=dur,
    )


@dataclass(frozen=True)
class RetentionOutcome:
    youtube_video_id: str
    snapshot: MetricSnapshot
    retention: RetentionView
    created: bool


@dataclass(frozen=True)
class RetentionSyncResult:
    channel_id: str
    requested_video_ids: tuple[str, ...]
    skipped_video_ids: tuple[str, ...]
    outcomes: tuple[RetentionOutcome, ...]
    unavailable_video_ids: tuple[str, ...]
    pages: int
    data_through: datetime | None
    complete: bool

    @property
    def created_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.created)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "requested_video_ids": list(self.requested_video_ids),
            "skipped_video_ids": list(self.skipped_video_ids),
            "outcomes": [
                {
                    "youtube_video_id": outcome.youtube_video_id,
                    "snapshot": outcome.snapshot.as_dict(),
                    "retention": outcome.retention.as_dict(),
                    "created": outcome.created,
                }
                for outcome in self.outcomes
            ],
            "unavailable_video_ids": list(self.unavailable_video_ids),
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
            "pages": self.pages,
            "data_through": (
                self.data_through.isoformat() if self.data_through else None
            ),
            "complete": self.complete,
        }


class RetentionSync:
    def __init__(
        self,
        database: Database,
        provider: AnalyticsProvider,
        oauth_provider: YouTubeOAuthProvider,
        results: PublishResultService,
        audit_log: AuditLog,
        *,
        retry_policy: UploadRetryPolicy = DEFAULT_SYNC_RETRY,
        page_size: int = MAX_VIDEO_IDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Clock | None = None,
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_VIDEO_IDS:
            raise ValueError(
                f"page_size must be a whole number from 1 to {MAX_VIDEO_IDS}"
            )
        self._page_size = page_size
        self._database = database
        self._provider = provider
        self._oauth = oauth_provider
        self._results = results
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
        video_ids: Sequence[str] | None = None,
    ) -> RetentionSyncResult:
        now = self._now()
        require_ended_period(period_end, now)
        channel = load_syncable_channel(self._database, channel_id)
        published = {
            record.youtube_video_id: record
            for record in self._results.list_for_channel(channel.id)
        }
        wanted = self._select(published, video_ids)
        eligible = tuple(
            vid for vid in wanted if published[vid].published_at < period_end
        )
        skipped = tuple(vid for vid in wanted if vid not in eligible)

        rows: dict[str, dict[str, Decimal]] = {}
        pages: list[AnalyticsPage] = []
        if eligible:
            token = self._oauth.get_token(
                channel.id, required_scopes=(OAuthScope.YT_ANALYTICS_READONLY,)
            )
            for start in range(0, len(eligible), MAX_VIDEO_IDS):
                chunk = eligible[start : start + MAX_VIDEO_IDS]
                self._read_chunk(
                    channel.youtube.channel_id,
                    chunk,
                    period_start,
                    period_end,
                    token,
                    rows,
                    pages,
                )

        outcomes, unavailable = self._store(eligible, rows, pages)
        through = _earliest(page.data_through for page in pages) if pages else None
        complete = bool(pages) and all(
            page.data_through is not None and page.data_through >= period_end
            for page in pages
        )
        result = RetentionSyncResult(
            channel_id=channel.id,
            requested_video_ids=eligible,
            skipped_video_ids=skipped,
            outcomes=outcomes,
            unavailable_video_ids=unavailable,
            pages=len(pages),
            data_through=through,
            complete=complete,
        )
        self._audit.record(
            RETENTION_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "source": pages[0].source if pages else None,
                "requested_count": len(eligible),
                "skipped_count": len(skipped),
                "available_count": len(outcomes),
                "unavailable_count": len(unavailable),
                "created_count": result.created_count,
                "unchanged_count": result.unchanged_count,
                "pages": len(pages),
                "complete": complete,
            },
        )
        return result

    def for_video(self, youtube_video_id: str) -> list[RetentionView]:
        """Retention views from stored snapshots of one video that actually have retention."""
        if not isinstance(youtube_video_id, str) or not VIDEO_ID_PATTERN.match(
            youtube_video_id
        ):
            raise ValueError(f"{youtube_video_id!r} is not a YouTube video id")
        connection = self._database.connect()
        try:
            snapshots = MetricSnapshotRepository(connection).list_by_subject(
                MetricScope.VIDEO, youtube_video_id
            )
        finally:
            connection.close()
        views = [extract_retention(snapshot) for snapshot in snapshots]
        return [view for view in views if view.available]

    def latest_for_video(self, youtube_video_id: str) -> RetentionView | None:
        views = self.for_video(youtube_video_id)
        return views[-1] if views else None

    @staticmethod
    def _select(
        published: dict[str, PublishResultRecord], video_ids: Sequence[str] | None
    ) -> tuple[str, ...]:
        if video_ids is None:
            return tuple(published)
        chosen = tuple(dict.fromkeys(video_ids))
        for video_id in chosen:
            if video_id not in published:
                raise MetricsSyncError(
                    MetricsSyncErrorCode.VIDEO_NOT_PUBLISHED,
                    f"video {video_id} was not published by this channel",
                )
        return chosen

    def _read_chunk(
        self,
        youtube_channel_id: str,
        chunk: tuple[str, ...],
        period_start: datetime,
        period_end: datetime,
        token: Any,
        rows: dict[str, dict[str, Decimal]],
        pages: list[AnalyticsPage],
    ) -> None:
        page_token: str | None = None
        for _ in range(MAX_PAGES_PER_CHUNK):
            query = AnalyticsQuery(
                youtube_channel_id=youtube_channel_id,
                scope=MetricScope.VIDEO,
                period_start=period_start,
                period_end=period_end,
                metrics=RETENTION_METRICS,
                video_ids=chunk,
                page_size=min(self._page_size, len(chunk)),
                page_token=page_token,
            )
            page = call_with_retry(
                lambda q=query: self._provider.query(q, token=token),
                self._retry,
                sleep=self._sleep,
            )
            pages.append(page)
            for row in page.rows:
                if row.subject_id in chunk:
                    rows[row.subject_id] = dict(row.metrics)
            if page.next_page_token is None:
                return
            page_token = page.next_page_token
        raise MetricsSyncError(
            MetricsSyncErrorCode.TOO_MANY_PAGES,
            f"more than {MAX_PAGES_PER_CHUNK} pages for {len(chunk)} videos",
        )

    def _store(
        self,
        eligible: tuple[str, ...],
        rows: dict[str, dict[str, Decimal]],
        pages: list[AnalyticsPage],
    ) -> tuple[tuple[RetentionOutcome, ...], tuple[str, ...]]:
        if not pages:
            return (), ()
        first = pages[0]
        retrieved_at = max(page.retrieved_at for page in pages)
        outcomes: list[RetentionOutcome] = []
        unavailable: list[str] = []

        # Validate all returned retention values before opening the write transaction.
        for video_id in eligible:
            values = rows.get(video_id)
            if values and "average_view_percentage" in values:
                validate_retention_values(
                    values.get("average_view_percentage"),
                    values.get("average_view_duration"),
                )

        with self._database.transaction() as connection:
            repository = MetricSnapshotRepository(connection)
            for video_id in eligible:
                values = rows.get(video_id)
                # Store retention ONLY when average_view_percentage is actually available.
                if not values or "average_view_percentage" not in values:
                    unavailable.append(video_id)
                    continue
                same_period = [
                    stored
                    for stored in repository.list_by_subject(
                        MetricScope.VIDEO, video_id
                    )
                    if stored.source == first.source
                    and stored.period_start == first.period_start
                    and stored.period_end == first.period_end
                ]
                merged = (
                    {**dict(same_period[-1].metrics), **values}
                    if same_period
                    else dict(values)
                )
                if same_period and dict(same_period[-1].metrics) == merged:
                    existing = same_period[-1]
                    outcomes.append(
                        RetentionOutcome(
                            video_id,
                            existing,
                            extract_retention(existing),
                            False,
                        )
                    )
                    continue
                snapshot = MetricSnapshot.create(
                    MetricScope.VIDEO,
                    video_id,
                    source=first.source,
                    period_start=first.period_start,
                    period_end=first.period_end,
                    metrics=merged,
                    clock=lambda: retrieved_at,
                )
                repository.add(snapshot)
                outcomes.append(
                    RetentionOutcome(
                        video_id,
                        snapshot,
                        extract_retention(snapshot),
                        True,
                    )
                )
        return tuple(outcomes), tuple(unavailable)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _earliest(moments: Any) -> datetime | None:
    values = list(moments)
    if any(value is None for value in values):
        return None
    return min(values) if values else None
