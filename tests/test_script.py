import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.script import Claim, Evidence, Script

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
TEXT = "Most people never check their bank fees. Here is why that matters."


def at(moment: datetime):
    return lambda: moment


def new_script() -> Script:
    return Script.create("item-1", f"  {TEXT}\n", clock=at(T0))


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


# Script versions


def test_create_builds_version_1() -> None:
    script = new_script()

    assert len(script.id) == 32
    assert script.content_item_id == "item-1"
    assert script.version == 1
    assert script.text == TEXT
    assert script.created_at == T0


def test_create_gives_each_script_its_own_id() -> None:
    assert new_script().id != new_script().id


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    script = Script.create("item-1", TEXT)
    assert before <= script.created_at <= datetime.now(UTC)
    assert script.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(("item_id", "text"), [("", TEXT), (" ", TEXT), ("i", " ")])
def test_create_needs_a_content_item_and_text(item_id: str, text: str) -> None:
    with pytest.raises(ValueError):
        Script.create(item_id, text)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"version": 0},
        {"version": True},
        {"version": 1.0},
        {"created_at": datetime(2026, 9, 29, 10, 0)},
        {"created_at": T0.astimezone(timezone(timedelta(hours=7)))},
    ],
)
def test_script_rejects_invalid_state(changes) -> None:
    with pytest.raises(ValueError):
        rebuild(new_script(), **changes)


def test_script_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_script().text = "changed"  # type: ignore[misc]


def test_next_version_is_a_new_record_and_keeps_the_old_one() -> None:
    first = new_script()

    second = first.next_version("  A shorter hook.  ", clock=at(T1))

    assert second.id != first.id
    assert second.content_item_id == "item-1"
    assert second.version == 2
    assert second.text == "A shorter hook."
    assert second.created_at == T1
    assert (first.version, first.text, first.created_at) == (1, TEXT, T0)


def test_versions_keep_counting_up() -> None:
    third = (
        new_script()
        .next_version("Second", clock=at(T1))
        .next_version("Third", clock=at(T1))
    )
    assert third.version == 3


@pytest.mark.parametrize("text", [TEXT, f"  {TEXT}  "])
def test_next_version_refuses_unchanged_text(text: str) -> None:
    with pytest.raises(ValueError, match="different script text"):
        new_script().next_version(text, clock=at(T1))


def test_next_version_refuses_empty_text() -> None:
    with pytest.raises(ValueError):
        new_script().next_version("  ", clock=at(T1))


# Claims


def test_claim_belongs_to_one_exact_script_version() -> None:
    script = new_script()

    claim = Claim.create(
        script.id, "  Most people never check bank fees. ", clock=at(T1)
    )

    assert len(claim.id) == 32
    assert claim.script_id == script.id
    assert claim.text == "Most people never check bank fees."
    assert claim.created_at == T1


def test_claims_do_not_change_the_script() -> None:
    script = new_script()
    Claim.create(script.id, "A claim", clock=at(T1))
    assert (script.version, script.text) == (1, TEXT)


def test_a_new_script_version_does_not_inherit_claims() -> None:
    first = new_script()
    claim = Claim.create(first.id, "A claim", clock=at(T1))
    second = first.next_version("Rewritten", clock=at(T1))
    assert claim.script_id == first.id != second.id


@pytest.mark.parametrize(("script_id", "text"), [("", "A claim"), ("s", "  ")])
def test_claim_needs_a_script_and_text(script_id: str, text: str) -> None:
    with pytest.raises(ValueError):
        Claim.create(script_id, text)


@pytest.mark.parametrize(
    "changes", [{"id": ""}, {"created_at": datetime(2026, 9, 29, 10, 0)}]
)
def test_claim_rejects_invalid_state(changes) -> None:
    with pytest.raises(ValueError):
        rebuild(Claim.create("s", "A claim", clock=at(T0)), **changes)


def test_claim_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Claim.create("s", "A claim").text = "changed"  # type: ignore[misc]


# Evidence


def test_evidence_links_a_claim_to_a_source() -> None:
    claim = Claim.create("s", "A claim", clock=at(T0))

    evidence = Evidence.create(
        claim.id, "source-42", "  Survey: 61% never compare fees. ", clock=at(T1)
    )

    assert len(evidence.id) == 32
    assert evidence.claim_id == claim.id
    assert evidence.source_ref == "source-42"
    assert evidence.excerpt == "Survey: 61% never compare fees."
    assert evidence.created_at == T1


def test_a_claim_can_have_several_pieces_of_evidence() -> None:
    claim = Claim.create("s", "A claim")
    links = [Evidence.create(claim.id, ref) for ref in ("source-1", "source-2")]
    assert {link.claim_id for link in links} == {claim.id}
    assert links[0].id != links[1].id


def test_excerpt_is_optional() -> None:
    assert Evidence.create("c", "source-1").excerpt is None


@pytest.mark.parametrize(
    ("claim_id", "source_ref", "excerpt"),
    [("", "source-1", None), ("c", "", None), ("c", "  ", None), ("c", "s", "  ")],
)
def test_evidence_rejects_missing_links_and_empty_excerpts(
    claim_id: str, source_ref: str, excerpt: str | None
) -> None:
    with pytest.raises(ValueError):
        Evidence.create(claim_id, source_ref, excerpt)


@pytest.mark.parametrize(
    "changes", [{"id": ""}, {"created_at": datetime(2026, 9, 29, 10, 0)}]
)
def test_evidence_rejects_invalid_state(changes) -> None:
    with pytest.raises(ValueError):
        rebuild(Evidence.create("c", "source-1", clock=at(T0)), **changes)


def test_evidence_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Evidence.create("c", "source-1").source_ref = "x"  # type: ignore[misc]


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    script = new_script()
    claim = Claim.create(script.id, "A claim", clock=at(T0))
    evidence = Evidence.create(claim.id, "source-1", clock=at(T0))
    stamp = "2026-09-29T10:00:00+00:00"

    assert script.as_dict() == {
        "id": script.id,
        "content_item_id": "item-1",
        "version": 1,
        "text": TEXT,
        "created_at": stamp,
        # F-064: sections, duration and version history.
        "sections": [{"kind": "body", "title": None, "text": TEXT, "seconds": None}],
        "duration_target": None,
        "estimated_seconds": 5,
        "within_target": None,
        "created_by": None,
        "reason": None,
        "parent_id": None,
        "strategy_version": None,
        "research_report_id": None,
    }
    assert claim.as_dict() == {
        "id": claim.id,
        "script_id": script.id,
        "text": "A claim",
        "created_at": stamp,
        "section_index": None,
        # F-068: claim kind and extraction run.
        "kind": None,
        "extraction_id": None,
    }
    assert evidence.as_dict() == {
        "id": evidence.id,
        "claim_id": claim.id,
        "source_ref": "source-1",
        "excerpt": None,
        "created_at": stamp,
    }
