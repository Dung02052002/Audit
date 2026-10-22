"""Video Metrics sync (Prompt Pack v8, prompt #160), context C13 Analytics.

``VideoMetricsSync.sync`` reads per-video metrics of a channel's published videos
from the ``AnalyticsProvider`` (#158) and stores one ``MetricSnapshot`` per video
(scope ``video``, subject = the YouTube video id, source = the provider name):

- Which videos: by default every video the channel published (a succeeded publish
  job, read through ``PublishResultService``, #155) that was published before the
  period ends. ``video_ids`` may name some of them; an id this channel did not publish
  is refused (``VIDEO_NOT_PUBLISHED``), so the agent never reads another channel's
  videos. A channel with no published video makes no request.
- The same rules as the channel sync (#159): the channel must be ``ACTIVE`` or
  ``PAUSED``, the period must have ended, the token needs the read-only analytics
  scope, retryable provider errors are retried, and only the metrics the source
  returned are stored. A video the source returned no row for has no snapshot and is
  listed in ``no_data_video_ids``; a requested metric the source lacks is absent
  from that video's snapshot (never 0).
- Requests go in chunks of at most 50 videos; each chunk is read page by page (the
  token of a page is only used for the same query) with a limit of 20 pages.
- Idempotent per video: the same numbers for the same video, source and period store
  nothing; changed numbers add a snapshot. All new snapshots of one sync are stored in
  one transaction.
- ``analytics.videos_synced`` is audited after the commit with counts only.

Shorts and LongForm normalisation of the numbers is #161 and #162.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
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
from ai_youtube_agent.providers.youtube_oauth import (
    OAuthScope,
    YouTubeOAuthProvider,
    get_or_renew_token,
)

Clock = Callable[[], datetime]

VIDEOS_SYNCED: Final[str] = "analytics.videos_synced"
MAX_PAGES_PER_CHUNK: Final[int] = 20
DEFAULT_VIDEO_METRICS: Final[tuple[str, ...]] = (
    "views",
    "estimated_minutes_watched",
    "average_view_duration",
    "subscribers_gained",
    "subscribers_lost",
    "likes",
    "comments",
    "shares",
)
"""What a video sync asks for by default. Retention and click-through rate have their
own tasks (#164, #165)."""


@dataclass(frozen=True)
class VideoSnapshotOutcome:
    youtube_video_id: str
    snapshot: MetricSnapshot
    created: bool
    missing: tuple[str, ...]


@dataclass(frozen=True)
class VideoMetricsResult:
    channel_id: str
    requested_video_ids: tuple[str, ...]
    skipped_video_ids: tuple[str, ...]
    outcomes: tuple[VideoSnapshotOutcome, ...]
    no_data_video_ids: tuple[str, ...]
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
                    "created": outcome.created,
                    "missing": list(outcome.missing),
                }
                for outcome in self.outcomes
            ],
            "no_data_video_ids": list(self.no_data_video_ids),
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
            "pages": self.pages,
            "data_through": self.data_through.isoformat()
            if self.data_through
            else None,
            "complete": self.complete,
        }


class VideoMetricsSync:
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
        metrics: Sequence[str] = DEFAULT_VIDEO_METRICS,
    ) -> VideoMetricsResult:
        now = self._now()
        require_ended_period(period_end, now)
        channel = load_syncable_channel(self._database, channel_id)
        requested_metrics = tuple(metrics)

        published = {
            record.youtube_video_id: record
            for record in self._results.list_for_channel(channel.id)
        }
        wanted = self._select(published, video_ids)
        eligible = tuple(
            video_id
            for video_id in wanted
            if published[video_id].published_at < period_end
        )
        skipped = tuple(video_id for video_id in wanted if video_id not in eligible)

        rows: dict[str, dict[str, Decimal]] = {}
        pages: list[AnalyticsPage] = []
        if eligible:
            token = get_or_renew_token(
                self._oauth,
                channel.id,
                required_scopes=(OAuthScope.YT_ANALYTICS_READONLY,),
            )
            for start in range(0, len(eligible), MAX_VIDEO_IDS):
                chunk = eligible[start : start + MAX_VIDEO_IDS]
                self._read_chunk(
                    channel.youtube.channel_id,
                    chunk,
                    requested_metrics,
                    period_start,
                    period_end,
                    token,
                    rows,
                    pages,
                )

        outcomes, no_data = self._store(
            channel.id, eligible, rows, pages, requested_metrics
        )
        through = _earliest(page.data_through for page in pages) if pages else None
        complete = bool(pages) and all(
            page.data_through is not None and page.data_through >= period_end
            for page in pages
        )
        result = VideoMetricsResult(
            channel_id=channel.id,
            requested_video_ids=eligible,
            skipped_video_ids=skipped,
            outcomes=outcomes,
            no_data_video_ids=no_data,
            pages=len(pages),
            data_through=through,
            complete=complete,
        )
        self._audit.record(
            VIDEOS_SYNCED,
            actor,
            EntityRef(CHANNEL_ENTITY, channel.id),
            AuditResult.SUCCESS,
            metadata={
                "channel_id": channel.id,
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
        requested_metrics: tuple[str, ...],
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
                metrics=requested_metrics,
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
        channel_id: str,
        eligible: tuple[str, ...],
        rows: dict[str, dict[str, Decimal]],
        pages: list[AnalyticsPage],
        requested_metrics: tuple[str, ...],
    ) -> tuple[tuple[VideoSnapshotOutcome, ...], tuple[str, ...]]:
        outcomes: list[VideoSnapshotOutcome] = []
        no_data: list[str] = []
        if not pages:
            return (), ()
        first = pages[0]
        retrieved_at = max(page.retrieved_at for page in pages)
        with self._database.transaction() as connection:
            repository = MetricSnapshotRepository(connection)
            for video_id in eligible:
                values = rows.get(video_id)
                if not values:
                    no_data.append(video_id)
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
                missing = tuple(
                    name for name in requested_metrics if name not in values
                )
                if same_period and dict(same_period[-1].metrics) == values:
                    outcomes.append(
                        VideoSnapshotOutcome(video_id, same_period[-1], False, missing)
                    )
                    continue
                snapshot = MetricSnapshot.create(
                    MetricScope.VIDEO,
                    video_id,
                    source=first.source,
                    period_start=first.period_start,
                    period_end=first.period_end,
                    metrics=values,
                    clock=lambda: retrieved_at,
                )
                repository.add(snapshot)
                outcomes.append(VideoSnapshotOutcome(video_id, snapshot, True, missing))
        return tuple(outcomes), tuple(no_data)

    def snapshots(self, youtube_video_id: str) -> list[MetricSnapshot]:
        """The stored snapshots of one video, oldest period first."""
        connection = self._database.connect()
        try:
            return MetricSnapshotRepository(connection).list_by_subject(
                MetricScope.VIDEO, youtube_video_id
            )
        finally:
            connection.close()

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _earliest(moments: Any) -> datetime | None:
    """The earliest ``data_through``; ``None`` when any page has no complete data."""
    values = list(moments)
    if any(value is None for value in values):
        return None
    return min(values) if values else None
