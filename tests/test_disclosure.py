"""Tests for the AI disclosure entity (G-081)."""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.disclosure import (
    FACT_NAMES,
    DisclosureDecision,
    DisclosureEvaluation,
    DisclosureRecord,
    DisclosureSources,
    RationaleEntry,
)
from ai_youtube_agent.content.policy_rule import RuleSet
from ai_youtube_agent.core.audit import Actor, ActorKind

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
RULES = RuleSet("disclosure", 1)


def entry(triggered: bool = False, **overrides) -> RationaleEntry:
    fields = {
        "rule_id": "disclosure.realistic_person",
        "version": 1,
        "code": "disclosure.realistic_person." + ("required" if triggered else "ok"),
        "message": "A static message.",
        "triggered": triggered,
    }
    return RationaleEntry(**(fields | overrides))


def sources(**overrides) -> DisclosureSources:
    fields = {"facts": ("realistic_person",), "asset_ids": ("a1", "b2")}
    return DisclosureSources(**(fields | overrides))


def evaluation(
    decision=DisclosureDecision.REQUIRED, rationale=None, **overrides
) -> DisclosureEvaluation:
    if rationale is None:
        rationale = (entry(True), entry(False, rule_id="disclosure.other"))
    fields = {
        "rule_set": RULES,
        "decision": decision,
        "rationale": rationale,
        "sources": sources(),
    }
    return DisclosureEvaluation(**(fields | overrides))


def record(**overrides) -> DisclosureRecord:
    fields = {
        "id": "d1",
        "content_item_id": "item-1",
        "channel_id": "channel-1",
        "rule_set_id": "disclosure",
        "rule_set_version": "disclosure-rules-v1",
        "decision": DisclosureDecision.REQUIRED,
        "rationale": (entry(True), entry(False, rule_id="disclosure.other")),
        "sources": sources(),
        "decided_by": USER,
        "created_at": T0,
    }
    return DisclosureRecord(**(fields | overrides))


def test_the_decision_values_and_fact_names() -> None:
    assert [d.value for d in DisclosureDecision] == ["required", "not_required"]
    assert FACT_NAMES == (
        "realistic_person",
        "realistic_event",
        "synthetic_voice_of_real_person",
        "realistic_visual",
    )


# rationale entries


def test_a_rationale_entry_as_dict_has_exactly_the_five_keys() -> None:
    assert entry(True).as_dict() == {
        "rule_id": "disclosure.realistic_person",
        "version": 1,
        "code": "disclosure.realistic_person.required",
        "message": "A static message.",
        "triggered": True,
    }


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"rule_id": ""}, ValueError),
        ({"rule_id": "  "}, ValueError),
        ({"code": ""}, ValueError),
        ({"code": " x"}, ValueError),
        ({"message": ""}, ValueError),
        ({"version": 0}, ValueError),
        ({"version": True}, ValueError),
        ({"version": "1"}, ValueError),
        ({"triggered": 1}, TypeError),
        ({"triggered": None}, TypeError),
    ],
)
def test_a_bad_rationale_entry_is_refused(overrides, error) -> None:
    with pytest.raises(error):
        entry(**overrides)


# sources


def test_sources_as_dict_is_sorted_lists() -> None:
    assert sources().as_dict() == {
        "asset_ids": ["a1", "b2"],
        "facts": ["realistic_person"],
    }


def test_empty_sources_are_allowed() -> None:
    assert DisclosureSources((), ()).as_dict() == {"asset_ids": [], "facts": []}


def test_sources_accept_every_fact_in_canonical_order() -> None:
    assert DisclosureSources(FACT_NAMES, ()).facts == FACT_NAMES


@pytest.mark.parametrize(
    "facts",
    [
        ("unknown_fact",),
        ("realistic_event", "realistic_person"),  # not canonical
        ("realistic_person", "realistic_person"),  # duplicate
        ["realistic_person"],  # not a tuple
        ("realistic_person", 1),
        (None,),
    ],
)
def test_sources_refuse_bad_facts(facts) -> None:
    with pytest.raises(ValueError, match="facts"):
        DisclosureSources(facts, ())


@pytest.mark.parametrize(
    "asset_ids",
    [
        ("has space",),
        ("https://evil.example/x",),
        ("a" * 101,),
        ("",),
        ("a/b",),
        ("b", "a"),  # not sorted
        ("a", "a"),  # duplicate
        ["a"],  # not a tuple
        (1,),
    ],
)
def test_sources_refuse_bad_asset_ids(asset_ids) -> None:
    with pytest.raises(ValueError, match="asset_ids"):
        DisclosureSources((), asset_ids)


def test_sources_accept_safe_ids_at_the_limit() -> None:
    safe = DisclosureSources((), ("a" * 100, "b.c:d-e"))

    assert len(safe.asset_ids) == 2


# evaluation


def test_an_evaluation_is_consistent() -> None:
    assert evaluation().decision is DisclosureDecision.REQUIRED
    assert (
        evaluation(DisclosureDecision.NOT_REQUIRED, (entry(False),)).decision
        is DisclosureDecision.NOT_REQUIRED
    )


def test_required_with_no_triggered_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="required"):
        evaluation(DisclosureDecision.REQUIRED, (entry(False),))


def test_not_required_with_a_triggered_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="not_required"):
        evaluation(DisclosureDecision.NOT_REQUIRED, (entry(True),))


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"rule_set": ("disclosure", 1)}, TypeError),
        ({"decision": "required"}, TypeError),
        ({"rationale": ()}, ValueError),
        ({"rationale": [entry(True)]}, ValueError),
        ({"rationale": ("x",)}, ValueError),
        ({"sources": {"facts": []}}, TypeError),
    ],
)
def test_a_bad_evaluation_is_refused(overrides, error) -> None:
    with pytest.raises(error):
        evaluation(**overrides)


# record


def test_a_valid_record_and_as_dict() -> None:
    created = record(decided_by=SYSTEM)

    assert created.as_dict() == {
        "id": "d1",
        "content_item_id": "item-1",
        "channel_id": "channel-1",
        "rule_set_id": "disclosure",
        "rule_set_version": "disclosure-rules-v1",
        "decision": "required",
        "rationale": [
            entry(True).as_dict(),
            entry(False, rule_id="disclosure.other").as_dict(),
        ],
        "sources": {"asset_ids": ["a1", "b2"], "facts": ["realistic_person"]},
        "decided_by": {"kind": "system", "id": "pipeline"},
        "created_at": T0.isoformat(),
    }


def test_create_builds_a_record_from_an_evaluation() -> None:
    created = DisclosureRecord.create(
        "item-1", "channel-1", evaluation(), decided_by=USER, clock=lambda: T0
    )

    assert created.rule_set_id == "disclosure"
    assert created.rule_set_version == "disclosure-rules-v1"
    assert created.decision is DisclosureDecision.REQUIRED
    assert created.created_at == T0
    assert len(created.id) == 32
    assert created.rationale == evaluation().rationale
    assert created.sources == evaluation().sources


def test_create_reads_the_system_clock_in_utc_by_default() -> None:
    created = DisclosureRecord.create(
        "item-1", "channel-1", evaluation(), decided_by=USER
    )

    assert created.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"rule_set_version": "disclosure-v1"},
        {"rule_set_version": "other-rules-v1"},
        {"rule_set_version": "disclosure-rules-v"},
        {"rule_set_version": "disclosure-rules-v0"},
        {"rule_set_version": "disclosure-rules-v01"},
        {"rule_set_version": "disclosure-rules-v1x"},
        {"rule_set_version": "disclosure-rules-v-1"},
        {"rule_set_id": "other"},
        {"rule_set_id": "r" * 51, "rule_set_version": "r" * 51 + "-rules-v1"},
        {"id": ""},
        {"content_item_id": " "},
        {"channel_id": ""},
        {"rule_set_id": "", "rule_set_version": "-rules-v1"},
    ],
)
def test_a_bad_record_is_refused(overrides) -> None:
    with pytest.raises(ValueError):
        record(**overrides)


def test_a_stored_version_at_the_limit_is_accepted() -> None:
    rule_set_id = "r" * 38
    created = record(
        rule_set_id=rule_set_id, rule_set_version=f"{rule_set_id}-rules-v123"
    )

    assert len(created.rule_set_version) == 49


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"decision": "required"}, TypeError),
        ({"rationale": ()}, ValueError),
        ({"rationale": [entry(True)]}, ValueError),
        ({"sources": "x"}, TypeError),
        ({"decided_by": "owner"}, TypeError),
        ({"created_at": "2026-10-04"}, TypeError),
    ],
)
def test_a_record_with_a_wrong_type_is_refused(overrides, error) -> None:
    with pytest.raises(error):
        record(**overrides)


def test_a_record_decision_must_match_its_rationale() -> None:
    with pytest.raises(ValueError):
        record(decision=DisclosureDecision.NOT_REQUIRED)
    with pytest.raises(ValueError):
        record(decision=DisclosureDecision.REQUIRED, rationale=(entry(False),))


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 4, 12, 0),
        datetime(2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
)
def test_a_record_time_must_be_utc(moment) -> None:
    with pytest.raises(ValueError, match="created_at"):
        record(created_at=moment)


def test_a_record_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        record().decision = DisclosureDecision.NOT_REQUIRED  # type: ignore[misc]


# content key


def test_the_content_key_ignores_id_actor_channel_and_time() -> None:
    one = record()
    other = record(
        id="d2",
        decided_by=SYSTEM,
        channel_id="channel-2",
        content_item_id="item-2",
        created_at=T0 + timedelta(days=1),
    )

    assert one.content_key() == other.content_key()


def test_the_content_key_holds_version_decision_rationale_and_sources() -> None:
    base = record()
    changed = {
        "version": record(rule_set_version="disclosure-rules-v2"),
        "decision": record(
            decision=DisclosureDecision.NOT_REQUIRED, rationale=(entry(False),)
        ),
        "rationale": record(rationale=(entry(True, message="Another."),)),
        "sources facts": record(sources=sources(facts=())),
        "sources assets": record(sources=sources(asset_ids=("a1",))),
    }

    for name, other in changed.items():
        assert other.content_key() != base.content_key(), name
    assert base.content_key() == (
        base.rule_set_version,
        base.decision,
        base.rationale,
        base.sources,
    )
