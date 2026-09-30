"""Pipeline Gate Contract (Prompt Pack v8, prompt #033), context C3 Control Gates.

One shared interface for the Test, QC, Rights, Policy and Approval gates, as
the user approved on 2026-09-30:

- ``GateName`` is a closed enum: the five gates of #033, then one name per
  later gate (#036 ``daily_limit``, #037 ``budget``; #038-#041 add theirs
  when they come).
- A gate returns a ``GateResult`` that either passes or blocks. A block carries
  one or more ``GateReason`` values (a stable dotted code and a safe message).
- A gate receives a ``GateContext``: the ``ContentItem``, the status it is asked
  to move to (publishing is the move to ``PUBLISHING``), the actor and the time.
  A gate reads anything else it needs through repositories given to it when it
  is built.
- ``evaluate_gates`` runs every gate in order and returns a ``GateReport`` with
  every result. The report blocks if any gate blocks.
- Gates fail closed: a gate that raises, or returns something that is not its
  own ``GateResult``, counts as a block. Its detail goes to the log only.

Gates are synchronous, like the repositories they read from. This module has no
concrete gate; those are #034-#041.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ai_youtube_agent.core.audit import ACTION_PATTERN, Actor
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentTransitionError,
    can_transition,
)
from ai_youtube_agent.core.errors import AppError, DomainError
from ai_youtube_agent.core.log import get_logger

logger = get_logger(__name__)

GATE_ERROR_MESSAGE = "A check could not be completed, so the action is blocked."


class GateName(StrEnum):
    TEST = "test"
    QC = "qc"
    RIGHTS = "rights"
    POLICY = "policy"
    APPROVAL = "approval"
    DAILY_LIMIT = "daily_limit"  # #036
    BUDGET = "budget"  # #037


class GateOutcome(StrEnum):
    PASS = "pass"
    BLOCK = "block"


@dataclass(frozen=True)
class GateReason:
    """Why a gate blocked. ``message`` is shown to users, so keep it safe."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not ACTION_PATTERN.match(self.code):
            raise ValueError(
                f"reason code {self.code!r} must be a dotted name "
                "like 'rights.unresolved'"
            )
        if not self.message.strip():
            raise ValueError("reason message must not be empty")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class GateResult:
    gate: GateName
    outcome: GateOutcome
    reasons: tuple[GateReason, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.gate, GateName):
            raise TypeError("gate must be a GateName")
        if not isinstance(self.outcome, GateOutcome):
            raise TypeError("outcome must be a GateOutcome")
        if not isinstance(self.reasons, tuple) or not all(
            isinstance(r, GateReason) for r in self.reasons
        ):
            raise TypeError("reasons must be a tuple of GateReason values")
        if self.outcome is GateOutcome.PASS and self.reasons:
            raise ValueError("a passed gate has no reasons")
        if self.outcome is GateOutcome.BLOCK and not self.reasons:
            raise ValueError("a blocked gate needs at least one reason")
        _require_utc(self.evaluated_at, "evaluated_at")

    @classmethod
    def passed(cls, gate: GateName, at: datetime) -> "GateResult":
        return cls(gate, GateOutcome.PASS, (), at)

    @classmethod
    def blocked(
        cls, gate: GateName, at: datetime, *reasons: GateReason
    ) -> "GateResult":
        return cls(gate, GateOutcome.BLOCK, reasons, at)

    @property
    def is_passed(self) -> bool:
        return self.outcome is GateOutcome.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "outcome": self.outcome.value,
            "reasons": [r.as_dict() for r in self.reasons],
            "evaluated_at": self.evaluated_at.isoformat(),
        }


@dataclass(frozen=True)
class GateContext:
    """What a gate is asked: may ``item`` move to ``target_status`` now?"""

    item: ContentItem
    target_status: ContentStatus
    actor: Actor
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.item, ContentItem):
            raise TypeError("item must be a ContentItem")
        if not isinstance(self.target_status, ContentStatus):
            raise TypeError("target_status must be a ContentStatus")
        if not isinstance(self.actor, Actor):
            raise TypeError("actor must be an Actor")
        _require_utc(self.at, "at")
        if self.target_status is self.item.status:
            raise ValueError("a gate is only asked about a change of status")
        if not can_transition(self.item.status, self.target_status):
            raise ContentTransitionError(
                self.item.id, self.item.status, self.target_status
            )


@runtime_checkable
class PipelineGate(Protocol):
    name: GateName

    def evaluate(self, context: GateContext) -> GateResult: ...


@dataclass(frozen=True)
class GateReport:
    results: tuple[GateResult, ...]

    def __post_init__(self) -> None:
        if not self.results:
            raise ValueError("a gate report needs at least one result")

    @property
    def outcome(self) -> GateOutcome:
        return GateOutcome.PASS if not self.blocked else GateOutcome.BLOCK

    @property
    def is_passed(self) -> bool:
        return self.outcome is GateOutcome.PASS

    @property
    def blocked(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if not r.is_passed)

    @property
    def reasons(self) -> tuple[GateReason, ...]:
        return tuple(reason for r in self.blocked for reason in r.reasons)

    def raise_if_blocked(self) -> None:
        if not self.is_passed:
            raise GateBlockedError(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "results": [r.as_dict() for r in self.results],
        }


class GateBlockedError(DomainError):
    default_code = "domain.gate_blocked"
    default_user_message = "This action is blocked by one or more checks."

    def __init__(self, report: GateReport) -> None:
        gates = ", ".join(r.gate.value for r in report.blocked)
        super().__init__(f"blocked by gates: {gates}")
        self.report = report

    def log_fields(self) -> dict[str, Any]:
        return {
            **super().log_fields(),
            "blocked_gates": [r.gate.value for r in self.report.blocked],
            "reason_codes": [r.code for r in self.report.reasons],
        }


def evaluate_gates(gates: Sequence[PipelineGate], context: GateContext) -> GateReport:
    """Run every gate in order and report every result."""
    if not gates:
        raise ValueError("at least one gate is needed")
    names = [gate.name for gate in gates]
    for name in names:
        if not isinstance(name, GateName):
            raise TypeError("every gate needs a GateName")
    if len(set(names)) != len(names):
        raise ValueError("each gate may run only once")
    return GateReport(tuple(_evaluate(gate, context) for gate in gates))


def _evaluate(gate: PipelineGate, context: GateContext) -> GateResult:
    try:
        result = gate.evaluate(context)
    except Exception as exc:
        extra = exc.log_fields() if isinstance(exc, AppError) else {}
        logger.warning(
            "gate failed",
            exc_info=exc,
            extra={"fields": _fields(gate, context, **extra)},
        )
        return _fail_closed(gate, context, "gate.error")
    if not isinstance(result, GateResult) or result.gate is not gate.name:
        logger.warning(
            "gate returned an invalid result",
            extra={"fields": _fields(gate, context, result_type=type(result).__name__)},
        )
        return _fail_closed(gate, context, "gate.invalid_result")
    return result


def _fail_closed(gate: PipelineGate, context: GateContext, code: str) -> GateResult:
    return GateResult.blocked(
        gate.name, context.at, GateReason(code, GATE_ERROR_MESSAGE)
    )


def _fields(gate: PipelineGate, context: GateContext, **extra: Any) -> dict[str, Any]:
    return {
        "gate": gate.name.value,
        "content_item_id": context.item.id,
        "from_status": context.item.status.value,
        "to_status": context.target_status.value,
        **extra,
    }


def _require_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
