"""Analytics Screen view and service (Prompt Pack v8, prompt Q-196), context X3 Command Center.

Builds the Command Center Analytics Screen view model and iOS-style HTML rendering,
presenting:
- Time-range channel metrics (views, watch time in minutes/hours/seconds,
  subscribers gained/lost/net, retention ``average_view_percentage`` when
  available, CTR ``impressions_click_through_rate`` when available, and
  per-period time series).
- Freshness evaluation (``fresh``, ``lagging``, ``stale``) via
  ``evaluate_freshness``.
- Side-by-side ``Shorts`` vs ``LongForm`` performance comparison across
  published videos using ``normalize_shorts_metrics`` and
  ``normalize_longform_metrics``, keeping any absent metric ``None`` (never
  fabricating ``0``).
"""

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.analytics_freshness import (
    DEFAULT_FRESHNESS_POLICY,
    AnalyticsFreshnessPolicy,
    FreshnessStatus,
    evaluate_freshness,
)
from ai_youtube_agent.content.ctr import extract_ctr
from ai_youtube_agent.content.dashboard_channel import (
    ChannelSubscribersSummary,
    ChannelViewsWatchTimeSummary,
    _build_subscribers_summary,
    _build_views_watch_time_summary,
    _fmt_metric,
    _fmt_signed_metric,
    _select_non_overlapping_snapshots,
    _sum_metric,
)
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.content.longform_metrics import (
    LongFormMetricsView,
    normalize_longform_metrics,
)
from ai_youtube_agent.content.retention import extract_retention
from ai_youtube_agent.content.shorts_metrics import (
    ShortsMetricsView,
    normalize_shorts_metrics,
)
from ai_youtube_agent.content.watch_time import TWO_PLACES, WatchTimeMeasurement, WatchTimeUnit
from ai_youtube_agent.core.content_item import ContentItem, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository
from ai_youtube_agent.core.db.repositories.publish import PublishJobRepository
from ai_youtube_agent.pipeline.publish import PublishStatus

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class TimeRangePeriodRow:
    snapshot_id: str
    period_start: datetime
    period_end: datetime
    source: str
    retrieved_at: datetime
    freshness_status: str
    views: Decimal | None
    watch_time_minutes: Decimal | None
    watch_time_hours: Decimal | None
    average_view_percentage: Decimal | None
    impressions: Decimal | None
    impressions_click_through_rate: Decimal | None
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    net_subscribers: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
            "freshness_status": self.freshness_status,
            "views": str(self.views) if self.views is not None else None,
            "watch_time_minutes": (
                str(self.watch_time_minutes)
                if self.watch_time_minutes is not None
                else None
            ),
            "watch_time_hours": (
                str(self.watch_time_hours)
                if self.watch_time_hours is not None
                else None
            ),
            "average_view_percentage": (
                str(self.average_view_percentage)
                if self.average_view_percentage is not None
                else None
            ),
            "impressions": (
                str(self.impressions) if self.impressions is not None else None
            ),
            "impressions_click_through_rate": (
                str(self.impressions_click_through_rate)
                if self.impressions_click_through_rate is not None
                else None
            ),
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
                str(self.net_subscribers)
                if self.net_subscribers is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ShortsAnalyticsSummary:
    published_video_count: int
    videos_with_metrics_count: int
    total_views: Decimal | None
    total_engaged_views: Decimal | None
    total_swiped_away: Decimal | None
    total_shorts_feed_impressions: Decimal | None
    total_watch_time_minutes: Decimal | None
    total_watch_time_hours: Decimal | None
    average_view_percentage: Decimal | None
    stay_to_watch_rate: Decimal | None
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    videos: tuple[ShortsMetricsView, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "published_video_count": self.published_video_count,
            "videos_with_metrics_count": self.videos_with_metrics_count,
            "total_views": (
                str(self.total_views) if self.total_views is not None else None
            ),
            "total_engaged_views": (
                str(self.total_engaged_views)
                if self.total_engaged_views is not None
                else None
            ),
            "total_swiped_away": (
                str(self.total_swiped_away)
                if self.total_swiped_away is not None
                else None
            ),
            "total_shorts_feed_impressions": (
                str(self.total_shorts_feed_impressions)
                if self.total_shorts_feed_impressions is not None
                else None
            ),
            "total_watch_time_minutes": (
                str(self.total_watch_time_minutes)
                if self.total_watch_time_minutes is not None
                else None
            ),
            "total_watch_time_hours": (
                str(self.total_watch_time_hours)
                if self.total_watch_time_hours is not None
                else None
            ),
            "average_view_percentage": (
                str(self.average_view_percentage)
                if self.average_view_percentage is not None
                else None
            ),
            "stay_to_watch_rate": (
                str(self.stay_to_watch_rate)
                if self.stay_to_watch_rate is not None
                else None
            ),
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
            "videos": [v.as_dict() for v in self.videos],
        }


@dataclass(frozen=True)
class LongFormAnalyticsSummary:
    published_video_count: int
    videos_with_metrics_count: int
    total_views: Decimal | None
    total_impressions: Decimal | None
    impressions_click_through_rate: Decimal | None
    total_watch_time_minutes: Decimal | None
    total_watch_time_hours: Decimal | None
    average_view_percentage: Decimal | None
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    videos: tuple[LongFormMetricsView, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "published_video_count": self.published_video_count,
            "videos_with_metrics_count": self.videos_with_metrics_count,
            "total_views": (
                str(self.total_views) if self.total_views is not None else None
            ),
            "total_impressions": (
                str(self.total_impressions)
                if self.total_impressions is not None
                else None
            ),
            "impressions_click_through_rate": (
                str(self.impressions_click_through_rate)
                if self.impressions_click_through_rate is not None
                else None
            ),
            "total_watch_time_minutes": (
                str(self.total_watch_time_minutes)
                if self.total_watch_time_minutes is not None
                else None
            ),
            "total_watch_time_hours": (
                str(self.total_watch_time_hours)
                if self.total_watch_time_hours is not None
                else None
            ),
            "average_view_percentage": (
                str(self.average_view_percentage)
                if self.average_view_percentage is not None
                else None
            ),
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
            "videos": [v.as_dict() for v in self.videos],
        }


@dataclass(frozen=True)
class AnalyticsScreenView:
    shell: DashboardShellState
    load_state: ViewLoadState
    channel_id: str | None
    requested_period_start: datetime | None
    requested_period_end: datetime | None
    freshness_status: str
    freshness_tone: BadgeTone
    channel_views_watch_time: ChannelViewsWatchTimeSummary | None
    channel_subscribers: ChannelSubscribersSummary | None
    channel_retention_percentage: Decimal | None
    channel_ctr_percentage: Decimal | None
    channel_impressions: Decimal | None
    period_rows: tuple[TimeRangePeriodRow, ...]
    shorts: ShortsAnalyticsSummary
    longform: LongFormAnalyticsSummary
    empty_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "channel_id": self.channel_id,
            "requested_period_start": (
                self.requested_period_start.isoformat()
                if self.requested_period_start
                else None
            ),
            "requested_period_end": (
                self.requested_period_end.isoformat()
                if self.requested_period_end
                else None
            ),
            "freshness_status": self.freshness_status,
            "freshness_tone": self.freshness_tone.value,
            "channel_views_watch_time": (
                self.channel_views_watch_time.as_dict()
                if self.channel_views_watch_time
                else None
            ),
            "channel_subscribers": (
                self.channel_subscribers.as_dict()
                if self.channel_subscribers
                else None
            ),
            "channel_retention_percentage": (
                str(self.channel_retention_percentage)
                if self.channel_retention_percentage is not None
                else None
            ),
            "channel_ctr_percentage": (
                str(self.channel_ctr_percentage)
                if self.channel_ctr_percentage is not None
                else None
            ),
            "channel_impressions": (
                str(self.channel_impressions)
                if self.channel_impressions is not None
                else None
            ),
            "period_rows": [r.as_dict() for r in self.period_rows],
            "shorts": self.shorts.as_dict(),
            "longform": self.longform.as_dict(),
            "empty_message": self.empty_message,
        }


class AnalyticsScreenService:
    """Builds the Analytics Screen view model and renders its HTML."""

    def __init__(
        self,
        database: Database,
        *,
        shell_service: DashboardShellService | None = None,
        freshness_policy: AnalyticsFreshnessPolicy = DEFAULT_FRESHNESS_POLICY,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._freshness_policy = freshness_policy
        self._clock = clock
        self._shell_service = shell_service or DashboardShellService(
            database, clock=clock
        )

    def load(
        self,
        channel_id: str | None = None,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> AnalyticsScreenView:
        if period_start is not None and period_start.utcoffset() != timedelta(0):
            raise ValueError("period_start must be timezone-aware UTC")
        if period_end is not None and period_end.utcoffset() != timedelta(0):
            raise ValueError("period_end must be timezone-aware UTC")
        if (
            period_start is not None
            and period_end is not None
            and period_start >= period_end
        ):
            raise ValueError("period_start must be earlier than period_end")

        now = self._now()
        shell = self._shell_service.build_shell(
            DashboardScreenId.ANALYTICS,
            channel_id=channel_id,
        )

        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
            if selected_channel is None:
                return AnalyticsScreenView(
                    shell=shell,
                    load_state=ViewLoadState.EMPTY,
                    channel_id=None,
                    requested_period_start=period_start,
                    requested_period_end=period_end,
                    freshness_status="empty",
                    freshness_tone=BadgeTone.NEUTRAL,
                    channel_views_watch_time=None,
                    channel_subscribers=None,
                    channel_retention_percentage=None,
                    channel_ctr_percentage=None,
                    channel_impressions=None,
                    period_rows=(),
                    shorts=_empty_shorts_summary(),
                    longform=_empty_longform_summary(),
                    empty_message="No analytics data or channel configured yet. Create a channel to view analytics.",
                )

            snapshot_repo = MetricSnapshotRepository(connection)
            channel_snapshots = snapshot_repo.list_by_subject(
                MetricScope.CHANNEL, selected_channel.id
            )
            items = ContentItemRepository(connection).list_by_channel(
                selected_channel.id
            )
            publish_repo = PublishJobRepository(connection)

            published_by_item: dict[str, tuple[ContentItem, str]] = {}
            for item in items:
                jobs = publish_repo.list_by_content_item(item.id)
                for job in reversed(jobs):
                    if job.status is PublishStatus.SUCCEEDED and job.result is not None:
                        published_by_item[item.id] = (
                            item,
                            job.result.youtube_video_id,
                        )
                        break

            video_snapshots_by_yt_id: dict[str, list[MetricSnapshot]] = {}
            for _, yt_video_id in published_by_item.values():
                video_snapshots_by_yt_id[yt_video_id] = snapshot_repo.list_by_subject(
                    MetricScope.VIDEO, yt_video_id
                )
        finally:
            connection.close()

        filtered_channel = _filter_by_window(
            channel_snapshots, period_start=period_start, period_end=period_end
        )
        non_overlapping_channel = _select_non_overlapping_snapshots(filtered_channel)

        vw_summary = _build_views_watch_time_summary(
            selected_channel.id, non_overlapping_channel
        )
        subs_summary = _build_subscribers_summary(
            selected_channel.id, non_overlapping_channel
        )
        channel_ret = _latest_present(
            non_overlapping_channel,
            lambda s: extract_retention(s).average_view_percentage,
        )
        channel_ctr = _latest_present(
            non_overlapping_channel,
            lambda s: extract_ctr(s).impressions_click_through_rate,
        )
        channel_impressions = _sum_metric(non_overlapping_channel, "impressions")

        period_rows: list[TimeRangePeriodRow] = []
        freshness_statuses: list[FreshnessStatus] = []
        for snap in non_overlapping_channel:
            eval_now = max(now, snap.retrieved_at)
            fresh = evaluate_freshness(
                snap, now=eval_now, policy=self._freshness_policy
            )
            freshness_statuses.append(fresh.status)
            raw_mins = snap.metrics.get("estimated_minutes_watched")
            wt = (
                WatchTimeMeasurement.from_unit(raw_mins, WatchTimeUnit.MINUTES)
                if raw_mins is not None
                else None
            )
            gained = snap.metrics.get("subscribers_gained")
            lost = snap.metrics.get("subscribers_lost")
            net = gained - lost if gained is not None and lost is not None else None
            period_rows.append(
                TimeRangePeriodRow(
                    snapshot_id=snap.id,
                    period_start=snap.period_start,
                    period_end=snap.period_end,
                    source=snap.source,
                    retrieved_at=snap.retrieved_at,
                    freshness_status=fresh.status.value,
                    views=snap.metrics.get("views"),
                    watch_time_minutes=wt.minutes if wt else None,
                    watch_time_hours=wt.hours if wt else None,
                    average_view_percentage=extract_retention(
                        snap
                    ).average_view_percentage,
                    impressions=snap.metrics.get("impressions"),
                    impressions_click_through_rate=extract_ctr(
                        snap
                    ).impressions_click_through_rate,
                    subscribers_gained=gained,
                    subscribers_lost=lost,
                    net_subscribers=net,
                )
            )

        shorts_summary = _build_shorts_summary(
            published_by_item,
            video_snapshots_by_yt_id,
            period_start=period_start,
            period_end=period_end,
        )
        longform_summary = _build_longform_summary(
            published_by_item,
            video_snapshots_by_yt_id,
            period_start=period_start,
            period_end=period_end,
        )

        overall_freshness, freshness_tone = _summarize_freshness(freshness_statuses)
        has_any_data = (
            bool(period_rows)
            or shorts_summary.videos_with_metrics_count > 0
            or longform_summary.videos_with_metrics_count > 0
        )

        return AnalyticsScreenView(
            shell=shell,
            load_state=ViewLoadState.READY if has_any_data else ViewLoadState.EMPTY,
            channel_id=selected_channel.id,
            requested_period_start=period_start,
            requested_period_end=period_end,
            freshness_status=overall_freshness,
            freshness_tone=freshness_tone,
            channel_views_watch_time=vw_summary,
            channel_subscribers=subs_summary,
            channel_retention_percentage=channel_ret,
            channel_ctr_percentage=channel_ctr,
            channel_impressions=channel_impressions,
            period_rows=tuple(period_rows),
            shorts=shorts_summary,
            longform=longform_summary,
            empty_message=(
                "No analytics snapshots synced for this time range yet."
                if not has_any_data
                else None
            ),
        )

    def render_html(self, view: AnalyticsScreenView) -> str:
        if view.load_state is ViewLoadState.EMPTY:
            msg = html.escape(view.empty_message or "No analytics data available.")
            body = (
                '<section class="yt-card" data-testid="analytics-empty-state">'
                '<div class="yt-card__header">'
                '<h2 class="yt-card__title">Analytics</h2>'
                "</div>"
                f'<p class="yt-empty">{msg}</p>'
                "</section>"
            )
            return render_dashboard_shell_html(
                view.shell,
                body_html=body,
                page_title="Analytics — YouTube Command Center",
            )

        vw = view.channel_views_watch_time
        subs = view.channel_subscribers
        sh = view.shorts
        lf = view.longform

        period_table_rows = "".join(
            f"<tr><td>{html.escape(r.period_start.date().isoformat())} → "
            f"{html.escape(r.period_end.date().isoformat())}</td>"
            f"<td>{html.escape(_fmt_metric(r.views))}</td>"
            f"<td>{html.escape(_fmt_metric(r.watch_time_hours, suffix=' h'))}</td>"
            f"<td>{html.escape(_fmt_metric(r.average_view_percentage, suffix='%'))}</td>"
            f"<td>{html.escape(_fmt_metric(r.impressions_click_through_rate, suffix='%'))}</td>"
            f"<td>{html.escape(_fmt_signed_metric(r.net_subscribers))}</td>"
            f"<td>{html.escape(r.freshness_status)}</td></tr>"
            for r in view.period_rows
        )

        body = f"""<div class="yt-grid" data-testid="analytics-screen">
  <section class="yt-card yt-grid__span-12" data-testid="analytics-time-range-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Channel Time-Range Metrics</p>
        <h2 class="yt-card__title">Performance Overview</h2>
      </div>
      <span class="yt-pill yt-pill--{view.freshness_tone.value}">{html.escape(view.freshness_status)}</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(_fmt_metric(vw.views if vw else None))}</span>
      <span class="yt-kpi__unit">views · {html.escape(_fmt_metric(vw.watch_time_hours if vw else None, suffix=' hrs'))} · Net subs: {html.escape(_fmt_signed_metric(subs.net_subscribers if subs else None))}</span>
    </div>
    <p class="yt-card__subtitle">Retention: {html.escape(_fmt_metric(view.channel_retention_percentage, suffix='%'))} · CTR: {html.escape(_fmt_metric(view.channel_ctr_percentage, suffix='%'))} · Impressions: {html.escape(_fmt_metric(view.channel_impressions))}</p>
    <table class="yt-table" data-testid="analytics-periods-table">
      <thead><tr><th>Period</th><th>Views</th><th>Watch Time</th><th>Retention</th><th>CTR</th><th>Net Subs</th><th>Freshness</th></tr></thead>
      <tbody>{period_table_rows}</tbody>
    </table>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="analytics-shorts-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Shorts Analytics</p>
        <h2 class="yt-card__title">Shorts Performance</h2>
      </div>
      <span class="yt-pill yt-pill--info">{sh.published_video_count} published</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(_fmt_metric(sh.total_views))}</span>
      <span class="yt-kpi__unit">views · Engaged: {html.escape(_fmt_metric(sh.total_engaged_views))}</span>
    </div>
    <p class="yt-card__subtitle">Stay-to-watch: {html.escape(_fmt_metric(sh.stay_to_watch_rate, suffix='%'))} · Swiped away: {html.escape(_fmt_metric(sh.total_swiped_away))} · Watch time: {html.escape(_fmt_metric(sh.total_watch_time_minutes, suffix=' min'))}</p>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="analytics-longform-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">LongForm Analytics</p>
        <h2 class="yt-card__title">LongForm Performance</h2>
      </div>
      <span class="yt-pill yt-pill--info">{lf.published_video_count} published</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(_fmt_metric(lf.total_views))}</span>
      <span class="yt-kpi__unit">views · Watch time: {html.escape(_fmt_metric(lf.total_watch_time_hours, suffix=' hrs'))}</span>
    </div>
    <p class="yt-card__subtitle">Impressions: {html.escape(_fmt_metric(lf.total_impressions))} · CTR: {html.escape(_fmt_metric(lf.impressions_click_through_rate, suffix='%'))} · Avg view %: {html.escape(_fmt_metric(lf.average_view_percentage, suffix='%'))}</p>
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title="Analytics — YouTube Command Center",
        )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def _filter_by_window(
    snapshots: Sequence[MetricSnapshot],
    *,
    period_start: datetime | None,
    period_end: datetime | None,
) -> list[MetricSnapshot]:
    return [
        snap
        for snap in snapshots
        if (period_start is None or snap.period_start >= period_start)
        and (period_end is None or snap.period_end <= period_end)
    ]


def _latest_present(
    snapshots: Sequence[MetricSnapshot],
    getter: Callable[[MetricSnapshot], Decimal | None],
) -> Decimal | None:
    for snap in reversed(snapshots):
        val = getter(snap)
        if val is not None:
            return val
    return None


def _summarize_freshness(
    statuses: Sequence[FreshnessStatus],
) -> tuple[str, BadgeTone]:
    if not statuses:
        return "no_data", BadgeTone.NEUTRAL
    if any(s is FreshnessStatus.STALE for s in statuses):
        return FreshnessStatus.STALE.value, BadgeTone.WARNING
    if any(s is FreshnessStatus.LAGGING for s in statuses):
        return FreshnessStatus.LAGGING.value, BadgeTone.INFO
    return FreshnessStatus.FRESH.value, BadgeTone.SUCCESS


def _empty_shorts_summary() -> ShortsAnalyticsSummary:
    return ShortsAnalyticsSummary(
        published_video_count=0,
        videos_with_metrics_count=0,
        total_views=None,
        total_engaged_views=None,
        total_swiped_away=None,
        total_shorts_feed_impressions=None,
        total_watch_time_minutes=None,
        total_watch_time_hours=None,
        average_view_percentage=None,
        stay_to_watch_rate=None,
        subscribers_gained=None,
        subscribers_lost=None,
        videos=(),
    )


def _empty_longform_summary() -> LongFormAnalyticsSummary:
    return LongFormAnalyticsSummary(
        published_video_count=0,
        videos_with_metrics_count=0,
        total_views=None,
        total_impressions=None,
        impressions_click_through_rate=None,
        total_watch_time_minutes=None,
        total_watch_time_hours=None,
        average_view_percentage=None,
        subscribers_gained=None,
        subscribers_lost=None,
        videos=(),
    )


def _build_shorts_summary(
    published_by_item: dict[str, tuple[ContentItem, str]],
    video_snapshots_by_yt_id: dict[str, list[MetricSnapshot]],
    *,
    period_start: datetime | None,
    period_end: datetime | None,
) -> ShortsAnalyticsSummary:
    shorts_items = [
        (item, yt_id)
        for item, yt_id in published_by_item.values()
        if item.content_type is ContentType.SHORTS
    ]
    views_list: list[ShortsMetricsView] = []
    all_selected_snapshots: list[MetricSnapshot] = []
    videos_with_metrics = 0

    for item, yt_id in shorts_items:
        raw = _filter_by_window(
            video_snapshots_by_yt_id.get(yt_id, ()),
            period_start=period_start,
            period_end=period_end,
        )
        deduped = _select_non_overlapping_snapshots(raw)
        if deduped:
            videos_with_metrics += 1
            all_selected_snapshots.extend(deduped)
            views_list.append(
                normalize_shorts_metrics(deduped[-1], content_item_id=item.id)
            )

    total_views = _sum_metric(all_selected_snapshots, "views")
    total_engaged = _sum_metric(all_selected_snapshots, "engaged_views")
    total_swiped = _sum_metric(all_selected_snapshots, "swiped_away")
    total_feed_imp = _sum_metric(
        all_selected_snapshots, "shorts_feed_impressions"
    )
    total_mins = _sum_metric(
        all_selected_snapshots, "estimated_minutes_watched"
    )
    total_hours = (
        (total_mins / Decimal("60")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if total_mins is not None
        else None
    )
    avg_pct = _average_metric(all_selected_snapshots, "average_view_percentage")
    stay_rate: Decimal | None = None
    if (
        total_engaged is not None
        and total_swiped is not None
        and (total_engaged + total_swiped) > 0
    ):
        stay_rate = (
            total_engaged * Decimal("100") / (total_engaged + total_swiped)
        ).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    return ShortsAnalyticsSummary(
        published_video_count=len(shorts_items),
        videos_with_metrics_count=videos_with_metrics,
        total_views=total_views,
        total_engaged_views=total_engaged,
        total_swiped_away=total_swiped,
        total_shorts_feed_impressions=total_feed_imp,
        total_watch_time_minutes=total_mins,
        total_watch_time_hours=total_hours,
        average_view_percentage=avg_pct,
        stay_to_watch_rate=stay_rate,
        subscribers_gained=_sum_metric(
            all_selected_snapshots, "subscribers_gained"
        ),
        subscribers_lost=_sum_metric(all_selected_snapshots, "subscribers_lost"),
        videos=tuple(views_list),
    )


def _build_longform_summary(
    published_by_item: dict[str, tuple[ContentItem, str]],
    video_snapshots_by_yt_id: dict[str, list[MetricSnapshot]],
    *,
    period_start: datetime | None,
    period_end: datetime | None,
) -> LongFormAnalyticsSummary:
    lf_items = [
        (item, yt_id)
        for item, yt_id in published_by_item.values()
        if item.content_type is ContentType.LONGFORM
    ]
    views_list: list[LongFormMetricsView] = []
    all_selected_snapshots: list[MetricSnapshot] = []
    videos_with_metrics = 0

    for item, yt_id in lf_items:
        raw = _filter_by_window(
            video_snapshots_by_yt_id.get(yt_id, ()),
            period_start=period_start,
            period_end=period_end,
        )
        deduped = _select_non_overlapping_snapshots(raw)
        if deduped:
            videos_with_metrics += 1
            all_selected_snapshots.extend(deduped)
            views_list.append(
                normalize_longform_metrics(deduped[-1], content_item_id=item.id)
            )

    total_views = _sum_metric(all_selected_snapshots, "views")
    total_impressions = _sum_metric(all_selected_snapshots, "impressions")
    avg_ctr = _average_metric(
        all_selected_snapshots, "impressions_click_through_rate"
    )
    total_mins = _sum_metric(
        all_selected_snapshots, "estimated_minutes_watched"
    )
    total_hours = (
        (total_mins / Decimal("60")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if total_mins is not None
        else None
    )
    avg_pct = _average_metric(all_selected_snapshots, "average_view_percentage")

    return LongFormAnalyticsSummary(
        published_video_count=len(lf_items),
        videos_with_metrics_count=videos_with_metrics,
        total_views=total_views,
        total_impressions=total_impressions,
        impressions_click_through_rate=avg_ctr,
        total_watch_time_minutes=total_mins,
        total_watch_time_hours=total_hours,
        average_view_percentage=avg_pct,
        subscribers_gained=_sum_metric(
            all_selected_snapshots, "subscribers_gained"
        ),
        subscribers_lost=_sum_metric(all_selected_snapshots, "subscribers_lost"),
        videos=tuple(views_list),
    )


def _average_metric(
    snapshots: Sequence[MetricSnapshot], key: str
) -> Decimal | None:
    vals = [
        snap.metrics[key]
        for snap in snapshots
        if snap.metrics.get(key) is not None
    ]
    if not vals:
        return None
    return (sum(vals, Decimal("0")) / Decimal(len(vals))).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )

