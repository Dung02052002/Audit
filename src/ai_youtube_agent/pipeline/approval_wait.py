"""Approval Wait Service (Prompt Pack v8, task R-207).

Coordinates pausing running jobs when manual human review is required,
monitoring approval request status, and resuming or cancelling jobs
based on human reviewer decisions (enforcing the ActorKind.USER gate).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from ai_youtube_agent.content.approval import (
    ApprovalRequest,
    ApprovalStatus,
)
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.db.repositories.review import ApprovalRequestRepository
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import JobQueue

Clock = Callable[[], datetime]
WAIT_SERVICE_ACTOR = Actor(ActorKind.SYSTEM, "approval_wait_service")


class ApprovalWaitStatus(str, Enum):
    RESUMED = "resumed"
    STILL_WAITING = "still_waiting"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"


class ApprovalWaitError(RuntimeError):
    """Raised when an approval wait operation fails."""


@dataclass(frozen=True)
class ApprovalWaitResolution:
    job_id: str
    status: ApprovalWaitStatus
    approval_request_id: str | None = None
    details: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "approval_request_id": self.approval_request_id,
            "details": self.details,
        }


class ApprovalWaitService:
    """Coordinates lifecycle transitions between running jobs and approval requests."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        queue: JobQueue | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._queue = queue
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def pause_for_approval(self, job_id: str, approval_request_id: str) -> AIJob:
        """Transitions a RUNNING job to WAITING and links it to an approval request."""
        with self._database.transaction() as conn:
            job_repo = AIJobRepository(conn)
            job = job_repo.get(job_id)
            if job is None:
                raise ApprovalWaitError(f"Job {job_id} not found")

            if job.status is not AIJobStatus.RUNNING:
                raise ApprovalWaitError(
                    f"Job {job_id} must be in RUNNING status to pause for approval, but was {job.status.value}"
                )

            # Verify the approval request exists
            approval_repo = ApprovalRequestRepository(conn)
            approval = approval_repo.get(approval_request_id)
            if approval is None:
                raise ApprovalWaitError(
                    f"Approval request {approval_request_id} not found for job {job_id}"
                )

            old_updated_at = job.updated_at
            waiting_job = job.wait_for_approval(approval_request_id, clock=self._clock)
            job_repo.update(waiting_job, expected_updated_at=old_updated_at)

            self._audit.record(
                actor=WAIT_SERVICE_ACTOR,
                action="job.approval_wait",
                result=AuditResult.SUCCESS,
                entity=EntityRef("ai_jobs", job.id),
                metadata={
                    "approval_request_id": approval_request_id,
                    "content_item_id": job.content_item_id,
                    "pipeline_stage": job.pipeline_stage.value,
                },
            )
            return waiting_job

    def check_and_resolve(self, job_id: str) -> ApprovalWaitResolution:
        """Inspects the linked approval request for a WAITING job and resolves it."""
        with self._database.transaction() as conn:
            job_repo = AIJobRepository(conn)
            job = job_repo.get(job_id)
            if job is None:
                return ApprovalWaitResolution(
                    job_id=job_id,
                    status=ApprovalWaitStatus.NOT_FOUND,
                    details="Job not found",
                )

            if job.status is not AIJobStatus.WAITING:
                return ApprovalWaitResolution(
                    job_id=job.id,
                    status=ApprovalWaitStatus.NOT_FOUND,
                    details=f"Job is not in WAITING status (current: {job.status.value})",
                )

            # Find linked approval_request_id from checkpoints
            approval_request_id: str | None = None
            for cp in reversed(job.checkpoints):
                if cp.step == "approval.wait":
                    req_id = cp.data.get("approval_request_id")
                    if isinstance(req_id, str):
                        approval_request_id = req_id
                        break

            approval_repo = ApprovalRequestRepository(conn)
            approval: ApprovalRequest | None = None
            if approval_request_id:
                approval = approval_repo.get(approval_request_id)
            else:
                # Fallback: check active approval requests for this content item
                active_list = approval_repo.list_by_content_item(job.content_item_id)
                if active_list:
                    approval = active_list[-1]
                    approval_request_id = approval.id

            if approval is None:
                return ApprovalWaitResolution(
                    job_id=job.id,
                    status=ApprovalWaitStatus.NOT_FOUND,
                    approval_request_id=approval_request_id,
                    details="No associated approval request found",
                )

            old_updated_at = job.updated_at

            if approval.status == ApprovalStatus.APPROVED:
                resumed_job = job.resume_from_wait(clock=self._clock)
                job_repo.update(resumed_job, expected_updated_at=old_updated_at)

                if self._queue is not None:
                    self._queue.enqueue(
                        job_id=resumed_job.id,
                        content_item_id=resumed_job.content_item_id,
                        pipeline_stage=resumed_job.pipeline_stage.value,
                    )

                self._audit.record(
                    actor=WAIT_SERVICE_ACTOR,
                    action="job.approval_resumed",
                    result=AuditResult.SUCCESS,
                    entity=EntityRef("ai_jobs", job.id),
                    metadata={
                        "approval_request_id": approval.id,
                        "content_item_id": job.content_item_id,
                        "decided_by": (
                            approval.decided_by.as_dict()
                            if approval.decided_by
                            else None
                        ),
                    },
                )
                return ApprovalWaitResolution(
                    job_id=job.id,
                    status=ApprovalWaitStatus.RESUMED,
                    approval_request_id=approval.id,
                    details="Approved by reviewer; resumed to RUNNING",
                )

            elif approval.status == ApprovalStatus.REJECTED:
                reason = approval.decision_reason or "Reviewer rejected the request"
                cancelled_job = job.cancel(reason=reason, clock=self._clock)
                job_repo.update(cancelled_job, expected_updated_at=old_updated_at)

                self._audit.record(
                    actor=WAIT_SERVICE_ACTOR,
                    action="job.approval_rejected",
                    result=AuditResult.SUCCESS,
                    entity=EntityRef("ai_jobs", job.id),
                    metadata={
                        "approval_request_id": approval.id,
                        "content_item_id": job.content_item_id,
                        "reason": reason,
                    },
                )
                return ApprovalWaitResolution(
                    job_id=job.id,
                    status=ApprovalWaitStatus.REJECTED,
                    approval_request_id=approval.id,
                    details=reason,
                )

            elif approval.status == ApprovalStatus.CHANGES_REQUESTED:
                reason = approval.decision_reason or "Changes requested by reviewer"
                cancelled_job = job.cancel(reason=reason, clock=self._clock)
                job_repo.update(cancelled_job, expected_updated_at=old_updated_at)

                self._audit.record(
                    actor=WAIT_SERVICE_ACTOR,
                    action="job.approval_changes_requested",
                    result=AuditResult.SUCCESS,
                    entity=EntityRef("ai_jobs", job.id),
                    metadata={
                        "approval_request_id": approval.id,
                        "content_item_id": job.content_item_id,
                        "reason": reason,
                    },
                )
                return ApprovalWaitResolution(
                    job_id=job.id,
                    status=ApprovalWaitStatus.CHANGES_REQUESTED,
                    approval_request_id=approval.id,
                    details=reason,
                )

            elif approval.status == ApprovalStatus.EXPIRED:
                return ApprovalWaitResolution(
                    job_id=job.id,
                    status=ApprovalWaitStatus.EXPIRED,
                    approval_request_id=approval.id,
                    details="Approval request has expired",
                )

            else:
                # Still PENDING
                return ApprovalWaitResolution(
                    job_id=job.id,
                    status=ApprovalWaitStatus.STILL_WAITING,
                    approval_request_id=approval.id,
                    details="Approval request is pending human review",
                )

    def poll_waiting_jobs(self) -> list[ApprovalWaitResolution]:
        """Scans all jobs in WAITING status and resolves them against their approval requests."""
        resolutions: list[ApprovalWaitResolution] = []
        with self._database.transaction() as conn:
            job_repo = AIJobRepository(conn)
            all_jobs = job_repo.list_all()
            waiting_ids = [j.id for j in all_jobs if j.status is AIJobStatus.WAITING]

        for jid in waiting_ids:
            res = self.check_and_resolve(jid)
            resolutions.append(res)
        return resolutions

    def get_waiting_jobs(self) -> list[AIJob]:
        """Returns all jobs currently in WAITING status."""
        with self._database.transaction() as conn:
            job_repo = AIJobRepository(conn)
            return [j for j in job_repo.list_all() if j.status is AIJobStatus.WAITING]
