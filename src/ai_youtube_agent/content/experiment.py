"""Experiment entity (Prompt Pack v8, prompt #028), context C13 Analytics.

An ``Experiment`` is an entry in the experiment registry:

- ``id``, ``channel_id`` and an optional ``content_item_id``.
- ``type``: ``ExperimentType`` title, thumbnail or content.
- ``hypothesis``: what the experiment is meant to find out.
- ``variants``: two or more ``ExperimentVariant`` values, each with a unique
  ``key`` and a ``value``: the title text, a thumbnail artifact id, or a
  content description.
- ``status``: ``ExperimentStatus`` proposed, running, concluded or cancelled.
- ``proposed_by``, ``started_by``, ``cancelled_by`` and ``conclusion``: who did
  what, and the recorded result.
- ``created_at`` and ``updated_at``: timezone-aware UTC.

Any actor, including the AI, may propose an experiment. Only a user may start,
conclude or cancel one (``ExperimentNotAllowedError``). The AI or the system
may analyse results and suggest a winner, but a user makes the call:
``conclude`` records an ``ExperimentConclusion`` with an optional winning
variant, a note, the metric snapshots used as evidence, the concluding user
and a UTC time. Concluded and cancelled are final (``ExperimentStateError``).

The registry never changes strategy (R-09). A conclusion is only a record:
there is no method to apply or promote a winner, and nothing here touches the
``StrategyProfile``, a title, a thumbnail or any setting. Applying a result is
always a separate user action.
"""

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

VARIANT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
Clock = Callable[[], datetime]


class ExperimentType(StrEnum):
    TITLE = "title"
    THUMBNAIL = "thumbnail"
    CONTENT = "content"


class ExperimentStatus(StrEnum):
    PROPOSED = "proposed"
    RUNNING = "running"
    CONCLUDED = "concluded"
    CANCELLED = "cancelled"


class ExperimentNotAllowedError(DomainError):
    default_code = "domain.experiment_not_allowed"
    default_user_message = "Only a user can start, conclude or cancel an experiment."


class ExperimentStateError(DomainError):
    default_code = "domain.experiment_state"
    default_user_message = "This experiment cannot do that in its current state."


@dataclass(frozen=True)
class ExperimentVariant:
    key: str
    value: str

    def __post_init__(self) -> None:
        if not VARIANT_KEY_PATTERN.match(self.key):
            raise ValueError(
                f"variant key {self.key!r} must be a lowercase name such as 'a'"
            )
        if not self.value.strip():
            raise ValueError("variant value must not be empty")

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "value": self.value}


@dataclass(frozen=True)
class ExperimentConclusion:
    winner_key: str | None
    note: str | None
    metric_snapshot_ids: tuple[str, ...]
    concluded_by: Actor
    concluded_at: datetime

    def __post_init__(self) -> None:
        if self.note is not None and not self.note.strip():
            raise ValueError("note must not be empty when given")
        if not isinstance(self.metric_snapshot_ids, tuple):
            raise ValueError("metric_snapshot_ids must be a tuple")
        if any(not snapshot_id.strip() for snapshot_id in self.metric_snapshot_ids):
            raise ValueError("metric snapshot ids must not be empty")
        if len(set(self.metric_snapshot_ids)) != len(self.metric_snapshot_ids):
            raise ValueError("metric snapshot ids must not repeat")
        _ensure_user(self.concluded_by)
        _require_utc(self.concluded_at, "concluded_at")

    @property
    def is_inconclusive(self) -> bool:
        return self.winner_key is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "winner_key": self.winner_key,
            "note": self.note,
            "metric_snapshot_ids": list(self.metric_snapshot_ids),
            "concluded_by": _actor_dict(self.concluded_by),
            "concluded_at": self.concluded_at.isoformat(),
        }


@dataclass(frozen=True)
class Experiment:
    id: str
    channel_id: str
    content_item_id: str | None
    type: ExperimentType
    hypothesis: str
    variants: tuple[ExperimentVariant, ...]
    status: ExperimentStatus
    proposed_by: Actor
    created_at: datetime
    updated_at: datetime
    started_by: Actor | None = None
    cancelled_by: Actor | None = None
    conclusion: ExperimentConclusion | None = None

    def __post_init__(self) -> None:
        for name in ("id", "channel_id", "hypothesis"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.content_item_id is not None and not self.content_item_id.strip():
            raise ValueError("content_item_id must not be empty when given")
        if not isinstance(self.type, ExperimentType):
            raise TypeError("type must be an ExperimentType")
        if not isinstance(self.status, ExperimentStatus):
            raise TypeError("status must be an ExperimentStatus")
        if not isinstance(self.proposed_by, Actor):
            raise TypeError("proposed_by must be an Actor")
        self._check_variants()
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        self._check_state()

    @classmethod
    def propose(
        cls,
        channel_id: str,
        type: ExperimentType,
        *,
        hypothesis: str,
        variants: Iterable[ExperimentVariant],
        proposed_by: Actor,
        content_item_id: str | None = None,
        clock: Clock | None = None,
    ) -> "Experiment":
        now = _now(clock)
        return cls(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            content_item_id=content_item_id,
            type=type,
            hypothesis=hypothesis.strip(),
            variants=tuple(variants),
            status=ExperimentStatus.PROPOSED,
            proposed_by=proposed_by,
            created_at=now,
            updated_at=now,
        )

    @property
    def variant_keys(self) -> tuple[str, ...]:
        return tuple(variant.key for variant in self.variants)

    @property
    def is_final(self) -> bool:
        return self.status in (ExperimentStatus.CONCLUDED, ExperimentStatus.CANCELLED)

    def start(self, *, actor: Actor, clock: Clock | None = None) -> "Experiment":
        _ensure_user(actor)
        if self.status is not ExperimentStatus.PROPOSED:
            raise self._state_error("start")
        return replace(
            self,
            status=ExperimentStatus.RUNNING,
            started_by=actor,
            updated_at=_now(clock),
        )

    def conclude(
        self,
        *,
        actor: Actor,
        winner_key: str | None,
        note: str | None = None,
        metric_snapshot_ids: Iterable[str] = (),
        clock: Clock | None = None,
    ) -> "Experiment":
        _ensure_user(actor)
        if self.status is not ExperimentStatus.RUNNING:
            raise self._state_error("conclude")
        now = _now(clock)
        conclusion = ExperimentConclusion(
            winner_key=winner_key,
            note=note.strip() if note is not None else None,
            metric_snapshot_ids=tuple(metric_snapshot_ids),
            concluded_by=actor,
            concluded_at=now,
        )
        return replace(
            self,
            status=ExperimentStatus.CONCLUDED,
            conclusion=conclusion,
            updated_at=now,
        )

    def cancel(self, *, actor: Actor, clock: Clock | None = None) -> "Experiment":
        _ensure_user(actor)
        if self.is_final:
            raise self._state_error("cancel")
        return replace(
            self,
            status=ExperimentStatus.CANCELLED,
            cancelled_by=actor,
            updated_at=_now(clock),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "content_item_id": self.content_item_id,
            "type": self.type.value,
            "hypothesis": self.hypothesis,
            "variants": [variant.as_dict() for variant in self.variants],
            "status": self.status.value,
            "proposed_by": _actor_dict(self.proposed_by),
            "started_by": _actor_dict(self.started_by) if self.started_by else None,
            "cancelled_by": (
                _actor_dict(self.cancelled_by) if self.cancelled_by else None
            ),
            "conclusion": self.conclusion.as_dict() if self.conclusion else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def _state_error(self, action: str) -> ExperimentStateError:
        return ExperimentStateError(
            f"cannot {action} experiment {self.id} while {self.status.value}"
        )

    def _check_variants(self) -> None:
        if not isinstance(self.variants, tuple) or len(self.variants) < 2:
            raise ValueError("an experiment needs a tuple of at least two variants")
        if any(not isinstance(v, ExperimentVariant) for v in self.variants):
            raise TypeError("variants must be ExperimentVariant values")
        if len(set(self.variant_keys)) != len(self.variant_keys):
            raise ValueError("variant keys must not repeat")

    def _check_state(self) -> None:
        status = self.status
        needs_start = status in (ExperimentStatus.RUNNING, ExperimentStatus.CONCLUDED)
        if needs_start and self.started_by is None:
            raise ValueError(f"a {status.value} experiment needs started_by")
        if status is ExperimentStatus.PROPOSED and self.started_by is not None:
            raise ValueError("a proposed experiment has not been started")
        if self.started_by is not None:
            _ensure_user(self.started_by)
        if status is ExperimentStatus.CANCELLED:
            if self.cancelled_by is None:
                raise ValueError("a cancelled experiment needs cancelled_by")
            _ensure_user(self.cancelled_by)
        elif self.cancelled_by is not None:
            raise ValueError("only a cancelled experiment has cancelled_by")
        if status is ExperimentStatus.CONCLUDED:
            if self.conclusion is None:
                raise ValueError("a concluded experiment needs a conclusion")
            winner = self.conclusion.winner_key
            if winner is not None and winner not in self.variant_keys:
                raise ValueError(f"winner {winner!r} is not one of the variants")
            if not self.created_at <= self.conclusion.concluded_at <= self.updated_at:
                raise ValueError("concluded_at must be within the experiment")
        elif self.conclusion is not None:
            raise ValueError("only a concluded experiment has a conclusion")


def _ensure_user(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.kind is not ActorKind.USER:
        raise ExperimentNotAllowedError(
            f"actor {actor!r} may not start, conclude or cancel an experiment"
        )


def _actor_dict(actor: Actor) -> dict[str, str]:
    return {"kind": actor.kind.value, "id": actor.id}


def _require_utc(moment: datetime, name: str) -> None:
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _now(clock: Clock | None) -> datetime:
    return clock() if clock else datetime.now(UTC)
