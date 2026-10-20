"""Estimated revenue sync and queries (Prompt Pack v8, prompt #171), context C14 Economics.

``EstimatedRevenueSync`` reads **estimated** revenue figures (``RevenueStage.ESTIMATED``)
from a ``RevenueProvider`` (#170) and stores them as ``RevenueRecord`` rows (#024)
strictly separate from finalized revenue (``RevenueStage.FINAL``, #172):

- Channel must exist and be ``ACTIVE`` or ``PAUSED`` (``load_syncable_channel``).
- Requires a valid OAuth token carrying ``OAuthScope.YT_ANALYTICS_MONETARY_READONLY``.
- Period must be a valid UTC half-open range ``[period_start, period_end)`` that has
  started (``period_start <= now``).
- Queries ``RevenueProvider`` with ``stage=RevenueStage.ESTIMATED`` only. Retryable
  provider errors are retried via ``call_with_retry``.
- Never invents zero-value rows: a requested ``revenue_type`` absent from the provider
  response produces no ``RevenueRecord`` and is reported in ``missing_revenue_types``
  (for channel sync) or ``no_data_video_ids`` (for video sync).
- Idempotent per ``(scope, subject_id, source, revenue_type, period_start, period_end)``
  at ``RevenueStage.ESTIMATED``: when the latest stored ``ESTIMATED`` record for that
  stream and period has the same ``amount`` and ``currency``, it is reused with
  ``created=False``; when the estimate changes, a new ``ESTIMATED`` record is appended
  so estimate revision history is preserved.
- Existing ``RevenueStage.FINAL`` records are never modified, overwritten, or returned
  by estimated revenue queries.
- Audits ``revenue.estimated_channel_synced`` and ``revenue.estimated_videos_synced``
  after commit with counts and freshness flags only (never raw monetary amounts).
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.channel_metrics import (
    CHANNEL_ENTITY,
    DEFAULT_SYNC_RETRY,
    MetricsSyncError,
    MetricsSyncErrorCode,
    load_syncable_channel,
)
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage
from ai_youtube_agent.content.strategy import RevenueSource
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.economics import RevenueRecordRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.publish_result import (
    PublishResultRecord,
    PublishResultService,
)
from ai_youtube_agent.pipeline.upload_retry import UploadRetryPolicy, call_with_retry
from ai_youtube_agent.providers.revenue import (
    DEFAULT_REVENUE_TYPES,
    MAX_VIDEO_IDS,
    RevenuePage,
    RevenueProvider,
    RevenueQuery,
    RevenueRow,
)
from ai_youtube_agent.providers.youtube_oauth import OAuthScope, YouTubeOAuthProvider

Clock = Callable[[], datetime]

ESTIMATED_CHANNEL_SYNCED: Final[str] = "revenue.estimated_channel_synced"
ESTIMATED_VIDEOS_SYNCED: Final[str] = "revenue.estimated_videos_synced"
MAX_PAGES_PER_CHUNK: Final[int] = 20
DEFAULT_ESTIMATED_REVENUE_TYPES: Final[tuple[str, ...]] = (
    RevenueSource.ADS.value,
    RevenueSource.SHORTS_ADS.value,
)


class EstimatedRevenueErrorCode(StrEnum):
    PERIOD_NOT_STARTED = "domain.estimated_revenue_period_not_started"
    STAGE_MISMATCH = "domain.estimated_revenue_stage_mismatch"


USER_MESSAGES: Final[dict[EstimatedRevenueErrorCode, str]] = {
    EstimatedRevenueErrorCode.PERIOD_NOT_STARTED: (
        "Estimated revenue can only be read for a period that has started."
    ),
    EstimatedRevenueErrorCode.STAGE_MISMATCH: (
        "The revenue source returned non-estimated rows for an estimated revenue query."
    ),
}

_STATUS: Final[dict[EstimatedRevenueErrorCode, HTTPStatus]] = {
    EstimatedRevenueErrorCode.PERIOD_NOT_STARTED: HTTPStatus.UNPROCESSABLE_ENTITY,
    EstimatedRevenueErrorCode.STAGE_MISMATCH: HTTPStatus.BAD_GATEWAY,
}


class EstimatedRevenueError(DomainError):
    default_code = EstimatedRevenueErrorCode.PERIOD_NOT_STARTED.value
    default_user_message = USER_MESSAGES[EstimatedRevenueErrorCode.PERIOD_NOT_STARTED]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: EstimatedRevenueErrorCode, detail: str) -> None:
        if not isinstance(code, EstimatedRevenueErrorCode):
            raise TypeError("code must be an EstimatedRevenueErrorCode")
        self.revenue_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class EstimatedRecordOutcome:
    record: RevenueRecord
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.as_dict(),
            "created": self.created,
        }


@dataclass(frozen=True)
class ChannelEstimatedRevenueResult:
    channel_id: str
    outcomes: tuple[EstimatedRecordOutcome, ...]
    requested_revenue_types: tuple[str, ...]
    missing_revenue_types: tuple[str, ...]
    data_through: datetime | None
    complete: bool

    @property
    def stage(self) -> RevenueStage:
        return RevenueStage.ESTIMATED

    @property
    def is_final(self) -> bool:
        return False

    @property
    def records(self) -> tuple[RevenueRecord, ...]:
        return tuple(outcome.record for outcome in self.outcomes)

    @property
    def created_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.created)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "stage": self.stage.value,
            "is_final": False,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "requested_revenue_types": list(self.requested_revenue_types),
            "missing_revenue_types": list(self.missing_revenue_types),
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
            "data_through": self.data_through.isoformat() if self.data_through else None,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class VideoEstimatedRevenueResult:
    channel_id: str
    requested_video_ids: tuple[str, ...]
    skipped_video_ids: tuple[str, ...]
    outcomes: tuple[EstimatedRecordOutcome, ...]
    no_data_video_ids: tuple[str, ...]
    requested_revenue_types: tuple[str, ...]
    pages: int
    data_through: datetime | None
    complete: bool

    @property
    def stage(self) -> RevenueStage:
        return RevenueStage.ESTIMATED

    @property
    def is_final(self) -> bool:
        return False

    @property
    def records(self) -> tuple[RevenueRecord, ...]:
        return tuple(outcome.record for outcome in self.outcomes)

    @property
    def created_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.created)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "stage": self.stage.value,
            "is_final": False,
            "requested_video_ids": list(self.requested_video_ids),
            "skipped_video_ids": list(self.skipped_video_ids),
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "no_data_video_ids": list(self.no_data_video_ids),
            "requested_revenue_types": list(self.requested_revenue_types),
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
            "pages": self.pages,
            "data_through": self.data_through.isoformat() if self.data_through else None,
            "complete": self.complete,
        }


class EstimatedRevenueSync:
    """Syncs and queries ``RevenueStage.ESTIMATED`` records for channels and videos."""

    def __init__(
        self,
        database: Database,
        provider: RevenueProvider,
        oauth_provider: YouTubeOAuthProvider,
        audit_log: AuditLog,
        *,
        results: PublishResultService | None = None,
        retry_policy: UploadRetryPolicy = DEFAULT_SYNC_RETRY,
        page_size: int = MAX_VIDEO_IDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Clock | None = None,
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_VIDEO_IDS:
            raise ValueError(f"page_size must be a whole number from 1 to {MAX_VIDEO_IDS}")
        self._database = database
        self._provider = provider
        self._oauth = oauth_provider
        self._results = results or PublishResultService(database)
        self._audit = audit_log
        self._retry = retry_policy
        self._page_size = page_size
        self._sleep = sleep
        self._clock = clock

    def sync_channel(
        self,
        channel_id: str,
        *,
        period_start: datetime,
        period_end: datetime,
        actor: Actor,
        revenue_types: Sequence[str] = DEFAULT_REVENUE_TYPES,
    ) -> ChannelEstimatedRevenueResult:
        """Sync channel-level estimated revenue for ``[period_start, period_end)``."""
        now = self._now()
        require_started_period(period_start, now)
        channel = load_syncable_channel(self._database, channel_id)
        requested = tuple(revenue_types)
        query = RevenueQuery(
            youtube_channel_id=channel.youtube.channel_id,
            stage=RevenueStage.ESTIMATED,
            scope=MetricScope.CHANNEL,
            period_start=period_start,
            period_end=period_end,
            revenue_types=requested,
        )
        token = self._oauth.get_token(
            channel.id, required_scopes=(OAuthScope.YT_ANALYTICS_MONETARY_READONLY,)
        )
        page = call_with_retry(
            lambda: self._provider.query(query, token=token),
            self._retry,
            sleep=self._sleep,
        )
        _require_estimated_page(page)
        channel_rows = [
            row
            for row in page.rows
            if row.subject_id == channel.youtube.channel_id
            and row.revenue_type in requested
        ]
        found_types = {row.revenue_type for row in channel_rows}
        missing = tuple(rtype for rtype in requested if rtype not in found_types)
        outcomes = self._store_rows(
            MetricScope.CHANNEL,
            rows=[(channel.id, row) for row in channel_rows],
            source=page.source,
            period_start=page.period_start,
            period_end=page.period_end,
            retrieved_at=page.retrieved_at,
        )
        complete = page.data_through is not None and page.data_through >= period_end
        result = ChannelEstimatedRevenueResult(
            channel_id=channel.id,
            outcomes=outcomes,
            requested_revenue_types=requested,
            missing_revenue_types=missing,
            data_through=page.data_through,
            complete=complete,
        )
        self._audit.record(
            ESTIMATED_CHANNEL_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "stage": RevenueStage.ESTIMATED.value,
                "source": page.source,
                "created_count": result.created_count,
                "unchanged_count": result.unchanged_count,
                "missing_count": len(missing),
                "complete": complete,
                "data_through": (
                    page.data_through.isoformat() if page.data_through else None
                ),
            },
        )
        return result

    def sync_videos(
        self,
        channel_id: str,
        *,
        period_start: datetime,
        period_end: datetime,
        actor: Actor,
        video_ids: Sequence[str] | None = None,
        revenue_types: Sequence[str] = DEFAULT_REVENUE_TYPES,
    ) -> VideoEstimatedRevenueResult:
        """Sync per-video estimated revenue for a channel's published videos."""
        now = self._now()
        require_started_period(period_start, now)
        channel = load_syncable_channel(self._database, channel_id)
        requested_types = tuple(revenue_types)

        published = {
            record.youtube_video_id: record
            for record in self._results.list_for_channel(channel.id)
        }
        wanted = _select_published_videos(published, video_ids)
        eligible = tuple(
            vid for vid in wanted if published[vid].published_at < period_end
        )
        skipped = tuple(vid for vid in wanted if vid not in eligible)

        collected_rows: list[tuple[str, RevenueRow]] = []
        pages: list[RevenuePage] = []
        if eligible:
            token = self._oauth.get_token(
                channel.id, required_scopes=(OAuthScope.YT_ANALYTICS_MONETARY_READONLY,)
            )
            for start in range(0, len(eligible), MAX_VIDEO_IDS):
                chunk = eligible[start : start + MAX_VIDEO_IDS]
                self._read_video_chunk(
                    channel.youtube.channel_id,
                    chunk,
                    requested_types,
                    period_start,
                    period_end,
                    token,
                    collected_rows,
                    pages,
                )

        outcomes: tuple[EstimatedRecordOutcome, ...] = ()
        if pages and collected_rows:
            first = pages[0]
            retrieved_at = max(page.retrieved_at for page in pages)
            outcomes = self._store_rows(
                MetricScope.VIDEO,
                rows=collected_rows,
                source=first.source,
                period_start=first.period_start,
                period_end=first.period_end,
                retrieved_at=retrieved_at,
            )

        videos_with_data = {vid for vid, _ in collected_rows}
        no_data = tuple(vid for vid in eligible if vid not in videos_with_data)
        through = _earliest(page.data_through for page in pages) if pages else None
        complete = bool(pages) and all(
            page.data_through is not None and page.data_through >= period_end
            for page in pages
        )
        result = VideoEstimatedRevenueResult(
            channel_id=channel.id,
            requested_video_ids=eligible,
            skipped_video_ids=skipped,
            outcomes=outcomes,
            no_data_video_ids=no_data,
            requested_revenue_types=requested_types,
            pages=len(pages),
            data_through=through,
            complete=complete,
        )
        self._audit.record(
            ESTIMATED_VIDEOS_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "stage": RevenueStage.ESTIMATED.value,
                "source": pages[0].source if pages else None,
                "requested_count": len(eligible),
                "skipped_count": len(skipped),
                "created_count": result.created_count,
                "unchanged_count": result.unchanged_count,
                "no_data_count": len(no_data),
                "pages": len(pages),
                "complete": complete,
            },
        )
        return result

    def channel_records(
        self,
        channel_id: str,
        *,
        revenue_type: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> list[RevenueRecord]:
        """Return stored ``RevenueStage.ESTIMATED`` records for ``channel_id``."""
        connection = self._database.connect()
        try:
            if ChannelRepository(connection).get(channel_id) is None:
                raise MetricsSyncError(
                    MetricsSyncErrorCode.CHANNEL_NOT_FOUND,
                    f"channel {channel_id} was not found",
                )
            records = RevenueRecordRepository(connection).list_by_subject(
                MetricScope.CHANNEL, channel_id
            )
        finally:
            connection.close()
        return _filter_estimated(
            records,
            revenue_type=revenue_type,
            period_start=period_start,
            period_end=period_end,
        )

    def video_records(
        self,
        youtube_video_id: str,
        *,
        revenue_type: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> list[RevenueRecord]:
        """Return stored ``RevenueStage.ESTIMATED`` records for ``youtube_video_id``."""
        connection = self._database.connect()
        try:
            records = RevenueRecordRepository(connection).list_by_subject(
                MetricScope.VIDEO, youtube_video_id
            )
        finally:
            connection.close()
        return _filter_estimated(
            records,
            revenue_type=revenue_type,
            period_start=period_start,
            period_end=period_end,
        )

    def latest_by_type(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> dict[str, RevenueRecord]:
        """Return the latest ``ESTIMATED`` record per ``revenue_type`` for an exact period."""
        connection = self._database.connect()
        try:
            records = RevenueRecordRepository(connection).list_by_subject(
                scope, subject_id
            )
        finally:
            connection.close()
        latest: dict[str, RevenueRecord] = {}
        for record in records:
            if (
                record.stage is RevenueStage.ESTIMATED
                and record.period_start == period_start
                and record.period_end == period_end
            ):
                latest[record.revenue_type] = record
        return latest

    def _read_video_chunk(
        self,
        youtube_channel_id: str,
        chunk: tuple[str, ...],
        requested_types: tuple[str, ...],
        period_start: datetime,
        period_end: datetime,
        token: Any,
        collected_rows: list[tuple[str, RevenueRow]],
        pages: list[RevenuePage],
    ) -> None:
        page_token: str | None = None
        seen_pairs: dict[tuple[str, str], int] = {}
        for _ in range(MAX_PAGES_PER_CHUNK):
            query = RevenueQuery(
                youtube_channel_id=youtube_channel_id,
                stage=RevenueStage.ESTIMATED,
                scope=MetricScope.VIDEO,
                period_start=period_start,
                period_end=period_end,
                revenue_types=requested_types,
                video_ids=chunk,
                page_size=min(self._page_size, len(chunk)),
                page_token=page_token,
            )
            page = call_with_retry(
                lambda q=query: self._provider.query(q, token=token),
                self._retry,
                sleep=self._sleep,
            )
            _require_estimated_page(page)
            pages.append(page)
            for row in page.rows:
                if row.subject_id in chunk and row.revenue_type in requested_types:
                    key = (row.subject_id, row.revenue_type)
                    if key in seen_pairs:
                        collected_rows[seen_pairs[key]] = (row.subject_id, row)
                    else:
                        seen_pairs[key] = len(collected_rows)
                        collected_rows.append((row.subject_id, row))
            if page.next_page_token is None:
                return
            page_token = page.next_page_token
        raise MetricsSyncError(
            MetricsSyncErrorCode.TOO_MANY_PAGES,
            f"more than {MAX_PAGES_PER_CHUNK} pages for {len(chunk)} videos",
        )

    def _store_rows(
        self,
        scope: MetricScope,
        *,
        rows: list[tuple[str, RevenueRow]],
        source: str,
        period_start: datetime,
        period_end: datetime,
        retrieved_at: datetime,
    ) -> tuple[EstimatedRecordOutcome, ...]:
        if not rows:
            return ()
        outcomes: list[EstimatedRecordOutcome] = []
        with self._database.transaction() as connection:
            repository = RevenueRecordRepository(connection)
            for subject_id, row in rows:
                same_stream = [
                    stored
                    for stored in repository.list_by_subject(scope, subject_id)
                    if stored.stage is RevenueStage.ESTIMATED
                    and stored.source == source
                    and stored.revenue_type == row.revenue_type
                    and stored.period_start == period_start
                    and stored.period_end == period_end
                ]
                if (
                    same_stream
                    and same_stream[-1].amount == row.amount
                    and same_stream[-1].currency == row.currency
                ):
                    outcomes.append(EstimatedRecordOutcome(same_stream[-1], False))
                    continue
                record = RevenueRecord.create(
                    RevenueStage.ESTIMATED,
                    scope,
                    subject_id,
                    revenue_type=row.revenue_type,
                    source=source,
                    amount=row.amount,
                    currency=row.currency,
                    period_start=period_start,
                    period_end=period_end,
                    clock=lambda: retrieved_at,
                )
                repository.add(record)
                outcomes.append(EstimatedRecordOutcome(record, True))
        return tuple(outcomes)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def require_started_period(period_start: datetime, now: datetime) -> None:
    if period_start > now:
        raise EstimatedRevenueError(
            EstimatedRevenueErrorCode.PERIOD_NOT_STARTED,
            f"period_start {period_start.isoformat()} is after now {now.isoformat()}",
        )


def _require_estimated_page(page: RevenuePage) -> None:
    if page.stage is not RevenueStage.ESTIMATED:
        raise EstimatedRevenueError(
            EstimatedRevenueErrorCode.STAGE_MISMATCH,
            f"expected estimated revenue page, got {page.stage.value}",
        )


def _select_published_videos(
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


def _filter_estimated(
    records: list[RevenueRecord],
    *,
    revenue_type: str | None,
    period_start: datetime | None,
    period_end: datetime | None,
) -> list[RevenueRecord]:
    filtered: list[RevenueRecord] = []
    for record in records:
        if record.stage is not RevenueStage.ESTIMATED:
            continue
        if revenue_type is not None and record.revenue_type != revenue_type:
            continue
        if period_start is not None and record.period_start < period_start:
            continue
        if period_end is not None and record.period_end > period_end:
            continue
        filtered.append(record)
    return filtered


def _earliest(moments: Any) -> datetime | None:
    values = list(moments)
    if any(value is None for value in values):
        return None
    return min(values) if values else None
