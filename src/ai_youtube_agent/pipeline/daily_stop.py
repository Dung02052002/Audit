"""Daily Production Stop Service (Prompt Pack v8, task R-210).

Monitors daily production usage against configured channel cadence limits,
stopping new production starts once daily thresholds are reached for the calendar day.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.daily_limit_gate import day_window
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.usage import DailyUsageRepository

Clock = Callable[[], datetime]
DAILY_STOP_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "daily_stop_service")


@dataclass(frozen=True)
class DailyStopEvaluation:
    channel_id: str
    content_type: ContentType
    is_stopped: bool
    used_today: int
    daily_limit: int
    remaining_today: int
    time_zone: str
    window_start: datetime
    window_end: datetime
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "is_stopped": self.is_stopped,
            "used_today": self.used_today,
            "daily_limit": self.daily_limit,
            "remaining_today": self.remaining_today,
            "time_zone": self.time_zone,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ChannelDailyStopSummary:
    channel_id: str
    channel_title: str
    shorts_evaluation: DailyStopEvaluation
    longform_evaluation: DailyStopEvaluation

    @property
    def is_any_stopped(self) -> bool:
        return self.shorts_evaluation.is_stopped or self.longform_evaluation.is_stopped

    @property
    def is_all_stopped(self) -> bool:
        return self.shorts_evaluation.is_stopped and self.longform_evaluation.is_stopped

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "shorts": self.shorts_evaluation.as_dict(),
            "longform": self.longform_evaluation.as_dict(),
            "is_any_stopped": self.is_any_stopped,
            "is_all_stopped": self.is_all_stopped,
        }


class DailyStopService:
    """Evaluates whether new production should stop based on daily cadence limits."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def check_channel_limit(
        self,
        channel_id: str,
        content_type: ContentType,
    ) -> DailyStopEvaluation:
        """Evaluates whether production should be stopped for the given channel and content type."""
        now = self._now()
        with self._database.transaction() as conn:
            strategy_repo = StrategyProfileRepository(conn)
            strategy = strategy_repo.get_by_channel(channel_id)

            if strategy is None or strategy.cadence is None:
                start, end = day_window(now, "UTC")
                return DailyStopEvaluation(
                    channel_id=channel_id,
                    content_type=content_type,
                    is_stopped=True,
                    used_today=0,
                    daily_limit=0,
                    remaining_today=0,
                    time_zone="UTC",
                    window_start=start,
                    window_end=end,
                    reason=(
                        "Channel has no strategy configured"
                        if strategy is None
                        else "Channel has no cadence configured"
                    ),
                )

            time_zone = strategy.cadence.time_zone
            limit = (
                strategy.cadence.shorts_per_day
                if content_type is ContentType.SHORTS
                else strategy.cadence.longform_per_day
            )
            start, end = day_window(now, time_zone)

            usage_repo = DailyUsageRepository(conn)
            used = usage_repo.count_production_starts(
                channel_id=channel_id,
                content_type=content_type,
                start=start,
                end=end,
            )

            if limit <= 0:
                return DailyStopEvaluation(
                    channel_id=channel_id,
                    content_type=content_type,
                    is_stopped=True,
                    used_today=used,
                    daily_limit=limit,
                    remaining_today=0,
                    time_zone=time_zone,
                    window_start=start,
                    window_end=end,
                    reason=f"Daily limit is set to {limit}; production stopped",
                )

            if used >= limit:
                return DailyStopEvaluation(
                    channel_id=channel_id,
                    content_type=content_type,
                    is_stopped=True,
                    used_today=used,
                    daily_limit=limit,
                    remaining_today=0,
                    time_zone=time_zone,
                    window_start=start,
                    window_end=end,
                    reason=(
                        f"Daily {content_type.value} production limit reached "
                        f"({used}/{limit}) for {time_zone} calendar day"
                    ),
                )

            return DailyStopEvaluation(
                channel_id=channel_id,
                content_type=content_type,
                is_stopped=False,
                used_today=used,
                daily_limit=limit,
                remaining_today=max(0, limit - used),
                time_zone=time_zone,
                window_start=start,
                window_end=end,
                reason=None,
            )

    def should_stop(self, channel_id: str, content_type: ContentType) -> bool:
        """Returns True if new production should be stopped."""
        evaluation = self.check_channel_limit(channel_id, content_type)
        return evaluation.is_stopped

    def check_channel(self, channel_id: str) -> ChannelDailyStopSummary:
        """Evaluates daily stop limits for both Shorts and LongForm on a channel."""
        with self._database.transaction() as conn:
            channel = ChannelRepository(conn).get(channel_id)
            title = channel.title if channel else channel_id

        shorts_eval = self.check_channel_limit(channel_id, ContentType.SHORTS)
        longform_eval = self.check_channel_limit(channel_id, ContentType.LONGFORM)

        return ChannelDailyStopSummary(
            channel_id=channel_id,
            channel_title=title,
            shorts_evaluation=shorts_eval,
            longform_evaluation=longform_eval,
        )

    def check_all_channels(self) -> list[ChannelDailyStopSummary]:
        """Evaluates daily stop status across all active channels."""
        with self._database.transaction() as conn:
            channels = ChannelRepository(conn).list_all()

        return [
            self.check_channel(c.id)
            for c in channels
            if c.status is ChannelStatus.ACTIVE
        ]

    def record_stop_event(
        self,
        channel_id: str,
        content_type: ContentType,
        evaluation: DailyStopEvaluation,
    ) -> None:
        """Audits that a daily stop threshold has been reached."""
        self._audit.record(
            action="orchestrator.daily_stop_triggered",
            actor=DAILY_STOP_ACTOR,
            entity=EntityRef("channels", channel_id),
            result=AuditResult.DENIED,
            metadata={
                "content_type": content_type.value,
                "used_today": evaluation.used_today,
                "daily_limit": evaluation.daily_limit,
                "time_zone": evaluation.time_zone,
                "reason": evaluation.reason,
            },
        )
