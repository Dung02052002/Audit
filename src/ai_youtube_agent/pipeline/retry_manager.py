"""Job Retry Manager with Bounded Retries & Exponential Backoff (Prompt Pack v8, task R-208).

Coordinates retry evaluation, exponential backoff computation, bounded attempt limits,
and persistence transitions for failed pipeline jobs.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    AuditResult,
    EntityRef,
)
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.jobs import AIJobRepository
from ai_youtube_agent.core.errors import DomainError
from ai_youtube_agent.pipeline.job import AIJob, AIJobStatus
from ai_youtube_agent.pipeline.job_queue import JobQueue

Clock = Callable[[], datetime]
RETRY_ACTOR: Final[Actor] = Actor(ActorKind.SYSTEM, "retry_manager")

DEFAULT_NON_RETRYABLE_PATTERNS: Final[frozenset[str]] = frozenset({
    "policy_violation",
    "rights_block",
    "copyright_infringement",
    "validation_error",
    "unauthorized",
    "bad_request",
    "duplicate_blocked",
    "approval_rejected",
})


class RetryManagerError(DomainError):
    """Raised when retry management cannot proceed."""

    default_code = "domain.retry_manager_error"
    default_user_message = "Could not evaluate or schedule a retry for this job."


@dataclass(frozen=True)
class JobRetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 300.0
    backoff_multiplier: float = 2.0
    jitter: bool = True
    non_retryable_patterns: frozenset[str] = DEFAULT_NON_RETRYABLE_PATTERNS

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError("max_attempts must be an integer between 1 and 10")
        if (
            isinstance(self.base_delay_seconds, bool)
            or not isinstance(self.base_delay_seconds, (int, float))
            or self.base_delay_seconds < 0
        ):
            raise ValueError("base_delay_seconds must be a non-negative number")
        if (
            isinstance(self.max_delay_seconds, bool)
            or not isinstance(self.max_delay_seconds, (int, float))
            or self.max_delay_seconds < self.base_delay_seconds
        ):
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if (
            isinstance(self.backoff_multiplier, bool)
            or not isinstance(self.backoff_multiplier, (int, float))
            or self.backoff_multiplier < 1.0
        ):
            raise ValueError("backoff_multiplier must be >= 1.0")

    def delay_after(self, attempt: int, *, jitter_factor: float | None = None) -> float:
        """Computes exponential backoff delay in seconds after a given attempt count."""
        if isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be an integer >= 1")

        raw_delay = self.base_delay_seconds * (self.backoff_multiplier ** (attempt - 1))
        capped_delay = min(self.max_delay_seconds, raw_delay)

        if not self.jitter:
            return float(capped_delay)

        scale = 1.0
        if jitter_factor is not None:
            if not 0.0 <= jitter_factor <= 1.0:
                raise ValueError("jitter_factor must be between 0.0 and 1.0")
            scale = 0.8 + 0.4 * jitter_factor

        return float(min(self.max_delay_seconds, capped_delay * scale))

    def is_retryable_error(self, error_message: str | None) -> bool:
        """Determines if an error is transient and eligible for retry."""
        if not error_message:
            return True
        lowered = error_message.lower()
        return not any(pat in lowered for pat in self.non_retryable_patterns)


@dataclass(frozen=True)
class RetryEvaluation:
    job_id: str
    should_retry: bool
    attempt: int
    max_attempts: int
    delay_seconds: float
    next_eligible_at: datetime | None
    reason: str
    is_terminal: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "should_retry": self.should_retry,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "delay_seconds": self.delay_seconds,
            "next_eligible_at": (
                self.next_eligible_at.isoformat() if self.next_eligible_at else None
            ),
            "reason": self.reason,
            "is_terminal": self.is_terminal,
        }


class RetryManager:
    """Manages failure retries for AIJobs with exponential backoff and jitter."""

    def __init__(
        self,
        database: Database,
        audit_log: AuditLog,
        *,
        policy: JobRetryPolicy | None = None,
        queue: JobQueue | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._audit = audit_log
        self._policy = policy or JobRetryPolicy()
        self._queue = queue
        self._clock = clock

    @property
    def policy(self) -> JobRetryPolicy:
        return self._policy

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    def _evaluate_job(
        self,
        job: AIJob,
        error_message: str | None = None,
        *,
        jitter_factor: float | None = None,
    ) -> RetryEvaluation:
        err = error_message or job.last_error or ""
        current_attempts = job.attempts
        max_attempts = self._policy.max_attempts

        if current_attempts >= max_attempts:
            return RetryEvaluation(
                job_id=job.id,
                should_retry=False,
                attempt=current_attempts,
                max_attempts=max_attempts,
                delay_seconds=0.0,
                next_eligible_at=None,
                reason=f"Exceeded max retry attempts ({current_attempts}/{max_attempts})",
                is_terminal=True,
            )

        if not self._policy.is_retryable_error(err):
            return RetryEvaluation(
                job_id=job.id,
                should_retry=False,
                attempt=current_attempts,
                max_attempts=max_attempts,
                delay_seconds=0.0,
                next_eligible_at=None,
                reason=f"Non-retryable error: {err}",
                is_terminal=True,
            )

        delay = self._policy.delay_after(
            max(1, current_attempts), jitter_factor=jitter_factor
        )
        now = self._now()
        next_eligible = now + timedelta(seconds=delay)

        return RetryEvaluation(
            job_id=job.id,
            should_retry=True,
            attempt=current_attempts,
            max_attempts=max_attempts,
            delay_seconds=delay,
            next_eligible_at=next_eligible,
            reason=f"Eligible for retry (attempt {current_attempts + 1}/{max_attempts}) after {delay:.1f}s",
            is_terminal=False,
        )

    def evaluate_retry(
        self,
        job_id: str,
        error_message: str | None = None,
        *,
        jitter_factor: float | None = None,
    ) -> RetryEvaluation:
        """Evaluates whether a job can be retried without modifying its state."""
        with self._database.connection() as conn:
            job = AIJobRepository(conn).get(job_id)
            if job is None:
                raise RetryManagerError(f"Job {job_id} not found")
            return self._evaluate_job(
                job, error_message, jitter_factor=jitter_factor
            )

    def schedule_retry(
        self,
        job_id: str,
        error_message: str,
        *,
        jitter_factor: float | None = None,
    ) -> RetryEvaluation:
        """Transitions job to FAILED (if running), computes retry backoff, and records checkpoint."""
        updated_job: AIJob
        with self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            job = repo.get(job_id)
            if job is None:
                raise RetryManagerError(f"Job {job_id} not found")

            old_updated_at = job.updated_at

            if job.status is AIJobStatus.RUNNING:
                job = job.fail(error_message, clock=self._clock)
            elif job.status is not AIJobStatus.FAILED:
                raise RetryManagerError(
                    f"Job {job_id} must be in RUNNING or FAILED status to schedule retry, "
                    f"but was {job.status.value}"
                )

            evaluation = self._evaluate_job(
                job, error_message, jitter_factor=jitter_factor
            )

            if evaluation.should_retry:
                checkpoint_data = {
                    "attempt": evaluation.attempt,
                    "max_attempts": evaluation.max_attempts,
                    "delay_seconds": evaluation.delay_seconds,
                    "next_eligible_at": (
                        evaluation.next_eligible_at.isoformat()
                        if evaluation.next_eligible_at
                        else ""
                    ),
                    "error": error_message,
                }
                updated_job = job.record_checkpoint(
                    "retry.scheduled", checkpoint_data, clock=self._clock
                )
            else:
                checkpoint_data = {
                    "attempt": evaluation.attempt,
                    "max_attempts": evaluation.max_attempts,
                    "reason": evaluation.reason,
                    "error": error_message,
                }
                updated_job = job.record_checkpoint(
                    "retry.terminal", checkpoint_data, clock=self._clock
                )

            repo.update(updated_job, expected_updated_at=old_updated_at)

        if evaluation.should_retry:
            if self._queue is not None:
                self._queue.enqueue(updated_job)
            self._audit.record(
                action="job.retry_scheduled",
                actor=RETRY_ACTOR,
                entity=EntityRef("ai_jobs", updated_job.id),
                result=AuditResult.SUCCESS,
                metadata={
                    "attempt": evaluation.attempt,
                    "max_attempts": evaluation.max_attempts,
                    "delay_seconds": evaluation.delay_seconds,
                    "error": error_message,
                },
            )
        else:
            self._audit.record(
                action="job.retry_exhausted" if evaluation.is_terminal else "job.retry_rejected",
                actor=RETRY_ACTOR,
                entity=EntityRef("ai_jobs", updated_job.id),
                result=AuditResult.FAILURE,
                metadata={
                    "attempt": evaluation.attempt,
                    "max_attempts": evaluation.max_attempts,
                    "reason": evaluation.reason,
                    "error": error_message,
                },
            )

        return evaluation

    def execute_retry(self, job_id: str) -> AIJob:
        """Restarts a FAILED job back to RUNNING, incrementing its attempt counter."""
        with self._database.transaction() as conn:
            repo = AIJobRepository(conn)
            job = repo.get(job_id)
            if job is None:
                raise RetryManagerError(f"Job {job_id} not found")

            if job.status is not AIJobStatus.FAILED:
                raise RetryManagerError(
                    f"Job {job_id} must be in FAILED status to execute retry, "
                    f"but was {job.status.value}"
                )

            if job.attempts >= self._policy.max_attempts:
                raise RetryManagerError(
                    f"Job {job_id} cannot be retried: reached max attempts "
                    f"({job.attempts}/{self._policy.max_attempts})"
                )

            old_updated_at = job.updated_at
            restarted_job = job.start(clock=self._clock)
            repo.update(restarted_job, expected_updated_at=old_updated_at)

        self._audit.record(
            action="job.retry_started",
            actor=RETRY_ACTOR,
            entity=EntityRef("ai_jobs", restarted_job.id),
            result=AuditResult.SUCCESS,
            metadata={
                "attempt": restarted_job.attempts,
                "max_attempts": self._policy.max_attempts,
            },
        )
        return restarted_job

    def list_retriable_jobs(self) -> list[AIJob]:
        """Returns all FAILED jobs whose attempt count has not reached max_attempts."""
        with self._database.connection() as conn:
            repo = AIJobRepository(conn)
            all_jobs = repo.list_all()
            return [
                j
                for j in all_jobs
                if j.status is AIJobStatus.FAILED
                and j.attempts < self._policy.max_attempts
                and self._policy.is_retryable_error(j.last_error)
            ]
