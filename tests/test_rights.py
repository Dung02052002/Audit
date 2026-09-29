import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.rights import (
    RightsRecord,
    RightsResolutionNotAllowedError,
    RiskLevel,
    RiskResolution,
)
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import DomainError

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T1 + timedelta(minutes=5)
USER = Actor(ActorKind.USER, "owner-1")
AI = Actor(ActorKind.AI, "rights-agent")
SYSTEM = Actor(ActorKind.SYSTEM, "risk-engine")


def at(moment: datetime):
    return lambda: moment


def new_record(**overrides) -> RightsRecord:
    values = {"source": "  stock-music.example  ", "license": " CC-BY-4.0 "}
    return RightsRecord.create(
        "item-1", "asset-7", **{**values, **overrides}, clock=at(T0)
    )


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


def resolved_record() -> RightsRecord:
    return (
        new_record()
        .with_risk_level(RiskLevel.HIGH, clock=at(T1))
        .resolve(actor=USER, clock=at(T2))
    )


# Values


def test_risk_levels_and_resolutions() -> None:
    assert [r.value for r in RiskLevel] == ["unknown", "low", "medium", "high"]
    assert [r.value for r in RiskResolution] == ["unresolved", "resolved"]


# Creating a record


def test_create_starts_unknown_and_unresolved() -> None:
    record = new_record()

    assert len(record.id) == 32
    assert record.content_item_id == "item-1"
    assert record.asset_ref == "asset-7"
    assert record.source == "stock-music.example"
    assert record.license == "CC-BY-4.0"
    assert record.risk_level is RiskLevel.UNKNOWN
    assert record.resolution is RiskResolution.UNRESOLVED
    assert not record.is_resolved
    assert record.resolved_by is None
    assert record.resolved_at is None
    assert record.created_at == record.updated_at == T0


def test_license_is_optional() -> None:
    assert new_record(license=None).license is None


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    record = RightsRecord.create("item-1", "asset-7", source="generated")
    assert before <= record.created_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    ("item", "asset", "source", "license"),
    [
        ("", "a", "s", None),
        ("i", " ", "s", None),
        ("i", "a", "  ", None),
        ("i", "a", "s", "  "),
    ],
)
def test_create_rejects_missing_values(item, asset, source, license) -> None:
    with pytest.raises(ValueError):
        RightsRecord.create(item, asset, source=source, license=license)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"risk_level": "high"},
        {"resolution": "resolved"},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"updated_at": T0.astimezone(timezone(timedelta(hours=7)))},
        {"updated_at": T0 - timedelta(seconds=1)},
        {"resolved_by": USER},
        {"resolved_at": T0},
        {"resolution": RiskResolution.RESOLVED},
        {"resolution": RiskResolution.RESOLVED, "resolved_by": USER},
    ],
)
def test_record_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(new_record(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"resolved_by": AI},
        {"resolved_at": T0 - timedelta(seconds=1)},
        {"resolved_at": T2 + timedelta(seconds=1)},
        {"resolved_at": datetime(2026, 9, 29, 10, 10)},
    ],
)
def test_resolved_record_rejects_invalid_resolver(changes) -> None:
    with pytest.raises((ValueError, DomainError)):
        rebuild(resolved_record(), **changes)


def test_record_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_record().risk_level = RiskLevel.LOW  # type: ignore[misc]


# Risk level


@pytest.mark.parametrize("level", list(RiskLevel)[1:])
def test_with_risk_level_returns_a_new_record(level: RiskLevel) -> None:
    record = new_record()

    changed = record.with_risk_level(level, clock=at(T1))

    assert changed.risk_level is level
    assert changed.updated_at == T1
    assert (changed.id, changed.created_at) == (record.id, record.created_at)
    assert record.risk_level is RiskLevel.UNKNOWN


def test_same_risk_level_returns_the_same_record() -> None:
    record = new_record()
    assert record.with_risk_level(RiskLevel.UNKNOWN, clock=at(T1)) is record


def test_with_risk_level_rejects_unknown_values() -> None:
    with pytest.raises(TypeError):
        new_record().with_risk_level("critical", clock=at(T1))  # type: ignore[arg-type]


# Resolution


def test_a_user_resolves_a_risk() -> None:
    record = resolved_record()

    assert record.is_resolved
    assert record.resolution is RiskResolution.RESOLVED
    assert record.risk_level is RiskLevel.HIGH
    assert record.resolved_by == USER
    assert record.resolved_at == record.updated_at == T2


@pytest.mark.parametrize("actor", [AI, SYSTEM])
def test_ai_and_system_cannot_resolve_a_risk(actor: Actor) -> None:
    record = new_record().with_risk_level(RiskLevel.HIGH, clock=at(T1))
    with pytest.raises(RightsResolutionNotAllowedError):
        record.resolve(actor=actor, clock=at(T2))


def test_refusal_is_a_domain_error_with_a_safe_message() -> None:
    with pytest.raises(DomainError) as info:
        new_record().resolve(actor=AI)
    assert info.value.code == "domain.rights_resolution_not_allowed"
    assert info.value.user_message == "Only a user can resolve a rights risk."


def test_ai_is_refused_even_on_an_already_resolved_record() -> None:
    with pytest.raises(RightsResolutionNotAllowedError):
        resolved_record().resolve(actor=AI)


def test_resolving_twice_returns_the_same_record() -> None:
    record = resolved_record()
    assert record.resolve(actor=USER, clock=at(T2 + timedelta(hours=1))) is record


def test_system_may_set_the_risk_level() -> None:
    record = new_record().with_risk_level(RiskLevel.MEDIUM, clock=at(T1))
    assert record.risk_level is RiskLevel.MEDIUM


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.with_risk_level(RiskLevel.LOW, clock=at(T2)),
        lambda r: r.with_license("All rights reserved", clock=at(T2)),
        lambda r: r.with_license(None, clock=at(T2)),
    ],
)
def test_changing_level_or_license_makes_it_unresolved_again(change) -> None:
    record = change(resolved_record())

    assert not record.is_resolved
    assert record.resolved_by is None
    assert record.resolved_at is None
    assert record.updated_at == T2


def test_same_license_keeps_the_resolution() -> None:
    record = resolved_record()
    assert record.with_license("  CC-BY-4.0 ", clock=at(T2)) is record


def test_with_license_rejects_an_empty_license() -> None:
    with pytest.raises(ValueError):
        new_record().with_license("  ", clock=at(T1))


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    record = resolved_record()

    assert record.as_dict() == {
        "id": record.id,
        "content_item_id": "item-1",
        "asset_ref": "asset-7",
        "source": "stock-music.example",
        "license": "CC-BY-4.0",
        "risk_level": "high",
        "resolution": "resolved",
        "resolved_by": {"kind": "user", "id": "owner-1"},
        "resolved_at": "2026-09-29T10:10:00+00:00",
        "created_at": "2026-09-29T10:00:00+00:00",
        "updated_at": "2026-09-29T10:10:00+00:00",
    }
    assert new_record().as_dict()["resolved_by"] is None
    assert new_record().as_dict()["resolved_at"] is None
