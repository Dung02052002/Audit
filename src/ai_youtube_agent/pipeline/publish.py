"""Publish entity (Prompt Pack v8, prompt #022), context C12 Publishing.

A ``PublishJob`` is one request to publish an approved content item:

- ``id``: a stable id for the job.
- ``idempotency_key``: a caller-supplied key. #151 defines how it is generated,
  and persistence and the duplicate guard (#152) keep it unique.
- ``content_item_id``, ``approval_request_id`` and ``content_type``: what is
  published, the approval that allows it, and whether the Shorts (#149) or
  LongForm (#150) publisher handles it.
- ``status``: ``PublishStatus`` queued, in_progress, succeeded or failed, with
  the number of ``attempts`` started and the ``last_error`` of a failed one.
- ``result``: the ``PublishResult`` once the job has succeeded.
- ``created_at`` and ``updated_at``: timezone-aware UTC.

``create`` refuses an approval that is not approved or belongs to another item
(``PublishNotApprovedError``, R-08). The approval gate (#034) still checks that
the approved versions are current. ``start`` begins an attempt from queued or
failed, and ``fail`` or ``succeed`` ends it. Succeeded is final, so a second
upload can never be recorded (``PublishJobStateError``). Retry policy is #153.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.errors import DomainError

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
Clock = Callable[[], datetime]


class PublishStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PublishNotApprovedError(DomainError):
    default_code = "domain.publish_not_approved"
    default_user_message = "This video has no valid approval to publish."


class PublishJobStateError(DomainError):
    default_code = "domain.publish_job_state"
    default_user_message = "This publish job cannot do that in its current state."


@dataclass(frozen=True)
class PublishResult:
    job_id: str
    youtube_video_id: str
    published_at: datetime

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id must not be empty")
        if not VIDEO_ID_PATTERN.match(self.youtube_video_id):
            raise ValueError(
                f"youtube_video_id {self.youtube_video_id!r} must be 11 letters, "
                "digits, '_' or '-'"
            )
        _require_utc(self.published_at, "published_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "youtube_video_id": self.youtube_video_id,
            "published_at": self.published_at.isoformat(),
        }


@dataclass(frozen=True)
class PublishJob:
    id: str
    idempotency_key: str
    content_item_id: str
    approval_request_id: str
    content_type: ContentType
    status: PublishStatus
    attempts: int
    last_error: str | None
    result: PublishResult | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "content_item_id", "approval_request_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not self.idempotency_key or any(
            char.isspace() for char in self.idempotency_key
        ):
            raise ValueError("idempotency_key must not be empty or contain whitespace")
        if not isinstance(self.content_type, ContentType):
            raise TypeError("content_type must be a ContentType")
        if not isinstance(self.status, PublishStatus):
            raise TypeError("status must be a PublishStatus")
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 0
        ):
            raise ValueError("attempts must be a whole number of 0 or more")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        self._check_state()

    @classmethod
    def create(
        cls,
        approval: ApprovalRequest,
        content_type: ContentType,
        idempotency_key: str,
        *,
        clock: Clock | None = None,
    ) -> "PublishJob":
        if approval.status is not ApprovalStatus.APPROVED:
            raise PublishNotApprovedError(
                f"approval {approval.id} is {approval.status.value}, not approved"
            )
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            idempotency_key=idempotency_key,
            content_item_id=approval.content_item_id,
            approval_request_id=approval.id,
            content_type=content_type,
            status=PublishStatus.QUEUED,
            attempts=0,
            last_error=None,
            result=None,
            created_at=now,
            updated_at=now,
        )

    def start(self, *, clock: Clock | None = None) -> "PublishJob":
        if self.status not in (PublishStatus.QUEUED, PublishStatus.FAILED):
            raise self._state_error("start")
        return replace(
            self,
            status=PublishStatus.IN_PROGRESS,
            attempts=self.attempts + 1,
            last_error=None,
            updated_at=_now(clock),
        )

    def fail(self, reason: str, *, clock: Clock | None = None) -> "PublishJob":
        if self.status is not PublishStatus.IN_PROGRESS:
            raise self._state_error("fail")
        return replace(
            self,
            status=PublishStatus.FAILED,
            last_error=reason.strip(),
            updated_at=_now(clock),
        )

    def succeed(
        self, youtube_video_id: str, *, clock: Clock | None = None
    ) -> "PublishJob":
        if self.status is not PublishStatus.IN_PROGRESS:
            raise self._state_error("succeed")
        now = _now(clock)
        return replace(
            self,
            status=PublishStatus.SUCCEEDED,
            result=PublishResult(self.id, youtube_video_id, now),
            updated_at=now,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "content_item_id": self.content_item_id,
            "approval_request_id": self.approval_request_id,
            "content_type": self.content_type.value,
            "status": self.status.value,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "result": self.result.as_dict() if self.result else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def _state_error(self, action: str) -> PublishJobStateError:
        return PublishJobStateError(
            f"cannot {action} publish job {self.id} while {self.status.value}"
        )

    def _check_state(self) -> None:
        if (self.status is PublishStatus.QUEUED) != (self.attempts == 0):
            raise ValueError("a queued job has no attempts, and only a queued job")
        if self.status is PublishStatus.FAILED:
            if self.last_error is None or not self.last_error.strip():
                raise ValueError("a failed job needs a last_error")
        elif self.last_error is not None:
            raise ValueError("only a failed job has a last_error")
        if self.status is PublishStatus.SUCCEEDED:
            if self.result is None:
                raise ValueError("a succeeded job needs a result")
            if self.result.job_id != self.id:
                raise ValueError("the result belongs to another job")
            if not self.created_at <= self.result.published_at <= self.updated_at:
                raise ValueError("published_at must be within the job's lifetime")
        elif self.result is not None:
            raise ValueError("only a succeeded job has a result")


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
