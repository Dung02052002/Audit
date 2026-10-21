"""Crash Recovery & Checkpoint Resume Service (Prompt Pack v8, task R-211).

Resumes interrupted AI jobs from their last safe domain checkpoint after a worker
crash or process restart, preventing duplicate artifact generation or uploads.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
    MetadataValue,
)
from ai_youtube_agent.core.db.codec import parse_datetime
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus, JobCheckpoint
from ai_youtube_agent.pipeline.job_queue import LEASE_STEP_NAME, JobQueue

Clock = Callable[[], datetime]
CRASH_RESUME_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "crash_resume_service")

ADMINISTRATIVE_CHECKPOINT_STEPS: Final[frozenset[str]] = frozenset({
    LEASE_STEP_NAME,
    "approval.wait",
    "approval.resumed",
    "retry.scheduled",
    "retry.terminal",
    "crash.recovered",
    "job.cancelled",
    "cancelled",
})


class CrashResumeError(DomainError):
    """Raised when a job cannot be safely resumed from checkpoint."""

    default_code = "domain.crash_resume_error"
    default_user_message = "Could not resume the interrupted job from its checkpoint."


@dataclass(frozen=True)
class SafeCheckpointState:
    """Represents the last verified domain checkpoint of an AIJob."""

    job_id: str
    kind: str
    content_item_id: str | None
    session_id: str | None
    last_safe_step: str | None
    last_safe_sequence: int
    checkpoint_data: MappingProxyType[str, MetadataValue]
    completed_domain_steps: tuple[str, ...]
    attempts: int

    @property
    def has_safe_checkpoint(self) -> bool:
        return self.last_safe_step is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "content_item_id": self.content_item_id,
            "session_id": self.session_id,
            "last_safe_step": self.last_safe_step,
            "last_safe_sequence": self.last_safe_sequence,
            "checkpoint_data": dict(self.checkpoint_data),
            "completed_domain_steps": list(self.completed_domain_steps),
            "attempts": self.attempts,
            "has_safe_checkpoint": self.has_safe_checkpoint,
        }


@dataclass(frozen=True)
class RecoveredJobPlan:
    """Outcome of recovering and resuming an interrupted job."""

    job: AIJob
    checkpoint_state: SafeCheckpointState
    resumed: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job.id,
            "status": self.job.status.value,
            "resumed": self.resumed,
            "reason": self.reason,
            "checkpoint_state": self.checkpoint_state.as_dict(),
        }


@dataclass(frozen=True)
class CrashRecoveryReport:
    """Summary of startup or periodic crash recovery."""

    scanned_running_jobs: int
    recovered_jobs: tuple[RecoveredJobPlan, ...]
    skipped_active_leases: int
    exhausted_jobs: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned_running_jobs": self.scanned_running_jobs,
            "recovered_count": len(self.recovered_jobs),
            "skipped_active_leases": self.skipped_active_leases,
            "exhausted_jobs": self.exhausted_jobs,
            "recovered_jobs": [p.as_dict() for p in self.recovered_jobs],
        }


def extract_safe_checkpoint(job: AIJob) -> SafeCheckpointState:
    """Inspects a job's checkpoint history and returns the last safe domain checkpoint."""
    domain_checkpoints: list[JobCheckpoint] = [
        cp
        for cp in job.checkpoints
        if cp.step not in ADMINISTRATIVE_CHECKPOINT_STEPS
    ]
    last_cp = domain_checkpoints[-1] if domain_checkpoints else None
    return SafeCheckpointState(
        job_id=job.id,
        kind=job.kind,
        content_item_id=job.content_item_id,
        session_id=job.session_id,
        last_safe_step=last_cp.step if last_cp else None,
        last_safe_sequence=last_cp.sequence if last_cp else 0,
        checkpoint_data=MappingProxyType(dict(last_cp.data) if last_cp else {}),
        completed_domain_steps=tuple(cp.step for cp in domain_checkpoints),
        attempts=job.attempts,
    )


class CrashResumeService:
    """Recovers interrupted AI jobs after a crash and resumes from their last safe checkpoint."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        queue: JobQueue | None = None,
        max_attempts: int = 3,
        clock: Clock | None = None,
    ) -> None:
        if isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be an integer >= 1")
        self._database = database
        self._audit = audit_log
        self._queue = queue
        self._max_attempts = max_attempts
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def inspect_checkpoint_state(self, job_id: str) -> SafeCheckpointState:
        """Returns the last safe domain checkpoint state for a persisted job."""
        with self._database.connection() as conn:
            job = AIJobRepository(conn).get(job_id)
        if job is None:
            raise CrashResumeError(f"Job {job_id} not found")
        return extract_safe_checkpoint(job)

    def resume_job(
        self,
        job_id: str,
        *,
        force_reclaim: bool = False,
        actor: Actor | None = None,
    ) -> RecoveredJobPlan:
        """Recovers a single interrupted RUNNING or FAILED job from its last safe checkpoint."""
        now = self._now()
        audit_actor = actor or CRASH_RESUME_ACTOR
        resumed_flag = False
        plan: RecoveredJobPlan

        with self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            job = repo.get(job_id)
            if job is None:
                raise CrashResumeError(f"Job {job_id} not found")

            if job.is_final:
                raise CrashResumeError(
                    f"Job {job.id} is in terminal status {job.status.value} and cannot be resumed"
                )
            if job.status is AIJobStatus.WAITING:
                raise CrashResumeError(
                    f"Job {job.id} is WAITING for human approval and cannot be crash-resumed"
                )

            if job.status is AIJobStatus.RUNNING and not force_reclaim:
                if self._has_live_lease(job, now):
                    raise CrashResumeError(
                        f"Job {job.id} has an active worker lease; pass force_reclaim=True to take over"
                    )

            state = extract_safe_checkpoint(job)
            old_updated_at = job.updated_at

            if job.status is AIJobStatus.RUNNING:
                job = job.fail("Interrupted by crash or expired lease", clock=self._clock)

            if job.attempts >= self._max_attempts:
                exhausted = job.record_checkpoint(
                    "crash.recovered",
                    {
                        "resumed": False,
                        "resume_from_step": state.last_safe_step or "start",
                        "resume_from_sequence": state.last_safe_sequence,
                        "reason": "Max attempts reached",
                    },
                    clock=self._clock,
                )
                repo.update(exhausted, expected_updated_at=old_updated_at)
                plan = RecoveredJobPlan(
                    job=exhausted,
                    checkpoint_state=state,
                    resumed=False,
                    reason=f"Max attempts reached ({job.attempts}/{self._max_attempts})",
                )
            else:
                if job.status in (AIJobStatus.QUEUED, AIJobStatus.FAILED):
                    restarted = job.start(clock=self._clock)
                else:
                    restarted = job
                recovered = restarted.checkpoint(
                    "crash.recovered",
                    {
                        "resumed": True,
                        "resume_from_step": state.last_safe_step or "start",
                        "resume_from_sequence": state.last_safe_sequence,
                    },
                    clock=self._clock,
                )
                repo.update(recovered, expected_updated_at=old_updated_at)
                resumed_flag = True
                plan = RecoveredJobPlan(
                    job=recovered,
                    checkpoint_state=extract_safe_checkpoint(recovered),
                    resumed=True,
                    reason=(
                        f"Resumed from checkpoint '{state.last_safe_step}' (seq {state.last_safe_sequence})"
                        if state.last_safe_step
                        else "Resumed from beginning (no prior domain checkpoint)"
                    ),
                )

        if resumed_flag and self._queue is not None:
            self._queue.enqueue(plan.job)

        self._audit.record(
            action="orchestrator.crash_resumed",
            actor=audit_actor,
            entity=EntityRef("ai_jobs", plan.job.id),
            result=AuditResult.SUCCESS if plan.resumed else AuditResult.FAILURE,
            metadata={
                "kind": plan.job.kind,
                "content_item_id": plan.job.content_item_id,
                "resumed": plan.resumed,
                "resume_from_step": plan.checkpoint_state.last_safe_step or "start",
                "resume_from_sequence": plan.checkpoint_state.last_safe_sequence,
                "attempts": plan.job.attempts,
            },
        )
        return plan

    def recover_interrupted_jobs(
        self,
        *,
        force_reclaim: bool = False,
        actor: Actor | None = None,
    ) -> CrashRecoveryReport:
        """Scans all RUNNING jobs and recovers those that crashed or lost their lease."""
        now = self._now()
        audit_actor = actor or CRASH_RESUME_ACTOR

        with self._database.connection() as conn:
            all_jobs = AIJobRepository(conn).list_all()

        running_jobs = [j for j in all_jobs if j.status is AIJobStatus.RUNNING]
        recovered_plans: list[RecoveredJobPlan] = []
        skipped_active = 0
        exhausted = 0

        for j in running_jobs:
            if not force_reclaim and self._has_live_lease(j, now):
                skipped_active += 1
                continue
            plan = self.resume_job(j.id, force_reclaim=True, actor=audit_actor)
            recovered_plans.append(plan)
            if not plan.resumed:
                exhausted += 1

        report = CrashRecoveryReport(
            scanned_running_jobs=len(running_jobs),
            recovered_jobs=tuple(recovered_plans),
            skipped_active_leases=skipped_active,
            exhausted_jobs=exhausted,
        )

        self._audit.record(
            action="orchestrator.crash_recovery_completed",
            actor=audit_actor,
            entity=EntityRef("orchestrator", "crash_resume"),
            result=AuditResult.SUCCESS,
            metadata={
                "scanned_running_jobs": report.scanned_running_jobs,
                "recovered_count": len(report.recovered_jobs),
                "skipped_active_leases": report.skipped_active_leases,
                "exhausted_jobs": report.exhausted_jobs,
            },
        )
        return report

    @staticmethod
    def _has_live_lease(job: AIJob, now: datetime) -> bool:
        for cp in reversed(job.checkpoints):
            if cp.step == LEASE_STEP_NAME:
                exp_str = cp.data.get("expires_at")
                if isinstance(exp_str, str) and exp_str:
                    return now < parse_datetime(exp_str)
                break
        return False
