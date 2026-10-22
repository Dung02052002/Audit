"""Orchestrator Audit (Prompt Pack v8, prompt #214), context C16 Orchestration.

Audits and queries every job start, completion, failure, retry, approval wait,
session lifecycle transition, crash recovery, graceful shutdown, kill switch
toggle, and gate/limit block across the orchestrator runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ai_youtube_agent.core.audit import (
    Actor,
    AuditEvent,
    AuditLog,
    AuditResult,
    AuditSink,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import (
    AIJobRepository,
    SessionRepository,
)
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJob

JOB_STARTED: Final[str] = "job.started"
JOB_COMPLETED: Final[str] = "job.completed"
JOB_FAILED: Final[str] = "job.failed"
JOB_BLOCKED: Final[str] = "job.blocked"

_JOB_ENTITY_TYPES: Final[frozenset[str]] = frozenset({"ai_job", "ai_jobs"})
_SESSION_ENTITY_TYPES: Final[frozenset[str]] = frozenset({"session", "sessions"})
_ITEM_ENTITY_TYPES: Final[frozenset[str]] = frozenset({"content_item", "content_items"})

ORCHESTRATOR_AUDIT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        JOB_STARTED,
        JOB_COMPLETED,
        JOB_FAILED,
        JOB_BLOCKED,
        "job.scheduled",
        "job.schedule_blocked",
        "scheduler.job_scheduled",
        "scheduler.skipped",
        "pipeline.step_advanced",
        "pipeline.step_passed",
        "pipeline.step_blocked",
        "pipeline.approval_requested",
        "job.approval_wait",
        "job.approval_resumed",
        "job.approval_rejected",
        "job.approval_changes_requested",
        "job.waiting_for_approval",
        "job.resumed_from_approval",
        "job.cancelled_on_approval_rejection",
        "job.retry_scheduled",
        "job.retry_exhausted",
        "job.retry_rejected",
        "job.retry_started",
        "job.retry_restarted",
        "session.started",
        "session.stopped",
        "session.start_denied",
        "session.stop_denied",
        "orchestrator.daily_stop_triggered",
        "daily_stop.production_blocked",
        "orchestrator.crash_recovery",
        "orchestrator.graceful_shutdown",
        "killswitch.activated",
        "killswitch.deactivated",
        "killswitch.activate_denied",
        "killswitch.deactivate_denied",
        "killswitch.blocked",
    }
)


class OrchestratorAuditError(DomainError):
    """Raised when querying orchestrator audit trails fails."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.orchestrator_audit_error",
        user_message: str = "Orchestrator audit trail could not be retrieved.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


@dataclass(frozen=True)
class OrchestratorAuditEntry:
    """One immutable event in an orchestrator audit trail."""

    event_id: str
    action: str
    actor: Actor
    result: AuditResult
    entity_type: str
    entity_id: str
    job_id: str | None
    session_id: str | None
    content_item_id: str | None
    occurred_at: datetime
    metadata: Mapping[str, MetadataValue]

    @classmethod
    def from_event(
        cls,
        event: AuditEvent,
        *,
        fallback_job_id: str | None = None,
        fallback_session_id: str | None = None,
        fallback_content_item_id: str | None = None,
    ) -> OrchestratorAuditEntry:
        meta = event.metadata
        job_id = (
            _str_or_none(meta.get("job_id"))
            or event.job_id
            or (
                event.entity.id
                if event.entity.type in _JOB_ENTITY_TYPES
                else fallback_job_id
            )
        )
        session_id = (
            _str_or_none(meta.get("session_id"))
            or event.session_id
            or (
                event.entity.id
                if event.entity.type in _SESSION_ENTITY_TYPES
                else fallback_session_id
            )
        )
        content_item_id = _str_or_none(meta.get("content_item_id")) or (
            event.entity.id
            if event.entity.type in _ITEM_ENTITY_TYPES
            else fallback_content_item_id
        )
        return cls(
            event_id=event.event_id,
            action=event.action,
            actor=event.actor,
            result=event.result,
            entity_type=event.entity.type,
            entity_id=event.entity.id,
            job_id=job_id,
            session_id=session_id,
            content_item_id=content_item_id,
            occurred_at=event.timestamp,
            metadata=meta,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "actor": {"kind": self.actor.kind.value, "id": self.actor.id},
            "result": self.result.value,
            "entity": {"type": self.entity_type, "id": self.entity_id},
            "job_id": self.job_id,
            "session_id": self.session_id,
            "content_item_id": self.content_item_id,
            "occurred_at": self.occurred_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class OrchestratorAuditTrail:
    """Aggregated orchestrator audit trail."""

    scope: str
    subject_id: str
    entries: tuple[OrchestratorAuditEntry, ...]
    action_counts: Mapping[str, int]
    result_counts: Mapping[str, int]

    @property
    def total_events(self) -> int:
        return len(self.entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "subject_id": self.subject_id,
            "total_events": len(self.entries),
            "action_counts": dict(self.action_counts),
            "result_counts": dict(self.result_counts),
            "entries": [entry.as_dict() for entry in self.entries],
        }


class OrchestratorAuditRecorder:
    """Records standardized orchestrator job lifecycle events via ``AuditLog``."""

    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit

    def record_job_started(
        self,
        job: AIJob,
        *,
        actor: Actor,
        worker_id: str | None = None,
    ) -> AuditEvent:
        return self._audit.record(
            action=JOB_STARTED,
            actor=actor,
            entity=EntityRef("ai_job", job.id),
            result=AuditResult.SUCCESS,
            metadata={
                "job_id": job.id,
                "kind": job.kind,
                "attempts": job.attempts,
                "content_item_id": job.content_item_id,
                "session_id": job.session_id,
                "worker_id": worker_id,
            },
        )

    def record_job_completed(
        self,
        job: AIJob,
        *,
        actor: Actor,
        step_count: int | None = None,
    ) -> AuditEvent:
        return self._audit.record(
            action=JOB_COMPLETED,
            actor=actor,
            entity=EntityRef("ai_job", job.id),
            result=AuditResult.SUCCESS,
            metadata={
                "job_id": job.id,
                "kind": job.kind,
                "attempts": job.attempts,
                "content_item_id": job.content_item_id,
                "session_id": job.session_id,
                "step_count": (
                    step_count if step_count is not None else len(job.checkpoints)
                ),
            },
        )

    def record_job_failed(
        self,
        job: AIJob,
        *,
        actor: Actor,
        error_code: str,
        retryable: bool = False,
    ) -> AuditEvent:
        return self._audit.record(
            action=JOB_FAILED,
            actor=actor,
            entity=EntityRef("ai_job", job.id),
            result=AuditResult.FAILURE,
            metadata={
                "job_id": job.id,
                "kind": job.kind,
                "attempts": job.attempts,
                "content_item_id": job.content_item_id,
                "session_id": job.session_id,
                "error_code": error_code,
                "retryable": retryable,
            },
        )

    def record_job_blocked(
        self,
        job: AIJob,
        *,
        actor: Actor,
        reason_code: str,
        gate_name: str | None = None,
    ) -> AuditEvent:
        return self._audit.record(
            action=JOB_BLOCKED,
            actor=actor,
            entity=EntityRef("ai_job", job.id),
            result=AuditResult.DENIED,
            metadata={
                "job_id": job.id,
                "kind": job.kind,
                "content_item_id": job.content_item_id,
                "session_id": job.session_id,
                "reason_code": reason_code,
                "gate_name": gate_name,
            },
        )


class OrchestratorAuditService:
    """Queries orchestrator audit trails across jobs, sessions, items, and runtime."""

    def __init__(self, database: Database, audit_sink: AuditSink) -> None:
        self._database = database
        self._sink = audit_sink

    def trail_for_job(self, job_id: str) -> OrchestratorAuditTrail:
        """Return all orchestrator audit events associated with ``job_id``."""
        with self._database.connection() as conn:
            job = AIJobRepository(conn).get(job_id)
        if job is None:
            raise OrchestratorAuditError(
                f"AIJob '{job_id}' not found.",
                code="domain.orchestrator_audit.job_not_found",
                user_message="The requested AI job was not found.",
            )

        entries: list[OrchestratorAuditEntry] = []
        for event in self._sink.events():
            if event.action not in ORCHESTRATOR_AUDIT_ACTIONS:
                continue
            if self._matches_job(event, job):
                entries.append(
                    OrchestratorAuditEntry.from_event(
                        event,
                        fallback_job_id=job.id,
                        fallback_session_id=job.session_id,
                        fallback_content_item_id=job.content_item_id,
                    )
                )
        return _build_trail("ai_job", job.id, entries)

    def trail_for_session(self, session_id: str) -> OrchestratorAuditTrail:
        """Return all orchestrator audit events for a user session and its jobs."""
        with self._database.connection() as conn:
            session = SessionRepository(conn).get(session_id)
            if session is None:
                raise OrchestratorAuditError(
                    f"Session '{session_id}' not found.",
                    code="domain.orchestrator_audit.session_not_found",
                    user_message="The requested session was not found.",
                )
            jobs = AIJobRepository(conn).list_by_session(session_id)
            job_by_id = {j.id: j for j in jobs}

        entries: list[OrchestratorAuditEntry] = []
        for event in self._sink.events():
            if event.action not in ORCHESTRATOR_AUDIT_ACTIONS:
                continue
            matched_job = self._match_session_job(event, job_by_id)
            is_session_event = (
                (
                    event.entity.type in _SESSION_ENTITY_TYPES
                    and event.entity.id == session_id
                )
                or event.metadata.get("session_id") == session_id
                or event.session_id == session_id
            )
            if matched_job is None and not is_session_event:
                continue
            entries.append(
                OrchestratorAuditEntry.from_event(
                    event,
                    fallback_job_id=matched_job.id if matched_job else None,
                    fallback_session_id=session_id,
                    fallback_content_item_id=(
                        matched_job.content_item_id if matched_job else None
                    ),
                )
            )
        return _build_trail("session", session_id, entries)

    def trail_for_content_item(self, content_item_id: str) -> OrchestratorAuditTrail:
        """Return all orchestrator audit events for a content item."""
        with self._database.connection() as conn:
            jobs = AIJobRepository(conn).list_by_content_item(content_item_id)
            job_ids = {j.id for j in jobs}

        entries: list[OrchestratorAuditEntry] = []
        for event in self._sink.events():
            if event.action not in ORCHESTRATOR_AUDIT_ACTIONS:
                continue
            if (
                (
                    event.entity.type in _ITEM_ENTITY_TYPES
                    and event.entity.id == content_item_id
                )
                or event.metadata.get("content_item_id") == content_item_id
                or (
                    event.entity.type in _JOB_ENTITY_TYPES
                    and event.entity.id in job_ids
                )
                or (
                    isinstance(event.metadata.get("job_id"), str)
                    and event.metadata.get("job_id") in job_ids
                )
            ):
                entries.append(
                    OrchestratorAuditEntry.from_event(
                        event,
                        fallback_content_item_id=content_item_id,
                    )
                )
        return _build_trail("content_item", content_item_id, entries)

    def trail_for_runtime(self) -> OrchestratorAuditTrail:
        """Return all orchestrator audit events across the runtime."""
        entries = [
            OrchestratorAuditEntry.from_event(event)
            for event in self._sink.events()
            if event.action in ORCHESTRATOR_AUDIT_ACTIONS
        ]
        return _build_trail("runtime", "global", entries)

    @staticmethod
    def _matches_job(event: AuditEvent, job: AIJob) -> bool:
        if event.entity.type in _JOB_ENTITY_TYPES and event.entity.id == job.id:
            return True
        if event.metadata.get("job_id") == job.id:
            return True
        return event.job_id == job.id

    @staticmethod
    def _match_session_job(
        event: AuditEvent,
        job_by_id: Mapping[str, AIJob],
    ) -> AIJob | None:
        if event.entity.type in _JOB_ENTITY_TYPES and event.entity.id in job_by_id:
            return job_by_id[event.entity.id]
        meta_jid = event.metadata.get("job_id")
        if isinstance(meta_jid, str) and meta_jid in job_by_id:
            return job_by_id[meta_jid]
        if event.job_id and event.job_id in job_by_id:
            return job_by_id[event.job_id]
        return None


def _build_trail(
    scope: str,
    subject_id: str,
    entries: list[OrchestratorAuditEntry],
) -> OrchestratorAuditTrail:
    action_counts: dict[str, int] = {}
    result_counts: dict[str, int] = {}
    for entry in entries:
        action_counts[entry.action] = action_counts.get(entry.action, 0) + 1
        result_counts[entry.result.value] = result_counts.get(entry.result.value, 0) + 1
    return OrchestratorAuditTrail(
        scope=scope,
        subject_id=subject_id,
        entries=tuple(entries),
        action_counts=action_counts,
        result_counts=result_counts,
    )


def _str_or_none(value: MetadataValue | None) -> str | None:
    return value if isinstance(value, str) and value else None
