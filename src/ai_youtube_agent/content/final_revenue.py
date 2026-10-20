"""Final revenue sync, freshness, and reconciliation (Prompt Pack v8, prompt #172), context C14 Economics.

``FinalRevenueSync`` reads **finalized** revenue figures (``RevenueStage.FINAL``)
from a ``RevenueProvider`` (#170) and stores them as ``RevenueRecord`` rows (#024)
alongside—never overwriting—any ``RevenueStage.ESTIMATED`` rows (#171):

- Period must have ended (``period_end <= now``, ``require_ended_period``).
- Channel must exist and be ``ACTIVE`` or ``PAUSED`` (``load_syncable_channel``).
- Requires an OAuth token carrying ``OAuthScope.YT_ANALYTICS_MONETARY_READONLY``.
- Queries ``RevenueProvider`` with ``stage=RevenueStage.FINAL`` only and retries
  retryable provider errors via ``call_with_retry``.
- Never invents zero-value rows when the source has no finalized figure for a
  requested ``revenue_type`` or video.
- Idempotent per ``(scope, subject_id, source, revenue_type, period_start, period_end)``
  at ``RevenueStage.FINAL``: unchanged figures reuse the latest ``FINAL`` record
  (``created=False``); source corrections append a new ``FINAL`` record.
- Exposes source & freshness metadata via ``FinalRevenueFreshness`` (``source``,
  ``retrieved_at``, ``age``, ``max_age``, ``is_stale``) and side-by-side
  ``RevenueStageComparison`` (``compare_with_estimated``) without ever mutating
  or converting estimated records.
- Audits ``revenue.final_channel_synced`` and ``revenue.final_videos_synced``.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    require_ended_period,
)
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage
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

FINAL_CHANNEL_SYNCED: Final[str] = "revenue.final_channel_synced"
FINAL_VIDEOS_SYNCED: Final[str] = "revenue.final_videos_synced"
MAX_PAGES_PER_CHUNK: Final[int] = 20
DEFAULT_FINAL_MAX_AGE: Final[timedelta] = timedelta(days=30)


class FinalRevenueErrorCode(StrEnum):
    STAGE_MISMATCH = "domain.final_revenue_stage_mismatch"
    CURRENCY_MISMATCH = "domain.final_revenue_currency_mismatch"


USER_MESSAGES: Final[dict[FinalRevenueErrorCode, str]] = {
    FinalRevenueErrorCode.STAGE_MISMATCH: (
        "Final revenue operations require finalized revenue records."
    ),
    FinalRevenueErrorCode.CURRENCY_MISMATCH: (
        "Estimated and finalized revenue records have different currencies."
    ),
}

_STATUS: Final[dict[FinalRevenueErrorCode, HTTPStatus]] = {
    FinalRevenueErrorCode.STAGE_MISMATCH: HTTPStatus.UNPROCESSABLE_ENTITY,
    FinalRevenueErrorCode.CURRENCY_MISMATCH: HTTPStatus.CONFLICT,
}


class FinalRevenueError(DomainError):
    default_code = FinalRevenueErrorCode.STAGE_MISMATCH.value
    default_user_message = USER_MESSAGES[FinalRevenueErrorCode.STAGE_MISMATCH]
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, code: FinalRevenueErrorCode, detail: str) -> None:
        if not isinstance(code, FinalRevenueErrorCode):
            raise TypeError("code must be a FinalRevenueErrorCode")
        self.revenue_code = code
        super().__init__(detail, code=code.value, user_message=USER_MESSAGES[code])
        self.default_http_status = _STATUS[code]


@dataclass(frozen=True)
class FinalRevenueFreshness:
    record: RevenueRecord
    evaluated_at: datetime
    max_age: timedelta
    age: timedelta
    is_stale: bool

    def __post_init__(self) -> None:
        if self.record.stage is not RevenueStage.FINAL:
            raise FinalRevenueError(
                FinalRevenueErrorCode.STAGE_MISMATCH,
                f"record {self.record.id} is {self.record.stage.value}, not final",
            )

    @property
    def source(self) -> str:
        return self.record.source

    @property
    def retrieved_at(self) -> datetime:
        return self.record.retrieved_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record.id,
            "stage": self.record.stage.value,
            "source": self.record.source,
            "retrieved_at": self.record.retrieved_at.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat(),
            "age_seconds": int(self.age.total_seconds()),
            "max_age_seconds": int(self.max_age.total_seconds()),
            "is_stale": self.is_stale,
        }


@dataclass(frozen=True)
class FinalRecordOutcome:
    record: RevenueRecord
    created: bool
    freshness: FinalRevenueFreshness

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.as_dict(),
            "created": self.created,
            "freshness": self.freshness.as_dict(),
        }


@dataclass(frozen=True)
class ChannelFinalRevenueResult:
    channel_id: str
    source: str
    outcomes: tuple[FinalRecordOutcome, ...]
    requested_revenue_types: tuple[str, ...]
    missing_revenue_types: tuple[str, ...]
    retrieved_at: datetime
    data_through: datetime | None
    complete: bool

    @property
    def stage(self) -> RevenueStage:
        return RevenueStage.FINAL

    @property
    def is_final(self) -> bool:
        return True

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
            "is_final": True,
            "source": self.source,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "requested_revenue_types": list(self.requested_revenue_types),
            "missing_revenue_types": list(self.missing_revenue_types),
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
            "retrieved_at": self.retrieved_at.isoformat(),
            "data_through": self.data_through.isoformat() if self.data_through else None,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class VideoFinalRevenueResult:
    channel_id: str
    source: str | None
    requested_video_ids: tuple[str, ...]
    skipped_video_ids: tuple[str, ...]
    outcomes: tuple[FinalRecordOutcome, ...]
    no_data_video_ids: tuple[str, ...]
    requested_revenue_types: tuple[str, ...]
    pages: int
    data_through: datetime | None
    complete: bool

    @property
    def stage(self) -> RevenueStage:
        return RevenueStage.FINAL

    @property
    def is_final(self) -> bool:
        return True

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
            "is_final": True,
            "source": self.source,
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


@dataclass(frozen=True)
class RevenueStageComparison:
    """Side-by-side comparison of estimated vs final revenue without mutating either."""

    scope: MetricScope
    subject_id: str
    revenue_type: str
    period_start: datetime
    period_end: datetime
    estimated: RevenueRecord | None
    final: RevenueRecord | None
    variance: Decimal | None
    currency: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "revenue_type": self.revenue_type,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "estimated": self.estimated.as_dict() if self.estimated else None,
            "final": self.final.as_dict() if self.final else None,
            "variance": str(self.variance) if self.variance is not None else None,
            "currency": self.currency,
        }


class FinalRevenueSync:
    """Syncs, inspects freshness of, and reconciles ``RevenueStage.FINAL`` records."""

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
        max_age: timedelta = DEFAULT_FINAL_MAX_AGE,
        sleep: Callable[[float], None] = time.sleep,
        clock: Clock | None = None,
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_VIDEO_IDS:
            raise ValueError(f"page_size must be a whole number from 1 to {MAX_VIDEO_IDS}")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        self._database = database
        self._provider = provider
        self._oauth = oauth_provider
        self._results = results or PublishResultService(database)
        self._audit = audit_log
        self._retry = retry_policy
        self._page_size = page_size
        self._max_age = max_age
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
    ) -> ChannelFinalRevenueResult:
        """Sync channel-level finalized revenue for an ended period."""
        now = self._now()
        require_ended_period(period_end, now)
        channel = load_syncable_channel(self._database, channel_id)
        requested = tuple(revenue_types)
        query = RevenueQuery(
            youtube_channel_id=channel.youtube.channel_id,
            stage=RevenueStage.FINAL,
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
        _require_final_page(page)
        channel_rows = [
            row
            for row in page.rows
            if row.subject_id == channel.youtube.channel_id
            and row.revenue_type in requested
        ]
        found_types = {row.revenue_type for row in channel_rows}
        missing = tuple(rtype for rtype in requested if rtype not in found_types)
        eval_time = max(now, page.retrieved_at)
        outcomes = self._store_rows(
            MetricScope.CHANNEL,
            rows=[(channel.id, row) for row in channel_rows],
            source=page.source,
            period_start=page.period_start,
            period_end=page.period_end,
            retrieved_at=page.retrieved_at,
            evaluated_at=eval_time,
        )
        complete = page.data_through is not None and page.data_through >= period_end
        result = ChannelFinalRevenueResult(
            channel_id=channel.id,
            source=page.source,
            outcomes=outcomes,
            requested_revenue_types=requested,
            missing_revenue_types=missing,
            retrieved_at=page.retrieved_at,
            data_through=page.data_through,
            complete=complete,
        )
        self._audit.record(
            FINAL_CHANNEL_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "stage": RevenueStage.FINAL.value,
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
    ) -> VideoFinalRevenueResult:
        """Sync per-video finalized revenue for an ended period."""
        now = self._now()
        require_ended_period(period_end, now)
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

        outcomes: tuple[FinalRecordOutcome, ...] = ()
        if pages and collected_rows:
            first = pages[0]
            retrieved_at = max(page.retrieved_at for page in pages)
            eval_time = max(now, retrieved_at)
            outcomes = self._store_rows(
                MetricScope.VIDEO,
                rows=collected_rows,
                source=first.source,
                period_start=first.period_start,
                period_end=first.period_end,
                retrieved_at=retrieved_at,
                evaluated_at=eval_time,
            )

        videos_with_data = {vid for vid, _ in collected_rows}
        no_data = tuple(vid for vid in eligible if vid not in videos_with_data)
        through = _earliest(page.data_through for page in pages) if pages else None
        complete = bool(pages) and all(
            page.data_through is not None and page.data_through >= period_end
            for page in pages
        )
        source = pages[0].source if pages else None
        result = VideoFinalRevenueResult(
            channel_id=channel.id,
            source=source,
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
            FINAL_VIDEOS_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
                "stage": RevenueStage.FINAL.value,
                "source": source,
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

    def freshness(
        self,
        record: RevenueRecord,
        *,
        now: datetime | None = None,
        max_age: timedelta | None = None,
    ) -> FinalRevenueFreshness:
        """Evaluate source and freshness metadata for a ``RevenueStage.FINAL`` record."""
        if record.stage is not RevenueStage.FINAL:
            raise FinalRevenueError(
                FinalRevenueErrorCode.STAGE_MISMATCH,
                f"record {record.id} has stage {record.stage.value}, expected final",
            )
        eval_time = now if now is not None else self._now()
        limit = max_age if max_age is not None else self._max_age
        age = record.age_at(eval_time)
        stale = record.is_stale(eval_time, limit)
        return FinalRevenueFreshness(
            record=record,
            evaluated_at=eval_time,
            max_age=limit,
            age=age,
            is_stale=stale,
        )

    def channel_records(
        self,
        channel_id: str,
        *,
        revenue_type: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> list[RevenueRecord]:
        """Return stored ``RevenueStage.FINAL`` records for ``channel_id``."""
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
        return _filter_final(
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
        """Return stored ``RevenueStage.FINAL`` records for ``youtube_video_id``."""
        connection = self._database.connect()
        try:
            records = RevenueRecordRepository(connection).list_by_subject(
                MetricScope.VIDEO, youtube_video_id
            )
        finally:
            connection.close()
        return _filter_final(
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
        """Return the latest ``FINAL`` record per ``revenue_type`` for an exact period."""
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
                record.stage is RevenueStage.FINAL
                and record.period_start == period_start
                and record.period_end == period_end
            ):
                latest[record.revenue_type] = record
        return latest

    def compare_with_estimated(
        self,
        scope: MetricScope,
        subject_id: str,
        *,
        revenue_type: str,
        period_start: datetime,
        period_end: datetime,
    ) -> RevenueStageComparison:
        """Compare the latest estimated and final records side-by-side without mutating either."""
        connection = self._database.connect()
        try:
            records = RevenueRecordRepository(connection).list_by_subject(
                scope, subject_id
            )
        finally:
            connection.close()
        latest_est: RevenueRecord | None = None
        latest_fin: RevenueRecord | None = None
        for record in records:
            if (
                record.revenue_type == revenue_type
                and record.period_start == period_start
                and record.period_end == period_end
            ):
                if record.stage is RevenueStage.ESTIMATED:
                    latest_est = record
                elif record.stage is RevenueStage.FINAL:
                    latest_fin = record
        variance: Decimal | None = None
        currency: str | None = None
        if latest_est is not None and latest_fin is not None:
            if latest_est.currency != latest_fin.currency:
                raise FinalRevenueError(
                    FinalRevenueErrorCode.CURRENCY_MISMATCH,
                    f"estimated currency {latest_est.currency} != final currency {latest_fin.currency}",
                )
            currency = latest_fin.currency
            variance = latest_fin.amount - latest_est.amount
        elif latest_fin is not None:
            currency = latest_fin.currency
        elif latest_est is not None:
            currency = latest_est.currency
        return RevenueStageComparison(
            scope=scope,
            subject_id=subject_id,
            revenue_type=revenue_type,
            period_start=period_start,
            period_end=period_end,
            estimated=latest_est,
            final=latest_fin,
            variance=variance,
            currency=currency,
        )

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
                stage=RevenueStage.FINAL,
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
            _require_final_page(page)
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
        evaluated_at: datetime,
    ) -> tuple[FinalRecordOutcome, ...]:
        if not rows:
            return ()
        outcomes: list[FinalRecordOutcome] = []
        with self._database.transaction() as connection:
            repository = RevenueRecordRepository(connection)
            for subject_id, row in rows:
                same_stream = [
                    stored
                    for stored in repository.list_by_subject(scope, subject_id)
                    if stored.stage is RevenueStage.FINAL
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
                    existing = same_stream[-1]
                    freshness = self.freshness(
                        existing, now=max(evaluated_at, existing.retrieved_at)
                    )
                    outcomes.append(FinalRecordOutcome(existing, False, freshness))
                    continue
                record = RevenueRecord.create(
                    RevenueStage.FINAL,
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
                freshness = self.freshness(record, now=evaluated_at)
                outcomes.append(FinalRecordOutcome(record, True, freshness))
        return tuple(outcomes)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _require_final_page(page: RevenuePage) -> None:
    if page.stage is not RevenueStage.FINAL:
        raise FinalRevenueError(
            FinalRevenueErrorCode.STAGE_MISMATCH,
            f"expected final revenue page, got {page.stage.value}",
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


def _filter_final(
    records: list[RevenueRecord],
    *,
    revenue_type: str | None,
    period_start: datetime | None,
    period_end: datetime | None,
) -> list[RevenueRecord]:
    filtered: list[RevenueRecord] = []
    for record in records:
        if record.stage is not RevenueStage.FINAL:
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
