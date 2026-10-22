"""Durable job queue abstraction (Prompt Pack v8, prompt R-204), context C16
Orchestration.

Provides the durable, idempotent job queue abstraction for scheduling and executing
pipeline AI work units:

- ``JobQueue``: synchronous Protocol defining durable queue operations:
  ``enqueue``, ``enqueue_new``, ``dequeue``, ``ack``, ``nack``, ``renew_lease``,
  ``reclaim_expired``, ``peek``, and ``size``.
- ``QueuedJobLease``: frozen lease holding an exclusive worker assignment, lease token,
  timestamps, and expiry check.
- ``QueueEnqueueResult``: result indicating whether a job was newly enqueued or
  safely deduplicated/reused via idempotency key.
- ``JobQueueError`` and ``JobQueueErrorCode``: typed errors for lease expiration,
  invalid state, and concurrency conflicts.
- ``SqliteJobQueue``: durable, ACID-compliant SQLite implementation leveraging
  ``Database`` and ``AIJobRepository`` with lease tracking and crash recovery.
- ``InMemoryJobQueue``: thread-safe in-memory implementation for isolated tests.
"""

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from ai_youtube_agent.core.db.codec import format_datetime, parse_datetime
from ai_youtube_agent.core.db.database import ConcurrencyError, Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus

Clock = Callable[[], datetime]

DEFAULT_LEASE_DURATION: Final[timedelta] = timedelta(minutes=5)
LEASE_STEP_NAME: Final[str] = "queue.leased"


class JobQueueErrorCode(StrEnum):
    QUEUE_EMPTY = "domain.job_queue_empty"
    JOB_NOT_FOUND = "domain.job_not_found"
    LEASE_EXPIRED = "domain.job_lease_expired"
    LEASE_INVALID = "domain.job_lease_invalid"
    CONCURRENCY_CONFLICT = "domain.job_queue_conflict"
    INVALID_STATE = "domain.job_queue_invalid_state"


class JobQueueError(DomainError):
    def __init__(self, code: JobQueueErrorCode, detail: str) -> None:
        super().__init__(detail, code=code.value)
        self.queue_code = code


@dataclass(frozen=True)
class QueuedJobLease:
    job: AIJob
    worker_id: str
    lease_token: str
    leased_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.as_dict(),
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "leased_at": self.leased_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class QueueEnqueueResult:
    job: AIJob
    is_new: bool
    reused: bool
    status: AIJobStatus

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job.id,
            "is_new": self.is_new,
            "reused": self.reused,
            "status": self.status.value,
        }


@runtime_checkable
class JobQueue(Protocol):
    """Durable queue protocol for pipeline AI jobs."""

    def enqueue(self, job: AIJob) -> QueueEnqueueResult: ...

    def enqueue_new(
        self,
        kind: str,
        idempotency_key: str,
        *,
        content_item_id: str | None = None,
        session_id: str | None = None,
    ) -> QueueEnqueueResult: ...

    def dequeue(
        self,
        worker_id: str,
        *,
        kinds: Sequence[str] | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> QueuedJobLease | None: ...

    def ack(self, lease: QueuedJobLease) -> AIJob: ...

    def nack(
        self,
        lease: QueuedJobLease,
        error: str,
        *,
        retryable: bool = False,
    ) -> AIJob: ...

    def renew_lease(
        self,
        lease: QueuedJobLease,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> QueuedJobLease: ...

    def reclaim_expired(
        self,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> int: ...

    def peek(self, limit: int = 50) -> list[AIJob]: ...

    def size(self) -> int: ...


class SqliteJobQueue:
    """Durable SQLite-backed implementation of JobQueue."""

    def __init__(self, database: Database, *, clock: Clock | None = None) -> None:
        self._database = database
        self._clock = clock
        self._lock = threading.Lock()

    def enqueue(self, job: AIJob) -> QueueEnqueueResult:
        with self._lock, self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            existing = repo.get_by_idempotency_key(job.idempotency_key)
            if existing is not None:
                return QueueEnqueueResult(
                    job=existing,
                    is_new=False,
                    reused=True,
                    status=existing.status,
                )
            repo.add(job)
            return QueueEnqueueResult(
                job=job,
                is_new=True,
                reused=False,
                status=job.status,
            )

    def enqueue_new(
        self,
        kind: str,
        idempotency_key: str,
        *,
        content_item_id: str | None = None,
        session_id: str | None = None,
    ) -> QueueEnqueueResult:
        job = AIJob.create(
            kind=kind,
            idempotency_key=idempotency_key,
            content_item_id=content_item_id,
            session_id=session_id,
            clock=self._clock,
        )
        return self.enqueue(job)

    def dequeue(
        self,
        worker_id: str,
        *,
        kinds: Sequence[str] | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> QueuedJobLease | None:
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id must not be empty")

        now = self._now()
        kinds_filter = set(kinds) if kinds else None

        with self._lock, self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            all_jobs = repo.list_all()

            # First, check for queued jobs or running jobs with expired leases
            candidates: list[AIJob] = []
            for j in all_jobs:
                if kinds_filter and j.kind not in kinds_filter:
                    continue
                if j.status is AIJobStatus.QUEUED:
                    candidates.append(j)
                elif j.status is AIJobStatus.RUNNING:
                    # Check lease expiration on running jobs
                    last_cp = j.last_checkpoint
                    if last_cp and last_cp.step == LEASE_STEP_NAME:
                        exp_str = last_cp.data.get("expires_at")
                        if exp_str and isinstance(exp_str, str):
                            exp_dt = parse_datetime(exp_str)
                            if now >= exp_dt:
                                candidates.append(j)

            # Sort by created_at, then id for strict FIFO order
            candidates.sort(key=lambda j: (j.created_at, j.id))

            for candidate in candidates:
                old_updated_at = candidate.updated_at
                lease_token = uuid.uuid4().hex
                leased_at = now
                expires_at = now + lease_duration

                try:
                    # If job was running (expired lease), transition to failed then
                    # start, or start directly if queued
                    if candidate.status is AIJobStatus.RUNNING:
                        # Mark previous attempt failed due to lease timeout, then
                        # restart
                        step_job = candidate.fail(
                            "Worker lease expired, reclaiming job", clock=self._clock
                        )
                        step_job = step_job.start(clock=self._clock)
                    else:
                        step_job = candidate.start(clock=self._clock)

                    leased_job = step_job.checkpoint(
                        LEASE_STEP_NAME,
                        {
                            "worker_id": worker_id.strip(),
                            "token": lease_token,
                            "expires_at": format_datetime(expires_at),
                        },
                        clock=self._clock,
                    )
                    repo.update(leased_job, expected_updated_at=old_updated_at)
                    return QueuedJobLease(
                        job=leased_job,
                        worker_id=worker_id.strip(),
                        lease_token=lease_token,
                        leased_at=leased_at,
                        expires_at=expires_at,
                    )
                except ConcurrencyError:
                    # Another worker claimed or updated this job concurrently; try next
                    # candidate
                    continue

        return None

    def ack(self, lease: QueuedJobLease) -> AIJob:
        with self._lock, self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            current = repo.get(lease.job.id)
            if current is None:
                raise JobQueueError(
                    JobQueueErrorCode.JOB_NOT_FOUND,
                    f"Job {lease.job.id} does not exist in the queue.",
                )
            self._verify_lease_token(current, lease)
            if current.status is not AIJobStatus.RUNNING:
                raise JobQueueError(
                    JobQueueErrorCode.INVALID_STATE,
                    f"Cannot ack job {current.id} in status {current.status.value}.",
                )
            old_updated_at = current.updated_at
            succeeded_job = current.succeed(clock=self._clock)
            try:
                repo.update(succeeded_job, expected_updated_at=old_updated_at)
            except ConcurrencyError as exc:
                raise JobQueueError(
                    JobQueueErrorCode.CONCURRENCY_CONFLICT,
                    f"Concurrency conflict acking job {current.id}.",
                ) from exc
            return succeeded_job

    def nack(
        self,
        lease: QueuedJobLease,
        error: str,
        *,
        retryable: bool = False,
    ) -> AIJob:
        with self._lock, self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            current = repo.get(lease.job.id)
            if current is None:
                raise JobQueueError(
                    JobQueueErrorCode.JOB_NOT_FOUND,
                    f"Job {lease.job.id} does not exist in the queue.",
                )
            self._verify_lease_token(current, lease)
            if current.status is not AIJobStatus.RUNNING:
                raise JobQueueError(
                    JobQueueErrorCode.INVALID_STATE,
                    f"Cannot nack job {current.id} in status {current.status.value}.",
                )
            old_updated_at = current.updated_at
            failed_job = current.fail(error, clock=self._clock)
            try:
                repo.update(failed_job, expected_updated_at=old_updated_at)
            except ConcurrencyError as exc:
                raise JobQueueError(
                    JobQueueErrorCode.CONCURRENCY_CONFLICT,
                    f"Concurrency conflict nacking job {current.id}.",
                ) from exc
            return failed_job

    def renew_lease(
        self,
        lease: QueuedJobLease,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> QueuedJobLease:
        now = self._now()
        with self._lock, self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            current = repo.get(lease.job.id)
            if current is None:
                raise JobQueueError(
                    JobQueueErrorCode.JOB_NOT_FOUND,
                    f"Job {lease.job.id} does not exist in the queue.",
                )
            self._verify_lease_token(current, lease)
            if current.status is not AIJobStatus.RUNNING:
                raise JobQueueError(
                    JobQueueErrorCode.INVALID_STATE,
                    f"Cannot renew lease for job {current.id} in status "
                    f"{current.status.value}.",
                )
            old_updated_at = current.updated_at
            expires_at = now + lease_duration
            renewed_job = current.checkpoint(
                LEASE_STEP_NAME,
                {
                    "worker_id": lease.worker_id,
                    "token": lease.lease_token,
                    "expires_at": format_datetime(expires_at),
                },
                clock=self._clock,
            )
            try:
                repo.update(renewed_job, expected_updated_at=old_updated_at)
            except ConcurrencyError as exc:
                raise JobQueueError(
                    JobQueueErrorCode.CONCURRENCY_CONFLICT,
                    f"Concurrency conflict renewing lease for job {current.id}.",
                ) from exc

            return QueuedJobLease(
                job=renewed_job,
                worker_id=lease.worker_id,
                lease_token=lease.lease_token,
                leased_at=lease.leased_at,
                expires_at=expires_at,
            )

    def reclaim_expired(
        self,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> int:
        now = self._now()
        reclaimed = 0
        with self._lock, self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            all_jobs = repo.list_all()
            for j in all_jobs:
                if j.status is not AIJobStatus.RUNNING:
                    continue
                last_cp = j.last_checkpoint
                if last_cp and last_cp.step == LEASE_STEP_NAME:
                    exp_str = last_cp.data.get("expires_at")
                    if exp_str and isinstance(exp_str, str):
                        exp_dt = parse_datetime(exp_str)
                        if now >= exp_dt:
                            old_updated_at = j.updated_at
                            try:
                                failed = j.fail(
                                    "Reclaimed expired worker lease", clock=self._clock
                                )
                                repo.update(failed, expected_updated_at=old_updated_at)
                                reclaimed += 1
                            except ConcurrencyError:
                                pass
        return reclaimed

    def peek(self, limit: int = 50) -> list[AIJob]:
        with self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            queued = [j for j in repo.list_all() if j.status is AIJobStatus.QUEUED]
            queued.sort(key=lambda j: (j.created_at, j.id))
            return queued[:limit]

    def size(self) -> int:
        with self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            return sum(1 for j in repo.list_all() if j.status is AIJobStatus.QUEUED)

    def _verify_lease_token(self, job: AIJob, lease: QueuedJobLease) -> None:
        last_cp = job.last_checkpoint
        if not last_cp or last_cp.step != LEASE_STEP_NAME:
            raise JobQueueError(
                JobQueueErrorCode.LEASE_INVALID,
                f"Job {job.id} has no active lease checkpoint.",
            )
        token = last_cp.data.get("token")
        if token != lease.lease_token:
            raise JobQueueError(
                JobQueueErrorCode.LEASE_INVALID,
                f"Lease token mismatch on job {job.id}.",
            )

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


class InMemoryJobQueue:
    """Thread-safe in-memory implementation of JobQueue for fast isolated tests."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, AIJob] = {}
        self._idempotency_map: dict[str, str] = {}
        self._leases: dict[str, QueuedJobLease] = {}

    def enqueue(self, job: AIJob) -> QueueEnqueueResult:
        with self._lock:
            existing_id = self._idempotency_map.get(job.idempotency_key)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                return QueueEnqueueResult(
                    job=existing,
                    is_new=False,
                    reused=True,
                    status=existing.status,
                )
            self._jobs[job.id] = job
            self._idempotency_map[job.idempotency_key] = job.id
            return QueueEnqueueResult(
                job=job,
                is_new=True,
                reused=False,
                status=job.status,
            )

    def enqueue_new(
        self,
        kind: str,
        idempotency_key: str,
        *,
        content_item_id: str | None = None,
        session_id: str | None = None,
    ) -> QueueEnqueueResult:
        job = AIJob.create(
            kind=kind,
            idempotency_key=idempotency_key,
            content_item_id=content_item_id,
            session_id=session_id,
            clock=self._clock,
        )
        return self.enqueue(job)

    def dequeue(
        self,
        worker_id: str,
        *,
        kinds: Sequence[str] | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> QueuedJobLease | None:
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id must not be empty")

        now = self._now()
        kinds_filter = set(kinds) if kinds else None

        with self._lock:
            candidates: list[AIJob] = []
            for j in self._jobs.values():
                if kinds_filter and j.kind not in kinds_filter:
                    continue
                if j.status is AIJobStatus.QUEUED:
                    candidates.append(j)
                elif j.status is AIJobStatus.RUNNING:
                    active_lease = self._leases.get(j.id)
                    if active_lease and active_lease.is_expired(now):
                        candidates.append(j)

            candidates.sort(key=lambda j: (j.created_at, j.id))
            if not candidates:
                return None

            selected = candidates[0]
            if selected.status is AIJobStatus.RUNNING:
                selected = selected.fail("Lease expired", clock=self._clock)
            started = selected.start(clock=self._clock)
            lease_token = uuid.uuid4().hex
            expires_at = now + lease_duration

            checkpointed = started.checkpoint(
                LEASE_STEP_NAME,
                {
                    "worker_id": worker_id.strip(),
                    "token": lease_token,
                    "expires_at": format_datetime(expires_at),
                },
                clock=self._clock,
            )
            self._jobs[checkpointed.id] = checkpointed
            lease = QueuedJobLease(
                job=checkpointed,
                worker_id=worker_id.strip(),
                lease_token=lease_token,
                leased_at=now,
                expires_at=expires_at,
            )
            self._leases[checkpointed.id] = lease
            return lease

    def ack(self, lease: QueuedJobLease) -> AIJob:
        with self._lock:
            current = self._jobs.get(lease.job.id)
            if current is None:
                raise JobQueueError(JobQueueErrorCode.JOB_NOT_FOUND, "Job not found")
            active_lease = self._leases.get(current.id)
            if not active_lease or active_lease.lease_token != lease.lease_token:
                raise JobQueueError(JobQueueErrorCode.LEASE_INVALID, "Invalid lease")
            succeeded = current.succeed(clock=self._clock)
            self._jobs[succeeded.id] = succeeded
            self._leases.pop(succeeded.id, None)
            return succeeded

    def nack(
        self,
        lease: QueuedJobLease,
        error: str,
        *,
        retryable: bool = False,
    ) -> AIJob:
        with self._lock:
            current = self._jobs.get(lease.job.id)
            if current is None:
                raise JobQueueError(JobQueueErrorCode.JOB_NOT_FOUND, "Job not found")
            active_lease = self._leases.get(current.id)
            if not active_lease or active_lease.lease_token != lease.lease_token:
                raise JobQueueError(JobQueueErrorCode.LEASE_INVALID, "Invalid lease")
            failed = current.fail(error, clock=self._clock)
            self._jobs[failed.id] = failed
            self._leases.pop(failed.id, None)
            return failed

    def renew_lease(
        self,
        lease: QueuedJobLease,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> QueuedJobLease:
        now = self._now()
        with self._lock:
            current = self._jobs.get(lease.job.id)
            if current is None:
                raise JobQueueError(JobQueueErrorCode.JOB_NOT_FOUND, "Job not found")
            active_lease = self._leases.get(current.id)
            if not active_lease or active_lease.lease_token != lease.lease_token:
                raise JobQueueError(JobQueueErrorCode.LEASE_INVALID, "Invalid lease")
            expires_at = now + lease_duration
            renewed_job = current.checkpoint(
                LEASE_STEP_NAME,
                {
                    "worker_id": lease.worker_id,
                    "token": lease.lease_token,
                    "expires_at": format_datetime(expires_at),
                },
                clock=self._clock,
            )
            self._jobs[renewed_job.id] = renewed_job
            new_lease = QueuedJobLease(
                job=renewed_job,
                worker_id=lease.worker_id,
                lease_token=lease.lease_token,
                leased_at=lease.leased_at,
                expires_at=expires_at,
            )
            self._leases[renewed_job.id] = new_lease
            return new_lease

    def reclaim_expired(
        self,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> int:
        now = self._now()
        reclaimed = 0
        with self._lock:
            for job_id, lease in list(self._leases.items()):
                if lease.is_expired(now):
                    job = self._jobs.get(job_id)
                    if job and job.status is AIJobStatus.RUNNING:
                        failed = job.fail("Expired lease reclaimed", clock=self._clock)
                        self._jobs[failed.id] = failed
                        self._leases.pop(job_id, None)
                        reclaimed += 1
        return reclaimed

    def peek(self, limit: int = 50) -> list[AIJob]:
        with self._lock:
            queued = [j for j in self._jobs.values() if j.status is AIJobStatus.QUEUED]
            queued.sort(key=lambda j: (j.created_at, j.id))
            return queued[:limit]

    def size(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status is AIJobStatus.QUEUED)

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)
