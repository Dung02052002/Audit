"""C-038 Rights Gate (Prompt Pack v8, prompt #038).

Rules the user approved on 2026-10-01:

- an unresolved record at level ``high`` blocks the publish;
- ``unknown`` counts as high, so an unresolved unknown record blocks too;
- unresolved ``low`` and ``medium`` pass, and a resolved record passes at any
  level;
- an item with no rights records passes;
- each blocking record gives its own reason with its asset ref;
- the gate only blocks the move to ``publishing``.

G-078 (2026-10-04) makes the blocking levels configurable: ``high`` and
``unknown`` always block and ``medium`` may be added.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ai_youtube_agent.content.rights import RightsRecord, RiskLevel
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.config import RightsBlockLevel, Settings
from ai_youtube_agent.core.content_item import ContentItem, ContentStatus, ContentType
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.gates import (
    GateContext,
    GateName,
    GateOutcome,
    PipelineGate,
    evaluate_gates,
)
from ai_youtube_agent.core.rights_gate import (
    BLOCKING_LEVELS,
    RightsGate,
    blocking_levels_for,
)
from factories import make_channel, make_strategy_profile

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
USER = Actor(ActorKind.USER, "owner")


class Records:
    """In-memory stand-in for ``RightsRecordRepository``."""

    def __init__(self) -> None:
        self.records: list[RightsRecord] = []

    def list_by_content_item(self, content_item_id: str) -> list[RightsRecord]:
        return [r for r in self.records if r.content_item_id == content_item_id]


def at(moment: datetime):
    return lambda: moment


def new_item(status: ContentStatus = ContentStatus.APPROVED) -> ContentItem:
    item = ContentItem.create(
        "channel-1", "strategy-1", 1, ContentType.SHORTS, "Video", clock=at(T0)
    )
    return dataclasses.replace(item, status=status)


def record(
    item: ContentItem,
    asset_ref: str,
    level: RiskLevel = RiskLevel.UNKNOWN,
    *,
    resolved: bool = False,
    minutes: int = 0,
) -> RightsRecord:
    clock = at(T0 + timedelta(minutes=minutes))
    made = RightsRecord.create(
        item.id, asset_ref, source="stock-provider", clock=clock
    ).with_risk_level(level, clock=clock)
    return made.resolve(actor=USER, clock=clock) if resolved else made


@pytest.fixture
def records() -> Records:
    return Records()


@pytest.fixture
def gate(records: Records) -> RightsGate:
    return RightsGate(records)


def context(item: ContentItem, target=ContentStatus.PUBLISHING) -> GateContext:
    return GateContext(item=item, target_status=target, actor=SYSTEM, at=T0)


def codes(result) -> list[str]:
    return [r.code for r in result.reasons]


# Contract


def test_the_gate_follows_the_contract(gate: RightsGate) -> None:
    assert isinstance(gate, PipelineGate)
    assert gate.name is GateName.RIGHTS


# Passing


def test_passes_without_any_rights_record(gate: RightsGate) -> None:
    result = gate.evaluate(context(new_item()))

    assert result.outcome is GateOutcome.PASS
    assert result.gate is GateName.RIGHTS
    assert result.evaluated_at == T0


@pytest.mark.parametrize("level", [RiskLevel.LOW, RiskLevel.MEDIUM])
def test_unresolved_low_and_medium_pass(
    records: Records, gate: RightsGate, level: RiskLevel
) -> None:
    item = new_item()
    records.records.append(record(item, "music-1", level))

    assert gate.evaluate(context(item)).is_passed


@pytest.mark.parametrize("level", list(RiskLevel))
def test_a_resolved_record_passes_at_any_level(
    records: Records, gate: RightsGate, level: RiskLevel
) -> None:
    item = new_item()
    records.records.append(record(item, "music-1", level, resolved=True))

    assert gate.evaluate(context(item)).is_passed


@pytest.mark.parametrize(
    "target",
    [ContentStatus.GENERATING, ContentStatus.AWAITING_APPROVAL, ContentStatus.FAILED],
)
def test_other_moves_are_not_this_gates_concern(
    records: Records, gate: RightsGate, target: ContentStatus
) -> None:
    item = new_item(ContentStatus.PREVIEW_READY)
    records.records.append(record(item, "music-1", RiskLevel.HIGH))

    assert gate.evaluate(context(item, target)).is_passed


def test_other_items_records_are_ignored(records: Records, gate: RightsGate) -> None:
    item, other = new_item(), new_item()
    records.records.append(record(other, "music-1", RiskLevel.HIGH))

    assert gate.evaluate(context(item)).is_passed


# Blocking


def test_blocks_on_unresolved_high(records: Records, gate: RightsGate) -> None:
    item = new_item()
    records.records.append(record(item, "music-1", RiskLevel.HIGH))

    result = gate.evaluate(context(item))

    assert result.outcome is GateOutcome.BLOCK
    assert codes(result) == ["rights.unresolved_high"]
    assert "music-1" in result.reasons[0].message


def test_unknown_counts_as_high(records: Records, gate: RightsGate) -> None:
    item = new_item()
    records.records.append(record(item, "clip-7"))  # a new record is unknown

    result = gate.evaluate(context(item))

    assert codes(result) == ["rights.unresolved_unknown"]
    assert "clip-7" in result.reasons[0].message


def test_reports_every_blocking_record_in_record_order(
    records: Records, gate: RightsGate
) -> None:
    item = new_item()
    records.records += [
        record(item, "late-high", RiskLevel.HIGH, minutes=5),
        record(item, "safe", RiskLevel.LOW, minutes=1),
        record(item, "done", RiskLevel.HIGH, resolved=True, minutes=2),
        record(item, "early-unknown", minutes=0),
    ]

    result = gate.evaluate(context(item))

    assert codes(result) == ["rights.unresolved_unknown", "rights.unresolved_high"]
    assert "early-unknown" in result.reasons[0].message
    assert "late-high" in result.reasons[1].message


def test_raising_the_level_after_resolution_blocks_again(
    records: Records, gate: RightsGate
) -> None:
    item = new_item()
    resolved = record(item, "music-1", RiskLevel.MEDIUM, resolved=True)
    records.records.append(resolved.with_risk_level(RiskLevel.HIGH, clock=at(T0)))

    assert codes(gate.evaluate(context(item))) == ["rights.unresolved_high"]


# Through evaluate_gates and SQLite


def test_a_failing_source_blocks_through_evaluate_gates() -> None:
    class Broken:
        def list_by_content_item(self, content_item_id: str):
            raise RuntimeError("database is locked")

    report = evaluate_gates([RightsGate(Broken())], context(new_item()))

    assert report.outcome is GateOutcome.BLOCK
    assert [r.code for r in report.reasons] == ["gate.error"]


def test_the_real_repository_satisfies_the_gate(database: Database) -> None:
    channel = make_channel()
    strategy = make_strategy_profile(channel)
    item = dataclasses.replace(
        ContentItem.create(
            channel.id, strategy.id, strategy.version, ContentType.SHORTS, "Video"
        ),
        status=ContentStatus.APPROVED,
    )
    high = record(item, "music-1", RiskLevel.HIGH)
    with database.transaction() as connection:
        ChannelRepository(connection).add(channel)
        StrategyProfileRepository(connection).add(strategy)
        ContentItemRepository(connection).add(item)
        RightsRecordRepository(connection).add(high)

    with database.transaction() as connection:
        repository = RightsRecordRepository(connection)
        gate = RightsGate(repository)
        blocked = gate.evaluate(context(item))
        repository.update(
            high.resolve(actor=USER, clock=at(T0 + timedelta(minutes=1))),
            expected_updated_at=high.updated_at,
        )
        passed = gate.evaluate(context(item))

    assert codes(blocked) == ["rights.unresolved_high"]
    assert passed.is_passed


# Configurable blocking levels (G-078)

CONFIGURED = blocking_levels_for({RightsBlockLevel.MEDIUM, RightsBlockLevel.HIGH})


def test_the_default_blocking_levels_are_unchanged() -> None:
    assert {RiskLevel.HIGH, RiskLevel.UNKNOWN} == BLOCKING_LEVELS
    assert RightsGate(Records())._blocking_levels == BLOCKING_LEVELS


def test_a_default_gate_and_an_explicit_default_behave_the_same(
    records: Records,
) -> None:
    item = new_item()
    records.records += [
        record(item, f"asset-{level.value}", level, minutes=index)
        for index, level in enumerate(RiskLevel)
    ]

    default = RightsGate(records).evaluate(context(item))
    explicit = RightsGate(records, blocking_levels=BLOCKING_LEVELS).evaluate(
        context(item)
    )

    assert codes(default) == codes(explicit)
    assert codes(default) == ["rights.unresolved_unknown", "rights.unresolved_high"]


def test_a_configured_gate_blocks_unresolved_medium(records: Records) -> None:
    item = new_item()
    records.records.append(record(item, "photo-3", RiskLevel.MEDIUM))

    result = RightsGate(records, blocking_levels=CONFIGURED).evaluate(context(item))

    assert result.outcome is GateOutcome.BLOCK
    assert codes(result) == ["rights.unresolved_medium"]
    assert result.reasons[0].message == (
        "Asset photo-3 has a medium rights risk that is not resolved."
    )


def test_a_configured_gate_still_reports_high_and_unknown_as_before(
    records: Records,
) -> None:
    item = new_item()
    records.records += [
        record(item, "a-unknown", minutes=0),
        record(item, "b-medium", RiskLevel.MEDIUM, minutes=1),
        record(item, "c-high", RiskLevel.HIGH, minutes=2),
    ]

    result = RightsGate(records, blocking_levels=CONFIGURED).evaluate(context(item))

    assert codes(result) == [
        "rights.unresolved_unknown",
        "rights.unresolved_medium",
        "rights.unresolved_high",
    ]
    assert result.reasons[2].message == (
        "Asset c-high has a high rights risk that is not resolved."
    )
    assert result.reasons[0].message == (
        "Asset a-unknown has an unknown rights risk that is not resolved."
    )


@pytest.mark.parametrize("levels", [CONFIGURED, BLOCKING_LEVELS])
def test_low_never_blocks(records: Records, levels: frozenset) -> None:
    item = new_item()
    records.records.append(record(item, "photo-3", RiskLevel.LOW))

    assert RightsGate(records, blocking_levels=levels).evaluate(context(item)).is_passed


@pytest.mark.parametrize("level", list(RiskLevel))
def test_a_resolved_record_passes_a_configured_gate_at_any_level(
    records: Records, level: RiskLevel
) -> None:
    item = new_item()
    records.records.append(record(item, "photo-3", level, resolved=True))

    gate = RightsGate(records, blocking_levels=CONFIGURED)

    assert gate.evaluate(context(item)).is_passed


def test_a_configured_gate_only_judges_the_move_to_publishing(
    records: Records,
) -> None:
    waiting = new_item(ContentStatus.PREVIEW_READY)
    approved = new_item()
    records.records += [
        record(waiting, "photo-3", RiskLevel.MEDIUM),
        record(approved, "photo-4", RiskLevel.MEDIUM),
    ]
    gate = RightsGate(records, blocking_levels=CONFIGURED)

    other_move = context(waiting, ContentStatus.AWAITING_APPROVAL)
    assert gate.evaluate(other_move).is_passed
    assert not gate.evaluate(context(approved)).is_passed


def test_the_levels_may_be_any_collection(records: Records) -> None:
    item = new_item()
    records.records.append(record(item, "photo-3", RiskLevel.MEDIUM))

    for levels in (
        [RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.UNKNOWN],
        (RiskLevel.HIGH, RiskLevel.UNKNOWN, RiskLevel.MEDIUM),
        {RiskLevel.HIGH, RiskLevel.UNKNOWN, RiskLevel.MEDIUM},
    ):
        gate = RightsGate(records, blocking_levels=levels)
        assert codes(gate.evaluate(context(item))) == ["rights.unresolved_medium"]


@pytest.mark.parametrize(
    "levels",
    [
        set(),
        {RiskLevel.HIGH},
        {RiskLevel.UNKNOWN},
        {RiskLevel.MEDIUM},
        {RiskLevel.MEDIUM, RiskLevel.HIGH},
        {RiskLevel.MEDIUM, RiskLevel.UNKNOWN},
    ],
)
def test_levels_must_hold_high_and_unknown(levels: set) -> None:
    with pytest.raises(ValueError, match="high and unknown"):
        RightsGate(Records(), blocking_levels=levels)


@pytest.mark.parametrize(
    "levels",
    [
        {RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.UNKNOWN},
        set(RiskLevel),
        {RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.UNKNOWN},
    ],
)
def test_levels_may_not_hold_low(levels: set) -> None:
    with pytest.raises(ValueError, match="medium, high and unknown"):
        RightsGate(Records(), blocking_levels=levels)


def test_blocking_levels_for_the_default_setting() -> None:
    assert blocking_levels_for({RightsBlockLevel.HIGH}) == BLOCKING_LEVELS
    assert blocking_levels_for(Settings().rights_block_levels) == BLOCKING_LEVELS


def test_blocking_levels_for_medium_and_high() -> None:
    assert blocking_levels_for({RightsBlockLevel.MEDIUM, RightsBlockLevel.HIGH}) == {
        RiskLevel.MEDIUM,
        RiskLevel.HIGH,
        RiskLevel.UNKNOWN,
    }


def test_blocking_levels_for_always_adds_unknown_and_returns_a_frozenset() -> None:
    levels = blocking_levels_for([RightsBlockLevel.HIGH])

    assert isinstance(levels, frozenset)
    assert RiskLevel.UNKNOWN in levels
    assert RiskLevel.LOW not in levels


def test_blocking_levels_for_builds_a_valid_gate(records: Records) -> None:
    for configured in (
        {RightsBlockLevel.HIGH},
        {RightsBlockLevel.MEDIUM, RightsBlockLevel.HIGH},
    ):
        RightsGate(records, blocking_levels=blocking_levels_for(configured))
