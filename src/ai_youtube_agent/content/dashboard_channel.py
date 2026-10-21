"""Channel Overview screen view and service (Prompt Pack v8, prompt Q-193), context X3 Command Center.

Builds the Command Center Channel Overview screen view model and iOS-style HTML
rendering, presenting:
- Channel identity (internal ID, title, YouTube ``UC...`` channel ID, ``@handle``,
  lifecycle status, timestamps, and strategy profile details).
- Subscriber metrics (``subscribers_gained``, ``subscribers_lost``, and
  ``net_subscribers`` across non-overlapping channel metric periods, keeping
  absent metrics ``None`` rather than fabricating ``0``).
- Views and watch-time summary (``views``, watch time in minutes/hours/seconds,
  daily average minutes, average view duration, and engagement totals).
- Content counts broken down by ``ContentType.SHORTS`` and ``ContentType.LONGFORM``
  and across lifecycle states.
"""

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.dashboard_shell import (
    BadgeTone,
    DashboardScreenId,
    DashboardShellService,
    DashboardShellState,
    ViewLoadState,
    _select_channel,
    render_dashboard_shell_html,
)
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.content.subscriber_change import (
    SubscriberChangeError,
    SubscriberChangeErrorCode,
    aggregate_subscriber_changes,
    extract_subscriber_change,
)
from ai_youtube_agent.content.watch_time import (
    TWO_PLACES,
    WatchTimeError,
    WatchTimeErrorCode,
    WatchTimeMeasurement,
    WatchTimeUnit,
    aggregate_watch_time,
    normalize_snapshot_watch_time,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.economics import MetricSnapshotRepository

Clock = Callable[[], datetime]

_IN_PROGRESS_STATUSES: Final[frozenset[ContentStatus]] = frozenset(
    {
        ContentStatus.DRAFT,
        ContentStatus.GENERATING,
        ContentStatus.TESTING,
        ContentStatus.PREVIEW_READY,
        ContentStatus.AWAITING_APPROVAL,
        ContentStatus.APPROVED,
        ContentStatus.PUBLISHING,
    }
)
_FAILED_OR_REJECTED_STATUSES: Final[frozenset[ContentStatus]] = frozenset(
    {
        ContentStatus.REJECTED,
        ContentStatus.FAILED,
    }
)


@dataclass(frozen=True)
class ChannelIdentityCard:
    channel_id: str
    title: str
    youtube_channel_id: str
    handle: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    strategy_configured: bool
    strategy_complete: bool
    missing_settings: tuple[str, ...]
    niche: str | None
    target_audience: str | None
    tone_and_style: str | None
    primary_language: str | None
    time_zone: str | None
    shorts_per_day: int | None
    longform_per_day: int | None
    publish_windows: tuple[str, ...]
    tone: BadgeTone

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "title": self.title,
            "youtube_channel_id": self.youtube_channel_id,
            "handle": self.handle,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "strategy_configured": self.strategy_configured,
            "strategy_complete": self.strategy_complete,
            "missing_settings": list(self.missing_settings),
            "niche": self.niche,
            "target_audience": self.target_audience,
            "tone_and_style": self.tone_and_style,
            "primary_language": self.primary_language,
            "time_zone": self.time_zone,
            "shorts_per_day": self.shorts_per_day,
            "longform_per_day": self.longform_per_day,
            "publish_windows": list(self.publish_windows),
            "tone": self.tone.value,
        }


@dataclass(frozen=True)
class ChannelSubscribersSummary:
    subscribers_gained: Decimal | None
    subscribers_lost: Decimal | None
    net_subscribers: Decimal | None
    included_periods: int
    incomplete_period_count: int
    period_start: datetime | None
    period_end: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
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
            "included_periods": self.included_periods,
            "incomplete_period_count": self.incomplete_period_count,
            "period_start": (
                self.period_start.isoformat() if self.period_start else None
            ),
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }


@dataclass(frozen=True)
class ChannelViewsWatchTimeSummary:
    views: Decimal | None
    watch_time_minutes: Decimal | None
    watch_time_hours: Decimal | None
    watch_time_seconds: Decimal | None
    daily_average_minutes: Decimal | None
    average_view_duration_seconds: Decimal | None
    likes: Decimal | None
    comments: Decimal | None
    shares: Decimal | None
    included_periods: int
    period_start: datetime | None
    period_end: datetime | None
    latest_retrieved_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
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
            "watch_time_seconds": (
                str(self.watch_time_seconds)
                if self.watch_time_seconds is not None
                else None
            ),
            "daily_average_minutes": (
                str(self.daily_average_minutes)
                if self.daily_average_minutes is not None
                else None
            ),
            "average_view_duration_seconds": (
                str(self.average_view_duration_seconds)
                if self.average_view_duration_seconds is not None
                else None
            ),
            "likes": str(self.likes) if self.likes is not None else None,
            "comments": str(self.comments) if self.comments is not None else None,
            "shares": str(self.shares) if self.shares is not None else None,
            "included_periods": self.included_periods,
            "period_start": (
                self.period_start.isoformat() if self.period_start else None
            ),
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "latest_retrieved_at": (
                self.latest_retrieved_at.isoformat()
                if self.latest_retrieved_at
                else None
            ),
        }


@dataclass(frozen=True)
class ChannelContentCounts:
    total_items: int
    shorts_total: int
    longform_total: int
    published_shorts: int
    published_longform: int
    in_progress_shorts: int
    in_progress_longform: int
    failed_or_rejected_shorts: int
    failed_or_rejected_longform: int
    by_status: dict[str, int]
    shorts_by_status: dict[str, int]
    longform_by_status: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_items": self.total_items,
            "shorts_total": self.shorts_total,
            "longform_total": self.longform_total,
            "published_shorts": self.published_shorts,
            "published_longform": self.published_longform,
            "in_progress_shorts": self.in_progress_shorts,
            "in_progress_longform": self.in_progress_longform,
            "failed_or_rejected_shorts": self.failed_or_rejected_shorts,
            "failed_or_rejected_longform": self.failed_or_rejected_longform,
            "by_status": dict(self.by_status),
            "shorts_by_status": dict(self.shorts_by_status),
            "longform_by_status": dict(self.longform_by_status),
        }


@dataclass(frozen=True)
class ChannelOverviewView:
    shell: DashboardShellState
    load_state: ViewLoadState
    identity: ChannelIdentityCard | None
    subscribers: ChannelSubscribersSummary | None
    views_and_watch_time: ChannelViewsWatchTimeSummary | None
    content_counts: ChannelContentCounts
    empty_message: str | None = None

    @property
    def channel_id(self) -> str | None:
        if self.identity:
            return self.identity.channel_id
        return self.shell.active_channel_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "shell": self.shell.as_dict(),
            "load_state": self.load_state.value,
            "identity": self.identity.as_dict() if self.identity else None,
            "subscribers": self.subscribers.as_dict() if self.subscribers else None,
            "views_and_watch_time": (
                self.views_and_watch_time.as_dict()
                if self.views_and_watch_time
                else None
            ),
            "content_counts": self.content_counts.as_dict(),
            "empty_message": self.empty_message,
        }


class ChannelOverviewService:
    """Builds the Channel Overview screen state and renders its HTML."""

    def __init__(
        self,
        database: Database,
        *,
        shell_service: DashboardShellService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
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
    ) -> ChannelOverviewView:
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

        shell = self._shell_service.build_shell(
            DashboardScreenId.CHANNEL,
            channel_id=channel_id,
        )
        connection = self._database.connect()
        try:
            channels = ChannelRepository(connection).list_all()
            selected_channel = _select_channel(channels, channel_id)
            if selected_channel is None:
                return ChannelOverviewView(
                    shell=shell,
                    load_state=ViewLoadState.EMPTY,
                    identity=None,
                    subscribers=None,
                    views_and_watch_time=None,
                    content_counts=_empty_content_counts(),
                    empty_message="No channel selected or configured yet. Create a channel to view its overview.",
                )

            strategy = StrategyProfileRepository(connection).get_by_channel(
                selected_channel.id
            )
            items = ContentItemRepository(connection).list_by_channel(
                selected_channel.id
            )
            snapshots = MetricSnapshotRepository(connection).list_by_subject(
                MetricScope.CHANNEL, selected_channel.id
            )
        finally:
            connection.close()

        filtered_snapshots = [
            snap
            for snap in snapshots
            if (period_start is None or snap.period_start >= period_start)
            and (period_end is None or snap.period_end <= period_end)
        ]
        non_overlapping = _select_non_overlapping_snapshots(filtered_snapshots)

        identity = _build_identity_card(selected_channel, strategy)
        subscribers = _build_subscribers_summary(
            selected_channel.id, non_overlapping
        )
        views_and_watch_time = _build_views_watch_time_summary(
            selected_channel.id, non_overlapping
        )
        content_counts = _build_content_counts(items)

        return ChannelOverviewView(
            shell=shell,
            load_state=ViewLoadState.READY,
            identity=identity,
            subscribers=subscribers,
            views_and_watch_time=views_and_watch_time,
            content_counts=content_counts,
            empty_message=None,
        )

    def render_html(self, view: ChannelOverviewView) -> str:
        if view.load_state is ViewLoadState.EMPTY or view.identity is None:
            msg = html.escape(view.empty_message or "No channel selected.")
            body = (
                '<section class="yt-card" data-testid="channel-empty-state">'
                '<div class="yt-card__header">'
                '<h2 class="yt-card__title">Channel Overview</h2>'
                "</div>"
                f'<p class="yt-empty">{msg}</p>'
                "</section>"
            )
            return render_dashboard_shell_html(
                view.shell,
                body_html=body,
                page_title="Channel Overview — YouTube Command Center",
            )

        ident = view.identity
        subs = view.subscribers
        vw = view.views_and_watch_time
        counts = view.content_counts

        handle_display = html.escape(ident.handle) if ident.handle else "No @handle"
        niche_display = html.escape(ident.niche or "Unconfigured")
        audience_display = html.escape(ident.target_audience or "Unconfigured")
        lang_display = html.escape(ident.primary_language or "—")
        tz_display = html.escape(ident.time_zone or "—")
        windows_display = (
            ", ".join(html.escape(w) for w in ident.publish_windows)
            if ident.publish_windows
            else "—"
        )

        views_text = _fmt_metric(vw.views if vw else None)
        wt_hours_text = _fmt_metric(vw.watch_time_hours if vw else None, suffix=" hrs")
        wt_mins_text = _fmt_metric(
            vw.watch_time_minutes if vw else None, suffix=" min"
        )
        avg_dur_text = _fmt_metric(
            vw.average_view_duration_seconds if vw else None, suffix=" s"
        )

        net_subs_text = _fmt_signed_metric(
            subs.net_subscribers if subs else None
        )
        gained_subs_text = _fmt_metric(
            subs.subscribers_gained if subs else None
        )
        lost_subs_text = _fmt_metric(subs.subscribers_lost if subs else None)

        status_rows = "".join(
            f"<tr><td>{html.escape(status.value)}</td>"
            f"<td>{counts.shorts_by_status.get(status.value, 0)}</td>"
            f"<td>{counts.longform_by_status.get(status.value, 0)}</td>"
            f"<td>{counts.by_status.get(status.value, 0)}</td></tr>"
            for status in ContentStatus
        )

        body = f"""<div class="yt-grid" data-testid="channel-overview-screen">
  <section class="yt-card yt-grid__span-6" data-testid="channel-identity-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Channel Identity</p>
        <h2 class="yt-card__title">{html.escape(ident.title)}</h2>
      </div>
      <span class="yt-pill yt-pill--{ident.tone.value}">{html.escape(ident.status)}</span>
    </div>
    <p class="yt-card__subtitle">{html.escape(ident.youtube_channel_id)} · {handle_display}</p>
    <p class="yt-card__subtitle">Niche: {niche_display} · Audience: {audience_display}</p>
    <p class="yt-card__subtitle">Language: {lang_display} · TZ: {tz_display} · Windows: {windows_display}</p>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="channel-subscribers-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Subscribers</p>
        <h2 class="yt-card__title">Audience Growth</h2>
      </div>
      <span class="yt-pill yt-pill--info">{subs.included_periods if subs else 0} periods</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(net_subs_text)}</span>
      <span class="yt-kpi__unit">net subscribers</span>
    </div>
    <p class="yt-card__subtitle">Gained: {html.escape(gained_subs_text)} · Lost: {html.escape(lost_subs_text)}</p>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="channel-views-watch-time-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Views &amp; Watch Time</p>
        <h2 class="yt-card__title">Channel Reach</h2>
      </div>
      <span class="yt-pill yt-pill--info">{vw.included_periods if vw else 0} periods</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{html.escape(views_text)}</span>
      <span class="yt-kpi__unit">views · {html.escape(wt_hours_text)} ({html.escape(wt_mins_text)})</span>
    </div>
    <p class="yt-card__subtitle">Avg view duration: {html.escape(avg_dur_text)}</p>
  </section>

  <section class="yt-card yt-grid__span-6" data-testid="channel-content-counts-card">
    <div class="yt-card__header">
      <div>
        <p class="yt-card__subtitle">Content Inventory</p>
        <h2 class="yt-card__title">Shorts &amp; LongForm</h2>
      </div>
      <span class="yt-pill yt-pill--neutral">{counts.total_items} total</span>
    </div>
    <div class="yt-kpi">
      <span class="yt-kpi__value">{counts.shorts_total} / {counts.longform_total}</span>
      <span class="yt-kpi__unit">Shorts / LongForm</span>
    </div>
    <p class="yt-card__subtitle">Published: {counts.published_shorts} Shorts · {counts.published_longform} LongForm | In progress: {counts.in_progress_shorts} Shorts · {counts.in_progress_longform} LongForm</p>
    <table class="yt-table" data-testid="channel-content-status-table">
      <thead><tr><th>Lifecycle Status</th><th>Shorts</th><th>LongForm</th><th>Total</th></tr></thead>
      <tbody>{status_rows}</tbody>
    </table>
  </section>
</div>"""
        return render_dashboard_shell_html(
            view.shell,
            body_html=body,
            page_title=f"{ident.title} — Channel Overview",
        )


def _select_non_overlapping_snapshots(
    snapshots: Sequence[MetricSnapshot],
) -> list[MetricSnapshot]:
    """Deduplicate identical windows (keeping newest retrieved_at) and greedily keep non-overlapping windows."""
    by_window: dict[tuple[datetime, datetime], MetricSnapshot] = {}
    for snap in snapshots:
        window = (snap.period_start, snap.period_end)
        prior = by_window.get(window)
        if prior is None or snap.retrieved_at >= prior.retrieved_at:
            by_window[window] = snap

    ordered = [by_window[w] for w in sorted(by_window)]
    selected: list[MetricSnapshot] = []
    for snap in ordered:
        if not selected or snap.period_start >= selected[-1].period_end:
            selected.append(snap)
    return selected


def _build_identity_card(
    channel: Channel, strategy: StrategyProfile | None
) -> ChannelIdentityCard:
    strategy_configured = strategy is not None
    strategy_complete = bool(strategy and strategy.is_complete)
    missing_settings = strategy.missing_settings if strategy else ()

    if channel.status is ChannelStatus.ACTIVE and strategy_complete:
        tone = BadgeTone.SUCCESS
    elif channel.status in (ChannelStatus.PAUSED, ChannelStatus.PENDING):
        tone = BadgeTone.WARNING
    elif channel.status in (ChannelStatus.DISCONNECTED, ChannelStatus.ARCHIVED):
        tone = BadgeTone.DANGER
    else:
        tone = BadgeTone.WARNING

    return ChannelIdentityCard(
        channel_id=channel.id,
        title=channel.title,
        youtube_channel_id=channel.youtube.channel_id,
        handle=channel.youtube.handle,
        status=channel.status.value,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
        strategy_configured=strategy_configured,
        strategy_complete=strategy_complete,
        missing_settings=missing_settings,
        niche=strategy.niche.name if strategy and strategy.niche else None,
        target_audience=(
            strategy.audience.description
            if strategy and strategy.audience
            else None
        ),
        tone_and_style=(
            strategy.brand.tone if strategy and strategy.brand else None
        ),
        primary_language=(
            strategy.languages.primary
            if strategy and strategy.languages
            else None
        ),
        time_zone=(
            strategy.cadence.time_zone
            if strategy and strategy.cadence
            else None
        ),
        shorts_per_day=(
            strategy.cadence.shorts_per_day
            if strategy and strategy.cadence
            else None
        ),
        longform_per_day=(
            strategy.cadence.longform_per_day
            if strategy and strategy.cadence
            else None
        ),
        publish_windows=(
            strategy.cadence.shorts_schedule.times
            if strategy and strategy.cadence
            else ()
        ),
        tone=tone,
    )


def _build_subscribers_summary(
    channel_id: str, snapshots: Sequence[MetricSnapshot]
) -> ChannelSubscribersSummary:
    try:
        agg = aggregate_subscriber_changes(
            snapshots, scope=MetricScope.CHANNEL, subject_id=channel_id
        )
        return ChannelSubscribersSummary(
            subscribers_gained=agg.subscribers_gained,
            subscribers_lost=agg.subscribers_lost,
            net_subscribers=agg.net_subscribers,
            included_periods=agg.included_periods,
            incomplete_period_count=agg.incomplete_period_count,
            period_start=agg.period_start,
            period_end=agg.period_end,
        )
    except SubscriberChangeError as exc:
        if exc.subscriber_code is SubscriberChangeErrorCode.INVALID_SUBSCRIBER_VALUE:
            raise
        views = [extract_subscriber_change(s) for s in snapshots]
        return ChannelSubscribersSummary(
            subscribers_gained=views[-1].subscribers_gained if views else None,
            subscribers_lost=views[-1].subscribers_lost if views else None,
            net_subscribers=views[-1].net_subscribers if views else None,
            included_periods=1 if views else 0,
            incomplete_period_count=0,
            period_start=views[-1].period_start if views else None,
            period_end=views[-1].period_end if views else None,
        )


def _build_views_watch_time_summary(
    channel_id: str, snapshots: Sequence[MetricSnapshot]
) -> ChannelViewsWatchTimeSummary:
    if not snapshots:
        return ChannelViewsWatchTimeSummary(
            views=None,
            watch_time_minutes=None,
            watch_time_hours=None,
            watch_time_seconds=None,
            daily_average_minutes=None,
            average_view_duration_seconds=None,
            likes=None,
            comments=None,
            shares=None,
            included_periods=0,
            period_start=None,
            period_end=None,
            latest_retrieved_at=None,
        )

    try:
        wt_agg = aggregate_watch_time(
            snapshots, scope=MetricScope.CHANNEL, subject_id=channel_id
        )
        wt_measurement = wt_agg.total_watch_time
        daily_avg_mins = wt_agg.daily_average_minutes
    except WatchTimeError as exc:
        if exc.watch_time_code is WatchTimeErrorCode.INVALID_WATCH_TIME:
            raise
        latest_wt = normalize_snapshot_watch_time(snapshots[-1])
        wt_measurement = latest_wt.watch_time
        daily_avg_mins = latest_wt.daily_average_minutes

    views = _sum_metric(snapshots, "views")
    likes = _sum_metric(snapshots, "likes")
    comments = _sum_metric(snapshots, "comments")
    shares = _sum_metric(snapshots, "shares")
    avg_duration = _weighted_or_simple_avg_duration(snapshots)
    latest_retrieved = max(s.retrieved_at for s in snapshots)

    return ChannelViewsWatchTimeSummary(
        views=views,
        watch_time_minutes=wt_measurement.minutes if wt_measurement else None,
        watch_time_hours=wt_measurement.hours if wt_measurement else None,
        watch_time_seconds=wt_measurement.seconds if wt_measurement else None,
        daily_average_minutes=daily_avg_mins,
        average_view_duration_seconds=avg_duration,
        likes=likes,
        comments=comments,
        shares=shares,
        included_periods=len(snapshots),
        period_start=snapshots[0].period_start,
        period_end=snapshots[-1].period_end,
        latest_retrieved_at=latest_retrieved,
    )


def _sum_metric(snapshots: Sequence[MetricSnapshot], key: str) -> Decimal | None:
    total = Decimal("0")
    found = False
    for snap in snapshots:
        val = snap.metrics.get(key)
        if val is not None:
            total += val
            found = True
    return total if found else None


def _weighted_or_simple_avg_duration(
    snapshots: Sequence[MetricSnapshot],
) -> Decimal | None:
    weighted_sum = Decimal("0")
    total_views = Decimal("0")
    simple_values: list[Decimal] = []
    for snap in snapshots:
        dur = snap.metrics.get("average_view_duration")
        if dur is None:
            continue
        simple_values.append(dur)
        v = snap.metrics.get("views")
        if v is not None and v > 0:
            weighted_sum += dur * v
            total_views += v
    if total_views > 0:
        return (weighted_sum / total_views).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if simple_values:
        return (sum(simple_values, Decimal("0")) / Decimal(len(simple_values))).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )
    return None


def _empty_content_counts() -> ChannelContentCounts:
    zero_map = {status.value: 0 for status in ContentStatus}
    return ChannelContentCounts(
        total_items=0,
        shorts_total=0,
        longform_total=0,
        published_shorts=0,
        published_longform=0,
        in_progress_shorts=0,
        in_progress_longform=0,
        failed_or_rejected_shorts=0,
        failed_or_rejected_longform=0,
        by_status=dict(zero_map),
        shorts_by_status=dict(zero_map),
        longform_by_status=dict(zero_map),
    )


def _build_content_counts(items: Sequence[ContentItem]) -> ChannelContentCounts:
    by_status = {status.value: 0 for status in ContentStatus}
    shorts_by_status = {status.value: 0 for status in ContentStatus}
    longform_by_status = {status.value: 0 for status in ContentStatus}

    shorts_total = 0
    longform_total = 0
    published_shorts = 0
    published_longform = 0
    in_progress_shorts = 0
    in_progress_longform = 0
    failed_shorts = 0
    failed_longform = 0

    for item in items:
        by_status[item.status.value] = by_status.get(item.status.value, 0) + 1
        if item.content_type is ContentType.SHORTS:
            shorts_total += 1
            shorts_by_status[item.status.value] = (
                shorts_by_status.get(item.status.value, 0) + 1
            )
            if item.status is ContentStatus.PUBLISHED:
                published_shorts += 1
            elif item.status in _IN_PROGRESS_STATUSES:
                in_progress_shorts += 1
            elif item.status in _FAILED_OR_REJECTED_STATUSES:
                failed_shorts += 1
        elif item.content_type is ContentType.LONGFORM:
            longform_total += 1
            longform_by_status[item.status.value] = (
                longform_by_status.get(item.status.value, 0) + 1
            )
            if item.status is ContentStatus.PUBLISHED:
                published_longform += 1
            elif item.status in _IN_PROGRESS_STATUSES:
                in_progress_longform += 1
            elif item.status in _FAILED_OR_REJECTED_STATUSES:
                failed_longform += 1

    return ChannelContentCounts(
        total_items=len(items),
        shorts_total=shorts_total,
        longform_total=longform_total,
        published_shorts=published_shorts,
        published_longform=published_longform,
        in_progress_shorts=in_progress_shorts,
        in_progress_longform=in_progress_longform,
        failed_or_rejected_shorts=failed_shorts,
        failed_or_rejected_longform=failed_longform,
        by_status=by_status,
        shorts_by_status=shorts_by_status,
        longform_by_status=longform_by_status,
    )


def _fmt_metric(value: Decimal | None, *, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value}{suffix}"


def _fmt_signed_metric(value: Decimal | None) -> str:
    if value is None:
        return "—"
    if value > 0:
        return f"+{value}"
    return str(value)
