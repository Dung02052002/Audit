"""Job Scheduler (Prompt Pack v8, prompt #205), context C16 Orchestration.

``JobScheduler`` schedules eligible pipeline jobs without exceeding configured
channel cadence limits, daily production starts, or budget constraints:

- Reads channel status, feature flags, kill switch, strategy cadence, daily usage,
  and budget guard before scheduling.
- Refuses inactive channels (archived, paused, disconnected, pending), disabled
  content types (``SHORTS_ENABLED``, ``LONGFORM_ENABLED``), active emergency stops,
  channels without configured strategy/cadence, and channels that have reached their
  daily production limit for the calendar day in their cadence time zone.
- Transitions eligible draft content items to ``GENERATING`` via ``start_production``
  and enqueues an ``AIJob`` (kind ``content.generate``) into ``JobQueue``.
- Provides batch scheduling for a single channel (``schedule_channel``) or across
  all active channels (``schedule_all_active_channels``).
- Calculates remaining daily capacity per channel and content type.
- Audits every scheduled job with ``job.scheduled`` (or blocked attempts).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
import uuid

from ai_youtube_agent.content.budget_guard import BudgetGuard
from ai_youtube_agent.content.channel import Channel, ChannelStatus
from ai_youtube_agent.content.strategy import StrategyProfile
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.daily_limit_gate import day_window
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.usage import DailyUsageRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.flags import FeatureFlags
from ai_youtube_agent.core.kill_switch_gate import KillSwitchSource
from ai_youtube_agent.core.production import start_production
from ai_youtube_agent.pipeline.idempotency import (
    GENERATION_KIND,
    generation_key,
)
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import JobQueue

Clock = Callable[[], datetime]

JOB_SCHEDULED_ACTION: Final[str] = "job.scheduled"
JOB_SCHEDULE_BLOCKED_ACTION: Final[str] = "job.schedule_blocked"
SCHEDULER_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "job_scheduler")


class JobSchedulerError(DomainError):
    """Base error for job scheduler operations."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code=code, detail=detail, user_message=detail)


@dataclass(frozen=True)
class ScheduleEligibility:
    """Eligibility check outcome for a content item."""

    eligible: bool
    code: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ScheduleDecision:
    """Scheduling decision for a specific content item."""

    item_id: str
    channel_id: str
    content_type: ContentType
    scheduled: bool
    job_id: str | None = None
    session_id: str | None = None
    code: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "item_id": self.item_id,
            "channel_id": self.channel_id,
            "content_type": self.content_type.value,
            "scheduled": self.scheduled,
            "job_id": self.job_id,
            "session_id": self.session_id,
            "code": self.code,
            "reason": self.reason,
        }


class JobScheduler:
    """Schedules eligible pipeline jobs without exceeding limits."""

    def __init__(
        self,
        database: Database,
        job_queue: JobQueue,
        audit_log: AuditLog,
        *,
        budget_guard: BudgetGuard | None = None,
        kill_switch: KillSwitchSource | None = None,
        flags: FeatureFlags | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._queue = job_queue
        self._audit = audit_log
        self._budget_guard = budget_guard
        self._kill_switch = kill_switch
        self._flags = flags
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def evaluate_eligibility(
        self,
        item: ContentItem,
        *,
        at: datetime | None = None,
    ) -> ScheduleEligibility:
        """Evaluate if an item is eligible for production scheduling."""
        now = at or self._now()

        # 1. Status check: only DRAFT items can be scheduled for production start
        if item.status in {
            ContentStatus.PUBLISHED,
            ContentStatus.REJECTED,
            ContentStatus.FAILED,
        }:
            return ScheduleEligibility(
                False,
                code="terminal_status",
                reason=f"Item is in terminal status {item.status.value}",
            )
        if item.status is not ContentStatus.DRAFT:
            return ScheduleEligibility(
                False,
                code="not_draft",
                reason=f"Item is in status {item.status.value}, expected draft",
            )

        # 2. Feature flags check
        if self._flags is not None:
            if item.content_type is ContentType.LONGFORM and not self._flags.longform_enabled:
                return ScheduleEligibility(
                    False,
                    code="longform_disabled",
                    reason="LongForm content type is disabled by feature flags",
                )
            if item.content_type is ContentType.SHORTS and not self._flags.shorts_enabled:
                return ScheduleEligibility(
                    False,
                    code="shorts_disabled",
                    reason="Shorts content type is disabled by feature flags",
                )

        # 3. Emergency stop / kill switch check
        if self._kill_switch is not None:
            stop = self._kill_switch.current()
            if stop.active:
                reason = f"Emergency stop is active: {stop.reason or 'operations paused'}"
                return ScheduleEligibility(
                    False,
                    code="killswitch_active",
                    reason=reason,
                )

        # 4. Database-backed checks (Channel, Strategy, Cadence, Usage, In-flight jobs)
        with self._database.connection() as conn:
            channel = ChannelRepository(conn).get(item.channel_id)
            if channel is None:
                return ScheduleEligibility(
                    False,
                    code="channel_not_found",
                    reason=f"Channel {item.channel_id} not found",
                )
            if channel.status is ChannelStatus.PAUSED:
                return ScheduleEligibility(
                    False,
                    code="channel_paused",
                    reason=f"Channel {item.channel_id} is paused",
                )
            if channel.status is ChannelStatus.ARCHIVED:
                return ScheduleEligibility(
                    False,
                    code="channel_archived",
                    reason=f"Channel {item.channel_id} is archived",
                )
            if channel.status is ChannelStatus.DISCONNECTED:
                return ScheduleEligibility(
                    False,
                    code="channel_disconnected",
                    reason=f"Channel {item.channel_id} is disconnected",
                )
            if channel.status is ChannelStatus.PENDING:
                return ScheduleEligibility(
                    False,
                    code="channel_pending",
                    reason=f"Channel {item.channel_id} is pending onboarding",
                )

            strategy = StrategyProfileRepository(conn).get_by_channel(item.channel_id)
            if strategy is None:
                return ScheduleEligibility(
                    False,
                    code="no_strategy",
                    reason=f"Channel {item.channel_id} has no strategy profile configured",
                )
            if strategy.cadence is None:
                return ScheduleEligibility(
                    False,
                    code="no_cadence",
                    reason=f"Channel {item.channel_id} has no cadence configured",
                )

            limit = (
                strategy.cadence.shorts_per_day
                if item.content_type is ContentType.SHORTS
                else strategy.cadence.longform_per_day
            )
            if limit <= 0:
                return ScheduleEligibility(
                    False,
                    code="daily_limit_zero",
                    reason=f"Daily limit for {item.content_type.value} is 0",
                )

            start, end = day_window(now, strategy.cadence.time_zone)
            used = DailyUsageRepository(conn).count_production_starts(
                item.channel_id, item.content_type, start, end
            )
            if used >= limit:
                return ScheduleEligibility(
                    False,
                    code="daily_production_limit_reached",
                    reason=(
                        f"The daily {item.content_type.value} production limit of {limit} "
                        f"has been reached for this channel ({used} already started today)"
                    ),
                )

            # In-flight job check: do not schedule if an active job already exists for item
            existing_jobs = AIJobRepository(conn).list_by_content_item(item.id)
            for j in existing_jobs:
                if j.status in (AIJobStatus.QUEUED, AIJobStatus.RUNNING, AIJobStatus.WAITING):
                    return ScheduleEligibility(
                        False,
                        code="already_scheduled",
                        reason=f"AI job {j.id} is already {j.status.value} for item {item.id}",
                    )

        # 5. Budget check
        if self._budget_guard is not None:
            try:
                b_status = self._budget_guard.evaluate(item.channel_id, at=now)
                if not b_status.allowed:
                    codes = ", ".join(r.code for r in b_status.blocking_reasons)
                    return ScheduleEligibility(
                        False,
                        code="budget_exceeded",
                        reason=f"Channel budget limits exceeded: {codes}",
                    )
            except DomainError as err:
                return ScheduleEligibility(
                    False,
                    code="budget_error",
                    reason=str(err),
                )

        return ScheduleEligibility(True)

    def schedule_item(
        self,
        item: ContentItem,
        *,
        session_id: str | None = None,
        actor: Actor | None = None,
        at: datetime | None = None,
    ) -> ScheduleDecision:
        """Evaluate and schedule a single content item for production if eligible."""
        now = at or self._now()
        audit_actor = actor or SCHEDULER_ACTOR

        eligibility = self.evaluate_eligibility(item, at=now)
        if not eligibility.eligible:
            self._audit.record(
                action=JOB_SCHEDULE_BLOCKED_ACTION,
                actor=audit_actor,
                entity=EntityRef("content_item", item.id),
                result=AuditResult.DENIED,
                metadata={
                    "channel_id": item.channel_id,
                    "content_type": item.content_type.value,
                    "code": eligibility.code or "",
                    "reason": eligibility.reason or "",
                },
            )
            return ScheduleDecision(
                item_id=item.id,
                channel_id=item.channel_id,
                content_type=item.content_type,
                scheduled=False,
                code=eligibility.code,
                reason=eligibility.reason,
            )

        # Execute transition and enqueue
        job_id = str(uuid.uuid4())
        with self._database.transaction() as conn:
            # Re-read item inside transaction for race prevention
            current_item = ContentItemRepository(conn).get(item.id)
            if current_item is None or current_item.status is not ContentStatus.DRAFT:
                return ScheduleDecision(
                    item_id=item.id,
                    channel_id=item.channel_id,
                    content_type=item.content_type,
                    scheduled=False,
                    code="item_not_draft",
                    reason="Content item is no longer in draft status",
                )

            moved, _ = start_production(conn, current_item, clock=lambda: now)
            key = generation_key(moved, kind=GENERATION_KIND)
            job = AIJob(
                id=job_id,
                kind=GENERATION_KIND,
                idempotency_key=key,
                content_item_id=moved.id,
                session_id=session_id,
                status=AIJobStatus.QUEUED,
                attempts=0,
                last_error=None,
                checkpoints=(),
                created_at=now,
                updated_at=now,
            )
            AIJobRepository(conn).add(job)

        self._audit.record(
            action=JOB_SCHEDULED_ACTION,
            actor=audit_actor,
            entity=EntityRef("content_item", item.id),
            result=AuditResult.SUCCESS,
            metadata={
                "job_id": job_id,
                "channel_id": item.channel_id,
                "content_type": item.content_type.value,
                "session_id": session_id or "",
            },
        )

        return ScheduleDecision(
            item_id=item.id,
            channel_id=item.channel_id,
            content_type=item.content_type,
            scheduled=True,
            job_id=job_id,
            session_id=session_id,
        )

    def remaining_capacity(
        self,
        channel_id: str,
        content_type: ContentType,
        *,
        at: datetime | None = None,
    ) -> int:
        """Return remaining production capacity for channel and type today."""
        now = at or self._now()

        # Flags check
        if self._flags is not None:
            if content_type is ContentType.LONGFORM and not self._flags.longform_enabled:
                return 0
            if content_type is ContentType.SHORTS and not self._flags.shorts_enabled:
                return 0

        # Kill switch check
        if self._kill_switch is not None and self._kill_switch.current().active:
            return 0

        # Budget check
        if self._budget_guard is not None:
            try:
                b_status = self._budget_guard.evaluate(channel_id, at=now)
                if not b_status.allowed:
                    return 0
            except DomainError:
                return 0

        with self._database.connection() as conn:
            channel = ChannelRepository(conn).get(channel_id)
            if channel is None or channel.status is not ChannelStatus.ACTIVE:
                return 0
            strategy = StrategyProfileRepository(conn).get_by_channel(channel_id)
            if strategy is None or strategy.cadence is None:
                return 0

            limit = (
                strategy.cadence.shorts_per_day
                if content_type is ContentType.SHORTS
                else strategy.cadence.longform_per_day
            )
            if limit <= 0:
                return 0

            start, end = day_window(now, strategy.cadence.time_zone)
            used = DailyUsageRepository(conn).count_production_starts(
                channel_id, content_type, start, end
            )
            return max(0, limit - used)

    def schedule_channel(
        self,
        channel_id: str,
        *,
        session_id: str | None = None,
        actor: Actor | None = None,
        limit: int | None = None,
        at: datetime | None = None,
    ) -> list[ScheduleDecision]:
        """Schedule eligible draft items for a single channel up to remaining capacity."""
        now = at or self._now()
        with self._database.connection() as conn:
            items = ContentItemRepository(conn).list_by_channel(channel_id)

        drafts = [it for it in items if it.status is ContentStatus.DRAFT]
        decisions: list[ScheduleDecision] = []
        scheduled_count = 0

        for item in drafts:
            if limit is not None and scheduled_count >= limit:
                break

            decision = self.schedule_item(
                item, session_id=session_id, actor=actor, at=now
            )
            decisions.append(decision)
            if decision.scheduled:
                scheduled_count += 1
            elif decision.code in ("daily_production_limit_reached", "budget_exceeded", "killswitch_active"):
                # If hard limits or emergency stops triggered, further scheduling for this channel will fail
                break

        return decisions

    def schedule_all_active_channels(
        self,
        *,
        session_id: str | None = None,
        actor: Actor | None = None,
        limit_per_channel: int | None = None,
        at: datetime | None = None,
    ) -> list[ScheduleDecision]:
        """Schedule eligible items across all active channels."""
        now = at or self._now()
        with self._database.connection() as conn:
            channels = ChannelRepository(conn).list_all()

        all_decisions: list[ScheduleDecision] = []
        for channel in channels:
            if channel.status is ChannelStatus.ACTIVE:
                channel_decisions = self.schedule_channel(
                    channel.id,
                    session_id=session_id,
                    actor=actor,
                    limit=limit_per_channel,
                    at=now,
                )
                all_decisions.extend(channel_decisions)

        return all_decisions
