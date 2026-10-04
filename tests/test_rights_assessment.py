"""G-078 Rights Risk Engine: the assessment entity and the pure rule table.

Rules the user approved on 2026-10-04:

- deterministic rules (no AI): one level and one stable rule code per outcome,
  from the registered asset category and the current provenance;
- an asset that is missing or in another channel, or whose category is unknown,
  is high risk;
- the outcome carries the current provenance id, so a new provenance gives a
  new assessment;
- an assessment is frozen, never ``unknown`` and holds no URL or text.
"""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.content.rights import RiskLevel
from ai_youtube_agent.content.rights_assessment import (
    RULES_VERSION,
    Outcome,
    RightsAssessment,
    classify,
)
from ai_youtube_agent.core.audit import Actor, ActorKind

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
AI = Actor(ActorKind.AI, "mock/mock-1")
CHANNEL = "channel-1"
SHA = "ab" * 32
SOURCE = "https://stock.example/canary-source"
LICENCE_URL = "https://licences.example/canary-licence"
CANARY = "canary-secret-text"


def at(moment: datetime = T0):
    return lambda: moment


def asset(category: AssetCategory, **overrides) -> Asset:
    arguments = {
        AssetCategory.GENERATED: {"source": "mock-image"},
        AssetCategory.LICENSED: {"source": "stock.example", "license_ref": "CC-BY"},
        AssetCategory.USER_OWNED: {"source": "user", "owner": "Lan"},
    }.get(category, {"source": "stock.example"}) | overrides
    return Asset.create(
        CHANNEL,
        AssetKind.IMAGE,
        category,
        title="Logo",
        clock=at(),
        **arguments,
    )


def provenance(target: Asset, **details) -> Provenance:
    return Provenance.create(
        target.id, recorded_by=USER, clock=at(T0 + timedelta(seconds=1)), **details
    )


GENERATED = AssetCategory.GENERATED
LICENSED = AssetCategory.LICENSED
PUBLIC_DOMAIN = AssetCategory.PUBLIC_DOMAIN
USER_OWNED = AssetCategory.USER_OWNED
UNKNOWN = AssetCategory.UNKNOWN
LOW, MEDIUM, HIGH = RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH

# (rule row, category, provenance details or None for no provenance, level, code)
RULES = [
    (3, LICENSED, None, HIGH, "licensed.no_provenance"),
    (
        5,
        LICENSED,
        {"license_name": "CC BY 4.0"},
        MEDIUM,
        "licensed.licence_without_evidence",
    ),
    (
        5,
        LICENSED,
        {"license_ref": "CC-BY-4.0", "attribution": "Photo by Lan"},
        MEDIUM,
        "licensed.licence_without_evidence",
    ),
    (
        6,
        LICENSED,
        {"license_name": "CC BY 4.0", "license_url": LICENCE_URL},
        LOW,
        "licensed.documented",
    ),
    (
        6,
        LICENSED,
        {"license_ref": "CC-BY-4.0", "proof": "invoice 7"},
        LOW,
        "licensed.documented",
    ),
    (7, PUBLIC_DOMAIN, None, MEDIUM, "public_domain.no_provenance"),
    (8, PUBLIC_DOMAIN, {"proof": "note"}, MEDIUM, "public_domain.no_source_url"),
    (
        8,
        PUBLIC_DOMAIN,
        {"license_url": LICENCE_URL},
        MEDIUM,
        "public_domain.no_source_url",
    ),
    (9, PUBLIC_DOMAIN, {"source_url": SOURCE}, MEDIUM, "public_domain.source_only"),
    (
        9,
        PUBLIC_DOMAIN,
        {"source_url": SOURCE, "attribution": "Archive"},
        MEDIUM,
        "public_domain.source_only",
    ),
    (
        10,
        PUBLIC_DOMAIN,
        {"source_url": SOURCE, "proof": "archive page"},
        LOW,
        "public_domain.documented",
    ),
    (
        10,
        PUBLIC_DOMAIN,
        {"source_url": SOURCE, "license_url": LICENCE_URL},
        LOW,
        "public_domain.documented",
    ),
    (11, USER_OWNED, None, MEDIUM, "user_owned.no_provenance"),
    (
        12,
        USER_OWNED,
        {"owner": "Lan", "proof": "invoice 7"},
        LOW,
        "user_owned.documented",
    ),
    (
        12,
        USER_OWNED,
        {"owner": "Lan", "file_sha256": SHA},
        LOW,
        "user_owned.documented",
    ),
    (13, USER_OWNED, {"owner": "Lan"}, MEDIUM, "user_owned.unproven"),
    (
        13,
        USER_OWNED,
        {"owner": "Lan", "source_url": SOURCE, "attribution": "Lan"},
        MEDIUM,
        "user_owned.unproven",
    ),
    (14, GENERATED, None, LOW, "generated.declared"),
    (14, GENERATED, {"proof": "render log"}, LOW, "generated.declared"),
    (2, UNKNOWN, None, HIGH, "asset.category_unknown"),
    (2, UNKNOWN, {"source_url": SOURCE, "proof": "x"}, HIGH, "asset.category_unknown"),
]


@pytest.mark.parametrize(("row", "category", "details", "level", "code"), RULES)
def test_every_rule_gives_its_level_and_code(row, category, details, level, code):
    target = asset(category)
    current = provenance(target, **details) if details else None

    outcome = classify(target, CHANNEL, current)

    assert outcome == Outcome(
        level, (code,), target.id, current.id if current else None
    ), row


def test_a_missing_asset_is_high_and_has_no_ids() -> None:
    assert classify(None, CHANNEL, None) == Outcome(
        HIGH, ("asset.not_registered",), None, None
    )


def test_an_asset_of_another_channel_is_high_and_has_no_ids() -> None:
    other = asset(GENERATED)

    outcome = classify(other, "channel-2", provenance(other, proof="x"))

    assert outcome == Outcome(HIGH, ("asset.not_registered",), None, None)


def test_a_licensed_provenance_without_a_licence_is_high() -> None:
    # The recorder refuses such a record; the rule stays defensive (row 4).
    target = asset(LICENSED)
    current = provenance(target, proof="invoice 7")

    outcome = classify(target, CHANNEL, current)

    assert (outcome.level, outcome.rule_codes) == (HIGH, ("licensed.no_licence",))
    assert outcome.provenance_id == current.id


def test_the_rules_ignore_the_asset_fields_the_provenance_does_not_repeat() -> None:
    # The asset holds a licence reference, but the provenance record decides.
    target = asset(LICENSED, license_ref="CC-BY-4.0")

    assert classify(target, CHANNEL, None).level is HIGH


def test_the_outcome_names_the_current_provenance() -> None:
    target = asset(PUBLIC_DOMAIN)
    first = provenance(target, source_url=SOURCE)
    second = provenance(target, source_url=SOURCE, proof="archive page")

    assert classify(target, CHANNEL, first).provenance_id == first.id
    assert classify(target, CHANNEL, second).provenance_id == second.id
    assert classify(target, CHANNEL, first).level is MEDIUM
    assert classify(target, CHANNEL, second).level is LOW


def test_classify_is_deterministic() -> None:
    target = asset(LICENSED)
    current = provenance(target, license_name="CC BY", proof="x")

    assert classify(target, CHANNEL, current) == classify(target, CHANNEL, current)


def test_no_outcome_is_unknown_and_each_has_one_code() -> None:
    for _, category, details, _, _ in RULES:
        target = asset(category)
        current = provenance(target, **details) if details else None
        outcome = classify(target, CHANNEL, current)

        assert outcome.level is not RiskLevel.UNKNOWN
        assert len(outcome.rule_codes) == 1


def test_no_outcome_holds_a_url_or_a_text() -> None:
    target = asset(PUBLIC_DOMAIN)
    current = provenance(
        target, source_url=SOURCE, license_url=LICENCE_URL, proof=CANARY
    )

    outcome = classify(target, CHANNEL, current)

    assert not any(
        fragment in repr(outcome) for fragment in (SOURCE, LICENCE_URL, CANARY)
    )


# the entity


def outcome_of(**overrides) -> Outcome:
    values = {
        "level": MEDIUM,
        "rule_codes": ("public_domain.source_only",),
        "asset_id": "asset-1",
        "provenance_id": "prov-1",
    } | overrides
    return Outcome(**values)


def new_assessment(outcome: Outcome | None = None, **overrides) -> RightsAssessment:
    arguments = {"assessed_by": SYSTEM, "clock": at()} | overrides
    return RightsAssessment.create("rr-1", "ci-1", outcome or outcome_of(), **arguments)


def fields(**overrides) -> dict:
    values = {
        "id": "ra-1",
        "rights_record_id": "rr-1",
        "content_item_id": "ci-1",
        "asset_id": "asset-1",
        "level": MEDIUM,
        "rule_codes": ("public_domain.source_only",),
        "rules_version": RULES_VERSION,
        "provenance_id": "prov-1",
        "assessed_by": SYSTEM,
        "created_at": T0,
    }
    return values | overrides


def test_create_copies_the_outcome_and_reads_the_injected_clock() -> None:
    made = new_assessment(clock=at(T0 + timedelta(minutes=5)))

    assert made.rights_record_id == "rr-1"
    assert made.content_item_id == "ci-1"
    assert made.asset_id == "asset-1"
    assert made.provenance_id == "prov-1"
    assert made.level is MEDIUM
    assert made.rule_codes == ("public_domain.source_only",)
    assert made.rules_version == RULES_VERSION == "rights-rules-v1"
    assert made.assessed_by == SYSTEM
    assert made.created_at == T0 + timedelta(minutes=5)
    assert made.id and new_assessment().id != made.id


def test_create_without_a_clock_uses_utc_now() -> None:
    made = RightsAssessment.create("rr-1", "ci-1", outcome_of(), assessed_by=USER)

    assert made.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("actor", [USER, SYSTEM, AI])
def test_any_actor_may_assess(actor: Actor) -> None:
    assert new_assessment(assessed_by=actor).assessed_by == actor


def test_an_assessment_is_frozen() -> None:
    made = new_assessment()

    with pytest.raises(dataclasses.FrozenInstanceError):
        made.level = HIGH  # type: ignore[misc]


def test_a_valid_assessment_without_asset_or_provenance() -> None:
    made = new_assessment(
        outcome_of(
            level=HIGH,
            rule_codes=("asset.not_registered",),
            asset_id=None,
            provenance_id=None,
        )
    )

    assert (made.asset_id, made.provenance_id) == (None, None)


def test_an_asset_without_provenance_is_valid() -> None:
    assert new_assessment(outcome_of(provenance_id=None)).provenance_id is None


def test_the_level_unknown_is_refused() -> None:
    with pytest.raises(ValueError, match="other than unknown"):
        RightsAssessment(**fields(level=RiskLevel.UNKNOWN))


@pytest.mark.parametrize("level", ["high", None, 3])
def test_a_level_must_be_a_risk_level(level) -> None:
    with pytest.raises(TypeError, match="RiskLevel"):
        RightsAssessment(**fields(level=level))


@pytest.mark.parametrize("name", ["id", "rights_record_id", "content_item_id"])
@pytest.mark.parametrize("value", ["", "   ", None])
def test_required_ids_must_not_be_empty(name: str, value) -> None:
    with pytest.raises(ValueError, match=name):
        RightsAssessment(**fields(**{name: value}))


@pytest.mark.parametrize("name", ["asset_id", "provenance_id"])
@pytest.mark.parametrize("value", ["", "  "])
def test_optional_ids_must_not_be_empty(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        RightsAssessment(**fields(**{name: value}))


@pytest.mark.parametrize("value", ["", "  ", None])
def test_the_rules_version_must_not_be_empty(value) -> None:
    with pytest.raises(ValueError, match="rules_version"):
        RightsAssessment(**fields(rules_version=value))


@pytest.mark.parametrize(
    "codes",
    [(), [], None, ("",), (" ",), (" a.b",), ("a.b ",), ("a.b", ""), ("a.b", 3)],
)
def test_the_codes_need_at_least_one_clean_code(codes) -> None:
    with pytest.raises(ValueError, match="code"):
        RightsAssessment(**fields(rule_codes=codes))


def test_a_provenance_needs_an_asset() -> None:
    with pytest.raises(ValueError, match="provenance_id needs an asset_id"):
        RightsAssessment(**fields(asset_id=None, provenance_id="prov-1"))


@pytest.mark.parametrize("actor", [None, "user", ("user", "owner")])
def test_the_assessor_must_be_an_actor(actor) -> None:
    with pytest.raises(TypeError, match="Actor"):
        RightsAssessment(**fields(assessed_by=actor))


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 4, 12, 0),
        datetime(2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
    ids=["naive", "not-utc"],
)
def test_the_time_must_be_utc(moment: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        RightsAssessment(**fields(created_at=moment))


def test_the_time_must_be_a_datetime() -> None:
    with pytest.raises(TypeError, match="datetime"):
        RightsAssessment(**fields(created_at="2026-10-04"))


def test_create_refuses_a_non_utc_clock() -> None:
    naive = datetime(2026, 10, 4, 12, 0)

    with pytest.raises(ValueError, match="UTC"):
        new_assessment(clock=at(naive))


def test_the_outcome_key_is_level_codes_provenance_and_version() -> None:
    made = new_assessment()

    assert made.outcome_key() == (
        MEDIUM,
        ("public_domain.source_only",),
        "prov-1",
        RULES_VERSION,
    )


def test_the_outcome_key_ignores_id_actor_time_and_asset() -> None:
    first = new_assessment(assessed_by=USER, clock=at(T0))
    later = new_assessment(assessed_by=AI, clock=at(T0 + timedelta(days=1)))

    assert first.id != later.id
    assert first.outcome_key() == later.outcome_key()


@pytest.mark.parametrize(
    "overrides",
    [
        {"level": HIGH},
        {"rule_codes": ("public_domain.no_provenance",)},
        {"rule_codes": ("public_domain.source_only", "extra.code")},
        {"provenance_id": "prov-2"},
        {"provenance_id": None},
        {"rules_version": "rights-rules-v2"},
    ],
)
def test_a_different_outcome_has_a_different_key(overrides: dict) -> None:
    base = RightsAssessment(**fields())

    assert RightsAssessment(**fields(**overrides)).outcome_key() != base.outcome_key()


def test_as_dict_is_plain_data() -> None:
    made = RightsAssessment(**fields(assessed_by=AI))

    assert made.as_dict() == {
        "id": "ra-1",
        "rights_record_id": "rr-1",
        "content_item_id": "ci-1",
        "asset_id": "asset-1",
        "level": "medium",
        "rule_codes": ["public_domain.source_only"],
        "rules_version": "rights-rules-v1",
        "provenance_id": "prov-1",
        "assessed_by": {"kind": "ai", "id": "mock/mock-1"},
        "created_at": T0.isoformat(),
    }


def test_an_assessment_built_from_a_classification_holds_no_url_or_text() -> None:
    target = asset(PUBLIC_DOMAIN)
    current = provenance(
        target, source_url=SOURCE, license_url=LICENCE_URL, proof=CANARY
    )

    made = RightsAssessment.create(
        "rr-1", "ci-1", classify(target, CHANNEL, current), assessed_by=USER
    )

    text = repr(made) + repr(made.as_dict())
    assert not any(fragment in text for fragment in (SOURCE, LICENCE_URL, CANARY))
