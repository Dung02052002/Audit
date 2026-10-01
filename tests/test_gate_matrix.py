"""C-042 Gate Tests (Prompt Pack v8, prompt #042).

Every concrete gate against every status move, as the user approved on
2026-10-01:

- each gate is built in a *clean* world, where nothing is wrong, and a *worst*
  world, where everything it checks is wrong;
- for every move #032 allows, a gate in its worst world blocks exactly on the
  moves it guards (``GUARDS`` below) and passes every other move; in its clean
  world it passes every move;
- all gates run together through ``evaluate_gates`` for every allowed move, in
  both worlds, and a broken gate does not stop the others;
- every move #032 does not allow is refused before any gate is asked.

The table of which gate guards which move lives only here; wiring gates into
the flow is the pipeline runner (#206) and the publishing blocker (#084).
``GateName.TEST`` and ``GateName.QC`` have no gate yet (Phase K).
"""

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ai_youtube_agent.content.approval import ApprovalRequest, ApprovalStatus
from ai_youtube_agent.content.policy import PolicyCheck
from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.core.approval_gate import ApprovalGate
from ai_youtube_agent.core.artifact import Artifact, ArtifactKind
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.budget_gate import BudgetGate
from ai_youtube_agent.core.content_item import (
    ALLOWED_TRANSITIONS,
    ContentItem,
    ContentStatus,
    ContentTransitionError,
    ContentType,
)
from ai_youtube_agent.core.daily_limit_gate import DailyLimitGate
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.idempotency_gate import IdempotencyGate
from ai_youtube_agent.core.kill_switch_gate import KillSwitchGate
from ai_youtube_agent.core.policy_gate import PolicyGate
from ai_youtube_agent.core.rights_gate import RightsGate
from ai_youtube_agent.pipeline.idempotency import GENERATION_KIND
from ai_youtube_agent.pipeline.job import AIJob
from ai_youtube_agent.pipeline.kill_switch import EmergencyStop
from ai_youtube_agent.pipeline.publish import PublishJob
from factories import make_channel, make_cost_record, make_strategy_profile

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
USER = Actor(ActorKind.USER, "owner")
ITEM_ID = "item-1"

S = ContentStatus
MOVES = sorted(
    (
        (source, target)
        for source, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    ),
    key=lambda m: (list(S).index(m[0]), list(S).index(m[1])),
)
REFUSED = [
    (source, target) for source in S for target in S if (source, target) not in MOVES
]


def move_id(move: tuple[ContentStatus, ContentStatus]) -> str:
    return f"{move[0].value}->{move[1].value}"


def into(*targets: ContentStatus) -> Callable[[ContentStatus, ContentStatus], bool]:
    return lambda source, target: target in targets


def production_or_publish(source: ContentStatus, target: ContentStatus) -> bool:
    return (source, target) == (S.DRAFT, S.GENERATING) or target is S.PUBLISHING


# Which moves each gate guards, from the rules approved in C-034..C-041.
GUARDS: dict[GateName, Callable[[ContentStatus, ContentStatus], bool]] = {
    GateName.APPROVAL: into(S.PUBLISHING),
    GateName.DAILY_LIMIT: production_or_publish,
    GateName.BUDGET: into(S.GENERATING),
    GateName.RIGHTS: into(S.PUBLISHING),
    GateName.POLICY: into(S.PUBLISHING),
    GateName.KILL_SWITCH: into(S.GENERATING, S.PUBLISHING),
    GateName.IDEMPOTENCY: into(S.GENERATING, S.PUBLISHING),
}


def worst_codes(gate: GateName, target: ContentStatus) -> list[str]:
    """The reasons each gate gives in its worst world."""
    publishing = target is S.PUBLISHING
    return {
        GateName.APPROVAL: ["approval.missing"],
        GateName.DAILY_LIMIT: [
            "daily_limit.publish_reached"
            if publishing
            else "daily_limit.production_reached"
        ],
        GateName.BUDGET: ["budget.daily_exceeded", "budget.monthly_exceeded"],
        GateName.RIGHTS: ["rights.unresolved_high"],
        GateName.POLICY: ["policy.not_checked"],
        GateName.KILL_SWITCH: ["killswitch.active"],
        GateName.IDEMPOTENCY: [
            "idempotency.duplicate_publish"
            if publishing
            else "idempotency.duplicate_generation"
        ],
    }[gate]


# Shared data


def at(moment: datetime):
    return lambda: moment


CHANNEL = make_channel()
STRATEGY = make_strategy_profile(CHANNEL)
VIDEO = Artifact.create(
    ITEM_ID,
    ArtifactKind.VIDEO,
    uri="store://video/1",
    sha256="1" * 64,
    size_bytes=10,
    media_type="video/mp4",
    clock=at(T0),
)
APPROVED = dataclasses.replace(
    ApprovalRequest.create(ITEM_ID, [VIDEO], requested_by=SYSTEM, clock=at(T0)),
    status=ApprovalStatus.APPROVED,
)


def item_in(status: ContentStatus) -> ContentItem:
    item = ContentItem.create(
        CHANNEL.id, STRATEGY.id, 1, ContentType.SHORTS, "Video", clock=at(T0)
    )
    return dataclasses.replace(item, id=ITEM_ID, status=status)


def context(move: tuple[ContentStatus, ContentStatus]) -> GateContext:
    return GateContext(
        item=item_in(move[0]), target_status=move[1], actor=SYSTEM, at=T0
    )


# Stub sources: each returns a fixed answer, whatever it is asked.


class Fixed:
    def __init__(self, value) -> None:
        self.value = value

    def __call__(self, *args, **kwargs):
        return self.value


class Source:
    """A source whose named methods return fixed values."""

    def __init__(self, **methods) -> None:
        for name, value in methods.items():
            setattr(self, name, Fixed(value))


class AnyKeyJobs:
    """Holds a job under every key it is asked for."""

    def __init__(self, make: Callable[[str], object]) -> None:
        self.make = make

    def get_by_idempotency_key(self, key: str):
        return self.make(key)


def build(gate: GateName, worst: bool) -> PipelineGate:
    strategies = Source(get_by_channel=STRATEGY)
    if gate is GateName.APPROVAL:
        return ApprovalGate(
            Source(list_by_content_item=[] if worst else [APPROVED]),
            Source(list_by_content_item=[VIDEO]),
        )
    if gate is GateName.DAILY_LIMIT:
        used = 10**6 if worst else 0
        return DailyLimitGate(
            strategies,
            Source(count_production_starts=used, count_publishes=used),
        )
    if gate is GateName.BUDGET:
        spend = make_cost_record(CHANNEL, amount=Decimal("1000"), incurred_at=T0)
        return BudgetGate(strategies, Source(list_by_channel=[spend] if worst else []))
    if gate is GateName.RIGHTS:
        record = RightsRecord.create(
            ITEM_ID, "music-1", source="stock", clock=at(T0)
        ).with_risk_level(RiskLevel.HIGH if worst else RiskLevel.LOW, clock=at(T0))
        return RightsGate(Source(list_by_content_item=[record]))
    if gate is GateName.POLICY:
        checks = [] if worst else [PolicyCheck.create(ITEM_ID, clock=at(T0))]
        return PolicyGate(Source(list_by_content_item=checks))
    if gate is GateName.KILL_SWITCH:
        stop = (
            EmergencyStop.activated(USER, clock=at(T0))
            if worst
            else EmergencyStop.inactive()
        )
        return KillSwitchGate(Source(current=stop))
    if gate is GateName.IDEMPOTENCY:
        jobs = AnyKeyJobs(
            lambda key: (
                AIJob.create(GENERATION_KIND, key, clock=at(T0)).start(clock=at(T0))
                if worst
                else None
            )
        )
        publishes = AnyKeyJobs(
            lambda key: (
                PublishJob.create(APPROVED, ContentType.SHORTS, key, clock=at(T0))
                if worst
                else None
            )
        )
        return IdempotencyGate(jobs, publishes, Source(list_by_content_item=[APPROVED]))
    raise AssertionError(f"no gate for {gate}")


def all_gates(worst: bool) -> list[PipelineGate]:
    return [build(name, worst) for name in GUARDS]


# The matrix itself


def test_the_matrix_covers_every_concrete_gate_and_move() -> None:
    gates = all_gates(worst=True)

    assert [g.name for g in gates] == list(GUARDS)
    assert all(isinstance(g, PipelineGate) for g in gates)
    assert set(GateName) - set(GUARDS) == {GateName.TEST, GateName.QC}
    assert len(MOVES) == 21
    assert len(MOVES) + len(REFUSED) == len(S) ** 2
    # Every gate guards at least one move, and some moves no gate guards.
    for name, guards in GUARDS.items():
        assert any(guards(*m) for m in MOVES), name
    assert any(not any(g(*m) for g in GUARDS.values()) for m in MOVES)


@pytest.mark.parametrize("gate", list(GUARDS), ids=lambda g: g.value)
@pytest.mark.parametrize("move", MOVES, ids=move_id)
def test_worst_world_blocks_exactly_the_guarded_moves(
    gate: GateName, move: tuple[ContentStatus, ContentStatus]
) -> None:
    result = build(gate, worst=True).evaluate(context(move))

    assert result.gate is gate
    assert result.evaluated_at == T0
    if GUARDS[gate](*move):
        assert result.outcome is GateOutcome.BLOCK
        assert [r.code for r in result.reasons] == worst_codes(gate, move[1])
    else:
        assert result.outcome is GateOutcome.PASS


@pytest.mark.parametrize("gate", list(GUARDS), ids=lambda g: g.value)
@pytest.mark.parametrize("move", MOVES, ids=move_id)
def test_clean_world_passes_every_move(
    gate: GateName, move: tuple[ContentStatus, ContentStatus]
) -> None:
    result = build(gate, worst=False).evaluate(context(move))

    assert result.gate is gate
    assert result.outcome is GateOutcome.PASS


# All gates together


@pytest.mark.parametrize("move", MOVES, ids=move_id)
def test_all_gates_together_pass_in_the_clean_world(
    move: tuple[ContentStatus, ContentStatus],
) -> None:
    report = evaluate_gates(all_gates(worst=False), context(move))

    assert report.outcome is GateOutcome.PASS
    assert [r.gate for r in report.results] == list(GUARDS)
    assert report.reasons == ()


@pytest.mark.parametrize("move", MOVES, ids=move_id)
def test_all_gates_together_report_every_guarding_gate(
    move: tuple[ContentStatus, ContentStatus],
) -> None:
    report = evaluate_gates(all_gates(worst=True), context(move))

    guarding = [name for name, guards in GUARDS.items() if guards(*move)]
    blocked = [r.gate for r in report.results if not r.is_passed]
    assert [r.gate for r in report.results] == list(GUARDS)
    assert blocked == guarding
    assert [r.code for r in report.reasons] == [
        code for name in guarding for code in worst_codes(name, move[1])
    ]
    assert report.outcome is (GateOutcome.BLOCK if guarding else GateOutcome.PASS)


@pytest.mark.parametrize("broken", list(GUARDS), ids=lambda g: g.value)
def test_a_broken_gate_does_not_stop_the_others(broken: GateName) -> None:
    class Broken:
        name = broken

        def evaluate(self, context: GateContext):
            raise RuntimeError("source unavailable")

    gates = [Broken() if g.name is broken else g for g in all_gates(worst=False)]
    report = evaluate_gates(gates, context((S.APPROVED, S.PUBLISHING)))

    assert report.outcome is GateOutcome.BLOCK
    assert [r.gate for r in report.results] == list(GUARDS)
    assert [r.gate for r in report.results if not r.is_passed] == [broken]
    assert [r.code for r in report.reasons] == ["gate.error"]


# Moves #032 does not allow


@pytest.mark.parametrize("move", REFUSED, ids=move_id)
def test_a_refused_move_never_reaches_a_gate(
    move: tuple[ContentStatus, ContentStatus],
) -> None:
    asked: list[GateName] = []

    class Spy:
        def __init__(self, name: GateName) -> None:
            self.name = name

        def evaluate(self, context: GateContext):
            asked.append(self.name)
            raise AssertionError("a gate was asked about a refused move")

    expected = ValueError if move[0] is move[1] else ContentTransitionError
    with pytest.raises(expected):
        evaluate_gates([Spy(n) for n in GUARDS], context(move))
    assert asked == []
