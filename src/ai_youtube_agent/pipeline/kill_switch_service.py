"""Kill Switch Service (Prompt Pack v8, prompt #213), context C16 Orchestration.

Provides the runtime emergency stop toggle and ``KillSwitchSource`` store so new
production and publish actions stop immediately when activated.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ai_youtube_agent.core.audit import Actor, ActorKind, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.core.kill_switch_gate import STOP_MESSAGE, KillSwitchGate
from ai_youtube_agent.pipeline.job import AIJobStatus
from ai_youtube_agent.pipeline.kill_switch import EmergencyStop

Clock = Callable[[], datetime]


class KillSwitchError(DomainError):
    """Base error for kill switch operations."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.killswitch_error",
        user_message: str = "Emergency stop operation failed.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


class KillSwitchNotAllowedError(KillSwitchError):
    """Raised when an unauthorized actor attempts to toggle the kill switch."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            detail,
            code="domain.killswitch_not_allowed",
            user_message="Only an authorized human operator may perform this kill switch action.",
        )


class KillSwitchBlockedError(KillSwitchError):
    """Raised when an operation is blocked by an active emergency stop."""

    def __init__(self, stop: EmergencyStop) -> None:
        message = STOP_MESSAGE
        if stop.reason:
            message = f"{message} Reason: {stop.reason}"
        super().__init__(
            message,
            code="domain.killswitch_active",
            user_message=message,
        )
        self.stop = stop


@dataclass(frozen=True)
class KillSwitchTransition:
    """Record of an emergency stop state change."""

    previous: EmergencyStop
    current: EmergencyStop
    changed_by: Actor
    changed_at: datetime
    cancelled_job_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "previous": self.previous.as_dict(),
            "current": self.current.as_dict(),
            "changed_by": {
                "kind": self.changed_by.kind.value,
                "id": self.changed_by.id,
            },
            "changed_at": self.changed_at.isoformat(),
            "cancelled_job_ids": list(self.cancelled_job_ids),
        }


class KillSwitchService:
    """Thread-safe emergency stop store and controller implementing ``KillSwitchSource``."""

    def __init__(
        self,
        audit: AuditLog,
        *,
        database: Database | None = None,
        initial: EmergencyStop | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._audit = audit
        self._database = database
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._state: EmergencyStop = initial or EmergencyStop.inactive()
        self._history: list[KillSwitchTransition] = []

    def current(self) -> EmergencyStop:
        """Return the current ``EmergencyStop`` snapshot (satisfies ``KillSwitchSource``)."""
        with self._lock:
            return self._state

    @property
    def is_active(self) -> bool:
        return self.current().active

    def history(self) -> tuple[KillSwitchTransition, ...]:
        with self._lock:
            return tuple(self._history)

    def gate(self) -> KillSwitchGate:
        """Build a ``KillSwitchGate`` bound to this service."""
        return KillSwitchGate(self)

    def ensure_inactive(
        self,
        *,
        actor: Actor | None = None,
        action_name: str = "production_or_publish",
    ) -> EmergencyStop:
        """Raise ``KillSwitchBlockedError`` immediately if emergency stop is active."""
        stop = self.current()
        if stop.active:
            if actor is not None:
                self._audit.record(
                    action="killswitch.blocked",
                    actor=actor,
                    entity=EntityRef("kill_switch", "global"),
                    result=AuditResult.DENIED,
                    metadata={
                        "attempted_action": action_name,
                        "stop_reason": stop.reason or "unspecified",
                    },
                )
            raise KillSwitchBlockedError(stop)
        return stop

    def activate(
        self,
        actor: Actor,
        *,
        reason: str | None = None,
        cancel_queued_jobs: bool = False,
    ) -> KillSwitchTransition:
        """Activate the emergency stop immediately.

        Human users and system safety monitors may activate the kill switch;
        AI actors may never toggle the kill switch.
        """
        if not isinstance(actor, Actor):
            raise KillSwitchError("actor must be an Actor.")
        if actor.kind is ActorKind.AI:
            self._audit.record(
                action="killswitch.activate_denied",
                actor=actor,
                entity=EntityRef("kill_switch", "global"),
                result=AuditResult.DENIED,
                metadata={"reason": "ai_actor_not_allowed"},
            )
            raise KillSwitchNotAllowedError(
                "AI actors cannot toggle the emergency kill switch."
            )

        now = self._clock()
        new_stop = EmergencyStop.activated(
            actor,
            reason=reason,
            clock=lambda: now,
        )

        with self._lock:
            previous = self._state
            self._state = new_stop

        cancelled_ids: list[str] = []
        if cancel_queued_jobs and self._database is not None:
            cancel_reason = (
                f"kill_switch: {new_stop.reason}"
                if new_stop.reason
                else "kill_switch_activated"
            )
            with self._database.transaction() as conn:
                job_repo = AIJobRepository(conn)
                for job in job_repo.list_all():
                    if job.status is AIJobStatus.QUEUED:
                        cancelled = job.cancel(
                            reason=cancel_reason,
                            clock=lambda: max(now, job.updated_at),
                        )
                        job_repo.update(cancelled, expected_updated_at=job.updated_at)
                        cancelled_ids.append(job.id)

        transition = KillSwitchTransition(
            previous=previous,
            current=new_stop,
            changed_by=actor,
            changed_at=now,
            cancelled_job_ids=tuple(cancelled_ids),
        )
        with self._lock:
            self._history.append(transition)

        self._audit.record(
            action="killswitch.activated",
            actor=actor,
            entity=EntityRef("kill_switch", "global"),
            result=AuditResult.SUCCESS,
            metadata={
                "reason": new_stop.reason or "unspecified",
                "was_active": previous.active,
                "cancelled_queued_jobs": len(cancelled_ids),
            },
        )
        return transition

    def deactivate(
        self,
        actor: Actor,
        *,
        reason: str | None = None,
    ) -> KillSwitchTransition:
        """Deactivate the emergency stop.

        Strictly restricted to ``ActorKind.USER`` so automated systems or AI
        cannot clear an emergency stop without explicit human action.
        """
        if not isinstance(actor, Actor):
            raise KillSwitchError("actor must be an Actor.")
        if actor.kind is not ActorKind.USER:
            self._audit.record(
                action="killswitch.deactivate_denied",
                actor=actor,
                entity=EntityRef("kill_switch", "global"),
                result=AuditResult.DENIED,
                metadata={"actor_kind": actor.kind.value},
            )
            raise KillSwitchNotAllowedError(
                "Only a human user (ActorKind.USER) may deactivate the emergency stop."
            )

        cleaned_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "manual_reset"
        now = self._clock()
        inactive_stop = EmergencyStop.inactive()

        with self._lock:
            previous = self._state
            self._state = inactive_stop
            transition = KillSwitchTransition(
                previous=previous,
                current=inactive_stop,
                changed_by=actor,
                changed_at=now,
            )
            self._history.append(transition)

        self._audit.record(
            action="killswitch.deactivated",
            actor=actor,
            entity=EntityRef("kill_switch", "global"),
            result=AuditResult.SUCCESS,
            metadata={
                "reason": cleaned_reason,
                "was_active": previous.active,
            },
        )
        return transition
