"""AI job and session entities (Prompt Pack v8, prompt #027), context C16.

- ``Session``: a user-initiated working session (#209). Only a user may start
  one (``SessionNotAllowedError``). It is active until ``stop`` is called.
- ``AIJob``: one resumable unit of AI work, such as ``script.generate``. It has
  a ``kind``, a caller-supplied ``idempotency_key`` (so #041 can block
  duplicates), an optional content item and session, a ``status``, the number
  of ``attempts`` started, the ``last_error`` of a failed attempt, and an
  append-only history of ``JobCheckpoint`` values.

``start`` begins an attempt from queued or failed, ``checkpoint`` saves a safe
step while running, and ``fail`` or ``succeed`` ends the attempt. ``cancel``
stops a job that has not finished. Succeeded and cancelled are final
(``AIJobStateError``). Checkpoints survive a failed attempt, so a retry can
resume from ``last_checkpoint`` (#211). ``waiting`` is reserved for approval
wait (#207), and retry limits come with #208.
"""

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ai_youtube_agent.core.audit import Actor, ActorKind, MetadataValue
from ai_youtube_agent.core.errors import DomainError

DOTTED_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
Clock = Callable[[], datetime]


class SessionStatus(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"


class AIJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINAL_STATUSES = frozenset({AIJobStatus.SUCCEEDED, AIJobStatus.CANCELLED})


class SessionNotAllowedError(DomainError):
    default_code = "domain.session_not_allowed"
    default_user_message = "Only a user can start a session."


class AIJobStateError(DomainError):
    default_code = "domain.ai_job_state"
    default_user_message = "This job cannot do that in its current state."


@dataclass(frozen=True)
class Session:
    id: str
    started_by: Actor
    status: SessionStatus
    started_at: datetime
    stopped_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        _ensure_user(self.started_by)
        if not isinstance(self.status, SessionStatus):
            raise TypeError("status must be a SessionStatus")
        _require_utc(self.started_at, "started_at")
        if self.status is SessionStatus.STOPPED:
            if self.stopped_at is None:
                raise ValueError("a stopped session needs stopped_at")
            _require_utc(self.stopped_at, "stopped_at")
            if self.stopped_at < self.started_at:
                raise ValueError("stopped_at must not be earlier than started_at")
        elif self.stopped_at is not None:
            raise ValueError("an active session has no stopped_at")

    @classmethod
    def start(cls, *, actor: Actor, clock: Clock | None = None) -> "Session":
        _ensure_user(actor)
        return cls(
            id=_new_id(),
            started_by=actor,
            status=SessionStatus.ACTIVE,
            started_at=_now(clock),
        )

    @property
    def is_active(self) -> bool:
        return self.status is SessionStatus.ACTIVE

    def stop(self, *, clock: Clock | None = None) -> "Session":
        if not self.is_active:
            return self
        return replace(self, status=SessionStatus.STOPPED, stopped_at=_now(clock))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_by": _actor_dict(self.started_by),
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }


@dataclass(frozen=True)
class JobCheckpoint:
    sequence: int
    step: str
    created_at: datetime
    data: Mapping[str, MetadataValue] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise ValueError("sequence must be a whole number of 1 or more")
        if not DOTTED_NAME_PATTERN.match(self.step):
            raise ValueError(
                f"step {self.step!r} must be a lowercase dotted name such as "
                "'script.draft_saved'"
            )
        _require_utc(self.created_at, "created_at")
        object.__setattr__(self, "data", _freeze(self.data))

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "step": self.step,
            "created_at": self.created_at.isoformat(),
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class AIJob:
    id: str
    kind: str
    idempotency_key: str
    content_item_id: str | None
    session_id: str | None
    status: AIJobStatus
    attempts: int
    last_error: str | None
    checkpoints: tuple[JobCheckpoint, ...]
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        if not DOTTED_NAME_PATTERN.match(self.kind):
            raise ValueError(
                f"kind {self.kind!r} must be a lowercase dotted name such as "
                "'script.generate'"
            )
        if not self.idempotency_key or any(
            char.isspace() for char in self.idempotency_key
        ):
            raise ValueError("idempotency_key must not be empty or contain whitespace")
        for name in ("content_item_id", "session_id"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be empty when given")
        if not isinstance(self.status, AIJobStatus):
            raise TypeError("status must be an AIJobStatus")
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
        self._check_checkpoints()

    @classmethod
    def create(
        cls,
        kind: str,
        idempotency_key: str,
        *,
        content_item_id: str | None = None,
        session_id: str | None = None,
        clock: Clock | None = None,
    ) -> "AIJob":
        now = _now(clock)
        return cls(
            id=_new_id(),
            kind=kind,
            idempotency_key=idempotency_key,
            content_item_id=content_item_id,
            session_id=session_id,
            status=AIJobStatus.QUEUED,
            attempts=0,
            last_error=None,
            checkpoints=(),
            created_at=now,
            updated_at=now,
        )

    @property
    def last_checkpoint(self) -> JobCheckpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES

    def start(self, *, clock: Clock | None = None) -> "AIJob":
        if self.status not in (AIJobStatus.QUEUED, AIJobStatus.FAILED):
            raise self._state_error("start")
        return replace(
            self,
            status=AIJobStatus.RUNNING,
            attempts=self.attempts + 1,
            last_error=None,
            updated_at=_now(clock),
        )

    def checkpoint(
        self,
        step: str,
        data: Mapping[str, MetadataValue] | None = None,
        *,
        clock: Clock | None = None,
    ) -> "AIJob":
        if self.status is not AIJobStatus.RUNNING:
            raise self._state_error("checkpoint")
        now = _now(clock)
        saved = JobCheckpoint(len(self.checkpoints) + 1, step, now, data or {})
        return replace(self, checkpoints=(*self.checkpoints, saved), updated_at=now)

    def fail(self, reason: str, *, clock: Clock | None = None) -> "AIJob":
        if self.status is not AIJobStatus.RUNNING:
            raise self._state_error("fail")
        return replace(
            self,
            status=AIJobStatus.FAILED,
            last_error=reason.strip(),
            updated_at=_now(clock),
        )

    def succeed(self, *, clock: Clock | None = None) -> "AIJob":
        if self.status is not AIJobStatus.RUNNING:
            raise self._state_error("succeed")
        return replace(self, status=AIJobStatus.SUCCEEDED, updated_at=_now(clock))

    def cancel(self, *, clock: Clock | None = None) -> "AIJob":
        if self.is_final:
            raise self._state_error("cancel")
        return replace(
            self,
            status=AIJobStatus.CANCELLED,
            last_error=None,
            updated_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "idempotency_key": self.idempotency_key,
            "content_item_id": self.content_item_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "checkpoints": [saved.as_dict() for saved in self.checkpoints],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def _state_error(self, action: str) -> AIJobStateError:
        return AIJobStateError(
            f"cannot {action} job {self.id} while {self.status.value}"
        )

    def _check_state(self) -> None:
        if self.status is AIJobStatus.QUEUED and self.attempts != 0:
            raise ValueError("a queued job has no attempts")
        started = (
            AIJobStatus.RUNNING,
            AIJobStatus.WAITING,
            AIJobStatus.SUCCEEDED,
            AIJobStatus.FAILED,
        )
        if self.status in started and self.attempts < 1:
            raise ValueError(f"a {self.status.value} job has at least one attempt")
        if self.status is AIJobStatus.FAILED:
            if self.last_error is None or not self.last_error.strip():
                raise ValueError("a failed job needs a last_error")
        elif self.last_error is not None:
            raise ValueError("only a failed job has a last_error")

    def _check_checkpoints(self) -> None:
        if not isinstance(self.checkpoints, tuple):
            raise ValueError("checkpoints must be a tuple")
        previous = self.created_at
        for expected, saved in enumerate(self.checkpoints, start=1):
            if not isinstance(saved, JobCheckpoint):
                raise TypeError("checkpoints must be JobCheckpoint values")
            if saved.sequence != expected:
                raise ValueError("checkpoint sequences must run 1, 2, 3 and so on")
            if not previous <= saved.created_at <= self.updated_at:
                raise ValueError("checkpoints must be in time order within the job")
            previous = saved.created_at


def _freeze(data: Mapping[str, Any]) -> Mapping[str, MetadataValue]:
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(f"checkpoint data key {key!r} must be a string")
        if value is not None and not isinstance(value, str | int | float | bool):
            raise ValueError(
                f"checkpoint data for {key!r} must be a JSON scalar, "
                f"not {type(value).__name__}"
            )
    return MappingProxyType(dict(data))


def _ensure_user(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.kind is not ActorKind.USER:
        raise SessionNotAllowedError(f"actor {actor!r} may not start a session")


def _actor_dict(actor: Actor) -> dict[str, str]:
    return {"kind": actor.kind.value, "id": actor.id}


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _new_id() -> str:
    return uuid.uuid4().hex


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
