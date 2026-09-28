"""Immutable audit events (Prompt Pack v8, prompt #011).

An ``AuditEvent`` records who did what to which entity, when, and how it
ended:

- ``actor``: an ``Actor`` of kind ``user``, ``system`` or ``ai``, so actions
  taken by the AI are never confused with actions taken by a person.
- ``action``: a dotted name such as ``approval.decided``.
- ``entity``: an ``EntityRef`` with a type and an id.
- ``result``: ``success``, ``failure`` or ``denied``.
- ``timestamp``: timezone-aware UTC.
- ``correlation_id``, ``session_id`` and ``job_id``: taken from the active
  ``log_context``.
- ``metadata``: a read-only mapping of JSON scalar values.

Events are frozen and sinks only append, so an event never changes once it is
recorded. Code records events through ``AuditLog``, which it gets from the
container. The sink behind it is chosen in the composition root: in memory for
now, and a database sink once persistence (#029) exists. Tamper evidence and
secure storage belong to the security phase (#216–#223).
"""

import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ai_youtube_agent.core.errors import ApplicationError
from ai_youtube_agent.core.log import current_context, get_logger

logger = get_logger(__name__)

ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
MetadataValue = str | int | float | bool | None
Clock = Callable[[], datetime]


class ActorKind(StrEnum):
    USER = "user"
    SYSTEM = "system"
    AI = "ai"


class AuditResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class AuditError(ApplicationError):
    default_code = "application.audit"


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    id: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("actor id must not be empty")


@dataclass(frozen=True)
class EntityRef:
    type: str
    id: str

    def __post_init__(self) -> None:
        if not self.type or not self.id:
            raise ValueError("entity type and id must not be empty")


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    timestamp: datetime
    action: str
    actor: Actor
    entity: EntityRef
    result: AuditResult
    correlation_id: str | None = None
    session_id: str | None = None
    job_id: str | None = None
    metadata: Mapping[str, MetadataValue] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event id must not be empty")
        if self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        if not ACTION_PATTERN.match(self.action):
            raise ValueError(
                f"action {self.action!r} must be a dotted name like 'job.started'"
            )
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def create(
        cls,
        action: str,
        actor: Actor,
        entity: EntityRef,
        result: AuditResult,
        metadata: Mapping[str, MetadataValue] | None = None,
        *,
        clock: Clock | None = None,
    ) -> "AuditEvent":
        """Build an event with a new id, the current time and the log context."""
        now = clock() if clock else datetime.now(UTC)
        return cls(
            event_id=uuid.uuid4().hex,
            timestamp=now,
            action=action,
            actor=actor,
            entity=entity,
            result=result,
            metadata=metadata or {},
            **current_context(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "actor": {"kind": self.actor.kind.value, "id": self.actor.id},
            "entity": {"type": self.entity.type, "id": self.entity.id},
            "result": self.result.value,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class AuditSink(Protocol):
    """Append-only store of audit events. It has no update or delete."""

    def append(self, event: AuditEvent) -> None: ...

    def events(self) -> tuple[AuditEvent, ...]: ...


class InMemoryAuditSink:
    """Keeps events in process memory. They are lost when the process stops."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            if event.event_id in self._ids:
                raise AuditError(f"audit event {event.event_id} is already recorded")
            self._ids.add(event.event_id)
            self._events.append(event)

    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


class AuditLog:
    """The entry point for recording audit events."""

    def __init__(self, sink: AuditSink, clock: Clock | None = None) -> None:
        self._sink = sink
        self._clock = clock

    def record(
        self,
        action: str,
        actor: Actor,
        entity: EntityRef,
        result: AuditResult,
        metadata: Mapping[str, MetadataValue] | None = None,
    ) -> AuditEvent:
        event = AuditEvent.create(
            action, actor, entity, result, metadata, clock=self._clock
        )
        self._sink.append(event)
        logger.info("audit event", extra={"fields": {"audit": event.as_dict()}})
        return event


def _freeze(metadata: Mapping[str, Any]) -> Mapping[str, MetadataValue]:
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise ValueError(f"metadata key {key!r} must be a string")
        if value is not None and not isinstance(value, str | int | float | bool):
            raise ValueError(
                f"metadata value for {key!r} must be a JSON scalar, "
                f"not {type(value).__name__}"
            )
    return MappingProxyType(dict(metadata))
