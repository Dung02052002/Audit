"""Session Manager for User-Initiated Working Sessions (Prompt Pack v8, task R-209).

Coordinates starting, stopping, tracking, and auditing user-initiated sessions (context C16).
Only a human user (ActorKind.USER) may start a session.
"""

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
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository, SessionRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import (
    AIJob,
    AIJobStatus,
    Session,
    SessionNotAllowedError,
    SessionStatus,
)

Clock = Callable[[], datetime]


class SessionNotFoundError(DomainError):
    default_code = "domain.session_not_found"
    default_user_message = "The requested session was not found."


class SessionConflictError(DomainError):
    default_code = "domain.session_conflict"
    default_user_message = "An active session already exists."


@dataclass(frozen=True)
class SessionSummary:
    session: Session
    total_jobs: int
    succeeded_jobs: int
    failed_jobs: int
    running_jobs: int
    waiting_jobs: int
    duration_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.as_dict(),
            "total_jobs": self.total_jobs,
            "succeeded_jobs": self.succeeded_jobs,
            "failed_jobs": self.failed_jobs,
            "running_jobs": self.running_jobs,
            "waiting_jobs": self.waiting_jobs,
            "duration_seconds": self.duration_seconds,
        }


class SessionManager:
    """Coordinates lifecycle of user-initiated sessions and tracks their jobs."""

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

    def start_session(
        self,
        actor: Actor,
        *,
        auto_stop_existing: bool = False,
    ) -> Session:
        """Starts a new working session. Only a human user may start a session."""
        if not isinstance(actor, Actor) or actor.kind is not ActorKind.USER:
            raise SessionNotAllowedError("Only a user can start a session.")

        auto_stopped: Session | None = None
        with self._database.transaction() as conn:
            repo = SessionRepository(conn)
            sessions = repo.list_all()
            active = next((s for s in reversed(sessions) if s.is_active), None)
            if active is not None:
                if auto_stop_existing:
                    auto_stopped = active.stop(clock=self._clock)
                    repo.update(auto_stopped, expected_status=SessionStatus.ACTIVE)
                else:
                    raise SessionConflictError(
                        f"An active session ({active.id}) is already in progress."
                    )

            session = Session.start(actor=actor, clock=self._clock)
            repo.add(session)

        if auto_stopped is not None:
            self._audit.record(
                action="session.stopped",
                actor=actor,
                entity=EntityRef("sessions", auto_stopped.id),
                result=AuditResult.SUCCESS,
                metadata={
                    "stopped_by": actor.id,
                    "reason": "Auto-stopped by start of new session",
                    "started_at": auto_stopped.started_at.isoformat(),
                    "stopped_at": (
                        auto_stopped.stopped_at.isoformat()
                        if auto_stopped.stopped_at
                        else None
                    ),
                },
            )

        self._audit.record(
            action="session.started",
            actor=actor,
            entity=EntityRef("sessions", session.id),
            result=AuditResult.SUCCESS,
            metadata={
                "started_by": actor.id,
                "started_at": session.started_at.isoformat(),
            },
        )
        return session

    def stop_session(
        self,
        session_id: str,
        actor: Actor,
        *,
        reason: str | None = None,
    ) -> Session:
        """Stops an active session."""
        with self._database.transaction() as conn:
            repo = SessionRepository(conn)
            session = repo.get(session_id)
            if session is None:
                raise SessionNotFoundError(f"Session {session_id} not found")

            if not session.is_active:
                return session

            stopped = session.stop(clock=self._clock)
            repo.update(stopped, expected_status=SessionStatus.ACTIVE)

        self._audit.record(
            action="session.stopped",
            actor=actor,
            entity=EntityRef("sessions", session.id),
            result=AuditResult.SUCCESS,
            metadata={
                "stopped_by": actor.id,
                "reason": reason,
                "started_at": session.started_at.isoformat(),
                "stopped_at": (
                    stopped.stopped_at.isoformat()
                    if stopped.stopped_at
                    else None
                ),
            },
        )
        return stopped

    def get_session(self, session_id: str) -> Session | None:
        """Retrieves a session by ID."""
        with self._database.connection() as conn:
            return SessionRepository(conn).get(session_id)

    def get_active_session(self) -> Session | None:
        """Retrieves the current active session if one exists."""
        with self._database.connection() as conn:
            sessions = SessionRepository(conn).list_all()
            for s in reversed(sessions):
                if s.is_active:
                    return s
            return None

    def list_sessions(self) -> list[Session]:
        """Lists all recorded sessions ordered by start time."""
        with self._database.connection() as conn:
            return SessionRepository(conn).list_all()

    def get_session_jobs(self, session_id: str) -> list[AIJob]:
        """Retrieves all AIJobs initiated within a given session."""
        with self._database.connection() as conn:
            return AIJobRepository(conn).list_by_session(session_id)

    def get_session_summary(self, session_id: str) -> SessionSummary:
        """Computes summary metrics for a given session."""
        with self._database.connection() as conn:
            session = SessionRepository(conn).get(session_id)
            if session is None:
                raise SessionNotFoundError(f"Session {session_id} not found")

            jobs = AIJobRepository(conn).list_by_session(session_id)

        succeeded = sum(1 for j in jobs if j.status is AIJobStatus.SUCCEEDED)
        failed = sum(1 for j in jobs if j.status is AIJobStatus.FAILED)
        running = sum(1 for j in jobs if j.status is AIJobStatus.RUNNING)
        waiting = sum(1 for j in jobs if j.status is AIJobStatus.WAITING)

        end_time = session.stopped_at or self._now()
        duration = max(0.0, (end_time - session.started_at).total_seconds())

        return SessionSummary(
            session=session,
            total_jobs=len(jobs),
            succeeded_jobs=succeeded,
            failed_jobs=failed,
            running_jobs=running,
            waiting_jobs=waiting,
            duration_seconds=duration,
        )
