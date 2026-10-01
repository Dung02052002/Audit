"""C-040 Kill Switch Gate (Prompt Pack v8, prompt #040).

Rules the user approved on 2026-10-01:

- the gate reads an ``EmergencyStop`` through a protocol; the store and the
  toggle are #213;
- an active stop blocks every move into ``generating`` and the move to
  ``publishing``; other moves pass;
- the block has one ``killswitch.active`` reason with a fixed message plus the
  activator's reason, and does not name the activator.
"""

import dataclasses
from datetime import UTC, datetime

import pytest

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.content_item import (
    ContentItem,
    ContentStatus,
    ContentType,
    can_transition,
)
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.kill_switch_gate import STOP_MESSAGE, KillSwitchGate
from ai_youtube_agent.pipeline.kill_switch import EmergencyStop

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
USER = Actor(ActorKind.USER, "owner")


class Switch:
    """In-memory stand-in for the kill switch store of #213."""

    def __init__(self) -> None:
        self.stop = EmergencyStop.inactive()
        self.reads = 0

    def current(self) -> EmergencyStop:
        self.reads += 1
        return self.stop


def at(moment: datetime):
    return lambda: moment


def item_in(status: ContentStatus) -> ContentItem:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=at(T0)
    )
    return dataclasses.replace(item, status=status)


def context(item: ContentItem, target: ContentStatus) -> GateContext:
    return GateContext(item=item, target_status=target, actor=SYSTEM, at=T0)


def allowed_moves() -> list[tuple[ContentStatus, ContentStatus]]:
    return [
        (source, target)
        for source in ContentStatus
        for target in ContentStatus
        if can_transition(source, target)
    ]


STOPPED = [
    m
    for m in allowed_moves()
    if m[1] in (ContentStatus.GENERATING, ContentStatus.PUBLISHING)
]
OTHER = [m for m in allowed_moves() if m not in STOPPED]


@pytest.fixture
def switch() -> Switch:
    return Switch()


@pytest.fixture
def gate(switch: Switch) -> KillSwitchGate:
    return KillSwitchGate(switch)


def activate(switch: Switch, reason: str | None = None) -> None:
    switch.stop = EmergencyStop.activated(USER, reason=reason, clock=at(T0))


# EmergencyStop


def test_an_inactive_stop_carries_nothing() -> None:
    assert EmergencyStop.inactive().as_dict() == {
        "active": False,
        "activated_by": None,
        "activated_at": None,
        "reason": None,
    }
    with pytest.raises(ValueError):
        EmergencyStop(False, activated_by=USER)
    with pytest.raises(ValueError):
        EmergencyStop(False, reason="why")


def test_an_active_stop_needs_an_activator_and_a_utc_time() -> None:
    with pytest.raises(TypeError):
        EmergencyStop(True, activated_at=T0)
    with pytest.raises(ValueError):
        EmergencyStop(True, activated_by=USER)
    with pytest.raises(ValueError):
        EmergencyStop(True, activated_by=USER, activated_at=datetime(2026, 10, 1))
    with pytest.raises(ValueError):
        EmergencyStop(True, activated_by=USER, activated_at=T0, reason=" ")
    with pytest.raises(TypeError):
        EmergencyStop(1)  # type: ignore[arg-type]


def test_activated_records_who_when_and_why() -> None:
    stop = EmergencyStop.activated(USER, reason="  wrong upload  ", clock=at(T0))

    assert stop.as_dict() == {
        "active": True,
        "activated_by": {"kind": "user", "id": "owner"},
        "activated_at": T0.isoformat(),
        "reason": "wrong upload",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        stop.active = False  # type: ignore[misc]


# Contract


def test_the_gate_follows_the_contract(gate: KillSwitchGate) -> None:
    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.KILL_SWITCH


# Passing


@pytest.mark.parametrize("move", STOPPED, ids=lambda m: f"{m[0]}-{m[1]}")
def test_production_moves_pass_while_the_stop_is_off(
    gate: KillSwitchGate, move
) -> None:
    result = gate.evaluate(context(item_in(move[0]), move[1]))

    assert result.outcome is GateOutcome.PASS
    assert result.gate is GateName.KILL_SWITCH
    assert result.evaluated_at == T0


@pytest.mark.parametrize("move", OTHER, ids=lambda m: f"{m[0]}-{m[1]}")
def test_other_moves_pass_while_the_stop_is_on(
    switch: Switch, gate: KillSwitchGate, move
) -> None:
    activate(switch)

    assert gate.evaluate(context(item_in(move[0]), move[1])).is_passed


def test_the_stopped_moves_are_new_production_and_publishing() -> None:
    assert {t for _, t in STOPPED} == {
        ContentStatus.GENERATING,
        ContentStatus.PUBLISHING,
    }
    assert (ContentStatus.DRAFT, ContentStatus.GENERATING) in STOPPED
    assert (ContentStatus.APPROVED, ContentStatus.PUBLISHING) in STOPPED
    assert OTHER  # winding down stays possible


# Blocking


@pytest.mark.parametrize("move", STOPPED, ids=lambda m: f"{m[0]}-{m[1]}")
def test_an_active_stop_blocks_production_and_publishing(
    switch: Switch, gate: KillSwitchGate, move
) -> None:
    activate(switch)

    result = gate.evaluate(context(item_in(move[0]), move[1]))

    assert result.outcome is GateOutcome.BLOCK
    assert [r.code for r in result.reasons] == ["killswitch.active"]
    assert result.reasons[0].message == STOP_MESSAGE


def test_the_message_adds_the_reason_but_not_the_activator(
    switch: Switch, gate: KillSwitchGate
) -> None:
    activate(switch, reason="Copyright claim on channel")

    result = gate.evaluate(
        context(item_in(ContentStatus.APPROVED), ContentStatus.PUBLISHING)
    )

    message = result.reasons[0].message
    assert message == f"{STOP_MESSAGE} Reason: Copyright claim on channel"
    assert "owner" not in message


def test_the_state_is_read_on_every_evaluation(
    switch: Switch, gate: KillSwitchGate
) -> None:
    ctx = context(item_in(ContentStatus.DRAFT), ContentStatus.GENERATING)

    before = gate.evaluate(ctx)
    activate(switch)
    during = gate.evaluate(ctx)
    switch.stop = EmergencyStop.inactive()
    after = gate.evaluate(ctx)

    assert [before.is_passed, during.is_passed, after.is_passed] == [
        True,
        False,
        True,
    ]
    assert switch.reads == 3


def test_a_failing_source_blocks_through_evaluate_gates() -> None:
    class Broken:
        def current(self) -> EmergencyStop:
            raise RuntimeError("store unavailable")

    report = evaluate_gates(
        [KillSwitchGate(Broken())],
        context(item_in(ContentStatus.DRAFT), ContentStatus.GENERATING),
    )

    assert report.outcome is GateOutcome.BLOCK
    assert [r.code for r in report.reasons] == ["gate.error"]
