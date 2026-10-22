"""Graceful Shutdown (Prompt Pack v8, prompt #212), context C16 Orchestration.

Finishes safe in-flight operations, checkpoints running jobs, stops active
sessions cleanly, runs registered shutdown hooks, and persists state before exit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJobStatus, SessionStatus
from ai_youtube_agent.pipeline.job_queue import JobQueue

Clock = Callable[[], datetime]
ShutdownHook = Callable[[], None]


class GracefulShutdownError(DomainError):
    """Raised when graceful shutdown encounters an invalid request or state."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.graceful_shutdown_error",
        user_message: str = "Graceful shutdown failed.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


@dataclass(frozen=True)
class GracefulShutdownReport:
    """Summary of state persisted during graceful shutdown."""

    initiated_by: Actor
    shutdown_at: datetime
    reason: str
    checkpointed_job_ids: tuple[str, ...]
    waiting_job_ids: tuple[str, ...]
    queued_job_ids: tuple[str, ...]
    stopped_session_ids: tuple[str, ...]
    hooks_ran: int
    hook_errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "initiated_by": {
                "kind": self.initiated_by.kind.value,
                "id": self.initiated_by.id,
            },
            "shutdown_at": self.shutdown_at.isoformat(),
            "reason": self.reason,
            "checkpointed_job_ids": list(self.checkpointed_job_ids),
            "waiting_job_ids": list(self.waiting_job_ids),
            "queued_job_ids": list(self.queued_job_ids),
            "stopped_session_ids": list(self.stopped_session_ids),
            "hooks_ran": self.hooks_ran,
            "hook_errors": list(self.hook_errors),
        }


class GracefulShutdownService:
    """Coordinates clean shutdown of the orchestrator runtime."""

    def __init__(
        self,
        database: Database,
        queue: JobQueue,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._queue = queue
        self._audit = audit
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._shutting_down = False
        self._last_report: GracefulShutdownReport | None = None
        self._hooks: list[tuple[str, ShutdownHook]] = []

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def last_report(self) -> GracefulShutdownReport | None:
        return self._last_report

    def register_hook(self, name: str, hook: ShutdownHook) -> None:
        """Register a synchronous cleanup callback to run before DB state flush."""
        cleaned = name.strip() if isinstance(name, str) else ""
        if not cleaned:
            raise GracefulShutdownError("Hook name must be a non-empty string.")
        if not callable(hook):
            raise GracefulShutdownError("Hook must be callable.")
        self._hooks.append((cleaned, hook))

    def ensure_accepting_work(self) -> None:
        """Raise if the runtime has already entered graceful shutdown."""
        if self._shutting_down:
            raise GracefulShutdownError(
                "Runtime is shutting down and not accepting new work.",
                code="domain.shutdown_in_progress",
                user_message="The system is shutting down and cannot start new work.",
            )

    def shutdown(
        self,
        actor: Actor,
        *,
        reason: str = "graceful_shutdown",
    ) -> GracefulShutdownReport:
        """Execute shutdown hooks, checkpoint running jobs, and stop active sessions."""
        if not isinstance(actor, Actor):
            raise GracefulShutdownError("actor must be an Actor.")
        cleaned_reason = reason.strip() if isinstance(reason, str) else ""
        if not cleaned_reason:
            raise GracefulShutdownError("Shutdown reason must not be blank.")

        self._shutting_down = True
        now = self._clock()

        hooks_ran = 0
        hook_errors: list[str] = []
        for hook_name, hook in self._hooks:
            hooks_ran += 1
            try:
                hook()
            except Exception as exc:  # noqa: BLE001
                hook_errors.append(f"{hook_name}: {type(exc).__name__}")

        checkpointed_ids: list[str] = []
        waiting_ids: list[str] = []
        queued_ids: list[str] = []
        stopped_session_ids: list[str] = []

        stop_actor = (
            actor
            if actor.kind is ActorKind.USER
            else Actor(ActorKind.USER, f"shutdown:{actor.id}")
        )

        with self._database.transaction() as conn:
            job_repo = AIJobRepository(conn)
            session_repo = SessionRepository(conn)

            for job in job_repo.list_all():
                if job.status is AIJobStatus.RUNNING:
                    at = max(now, job.updated_at)
                    updated_job = job.record_checkpoint(
                        "runtime.shutdown_checkpoint",
                        {
                            "reason": cleaned_reason,
                            "actor_id": actor.id,
                            "shutdown_at": now.isoformat(),
                            "safe_to_resume": True,
                        },
                        clock=lambda at=at: at,
                    )
                    job_repo.update(updated_job, expected_updated_at=job.updated_at)
                    checkpointed_ids.append(job.id)
                elif job.status is AIJobStatus.WAITING:
                    waiting_ids.append(job.id)
                elif job.status is AIJobStatus.QUEUED:
                    queued_ids.append(job.id)

            for session in session_repo.list_all():
                if session.status is SessionStatus.ACTIVE:
                    at = max(now, session.started_at)
                    stopped = session.stop(
                        stop_actor,
                        clock=lambda at=at: at,
                    )
                    session_repo.update(
                        stopped,
                        expected_status=SessionStatus.ACTIVE,
                    )
                    stopped_session_ids.append(session.id)

        report = GracefulShutdownReport(
            initiated_by=actor,
            shutdown_at=now,
            reason=cleaned_reason,
            checkpointed_job_ids=tuple(checkpointed_ids),
            waiting_job_ids=tuple(waiting_ids),
            queued_job_ids=tuple(queued_ids),
            stopped_session_ids=tuple(stopped_session_ids),
            hooks_ran=hooks_ran,
            hook_errors=tuple(hook_errors),
        )
        self._last_report = report

        self._audit.record(
            action="orchestrator.graceful_shutdown",
            actor=actor,
            entity=EntityRef("orchestrator", "runtime"),
            result=AuditResult.SUCCESS if not hook_errors else AuditResult.FAILURE,
            metadata={
                "reason": cleaned_reason,
                "checkpointed_jobs": len(checkpointed_ids),
                "waiting_jobs": len(waiting_ids),
                "queued_jobs": len(queued_ids),
                "stopped_sessions": len(stopped_session_ids),
                "hooks_ran": hooks_ran,
                "hook_error_count": len(hook_errors),
            },
        )
        return report
