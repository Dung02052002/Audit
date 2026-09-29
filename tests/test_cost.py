import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_youtube_agent.content.cost import CostCategory, CostRecord

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def at(moment: datetime):
    return lambda: moment


def new_record(category: CostCategory = CostCategory.TTS, **overrides) -> CostRecord:
    values = {
        "provider": "mock_tts",
        "amount": Decimal("0.0421"),
        "currency": "USD",
        "clock": at(T0),
    }
    return CostRecord.create("channel-1", category, **{**values, **overrides})


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


# Categories


def test_categories_are_the_prompt_025_list_plus_llm() -> None:
    assert [c.value for c in CostCategory] == [
        "production",
        "api",
        "tts",
        "render",
        "storage",
        "llm",
    ]


# Creating a record


@pytest.mark.parametrize("category", list(CostCategory))
def test_create_records_a_cost_for_each_category(category: CostCategory) -> None:
    record = new_record(category)

    assert len(record.id) == 32
    assert record.channel_id == "channel-1"
    assert record.category is category
    assert record.provider == "mock_tts"
    assert record.amount == Decimal("0.0421")
    assert record.currency == "USD"
    assert record.incurred_at == T0
    assert record.content_item_id is None
    assert record.ref is None


def test_cost_can_point_to_an_item_and_a_job_or_artifact() -> None:
    record = new_record(content_item_id="item-1", ref="tts-job-9")
    assert (record.content_item_id, record.ref) == ("item-1", "tts-job-9")


def test_a_channel_level_cost_needs_no_item() -> None:
    record = new_record(CostCategory.STORAGE, provider="object-store")
    assert record.content_item_id is None


def test_zero_cost_is_allowed() -> None:
    assert new_record(amount=Decimal("0")).amount == 0


def test_each_record_gets_its_own_id() -> None:
    assert new_record().id != new_record().id


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    record = new_record(clock=None)
    assert before <= record.incurred_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": ""},
        {"provider": "Mock TTS"},
        {"provider": "1tts"},
        {"amount": Decimal("-0.01")},
        {"amount": Decimal("NaN")},
        {"amount": Decimal("-Infinity")},
        {"amount": 1},
        {"amount": 0.5},
        {"currency": "usd"},
        {"currency": "EURO"},
        {"content_item_id": " "},
        {"ref": ""},
        {"clock": at(datetime(2026, 9, 29, 10, 0))},
        {"clock": at(T0.astimezone(timezone(timedelta(hours=7))))},
    ],
)
def test_create_rejects_bad_values(overrides) -> None:
    with pytest.raises(ValueError):
        new_record(**overrides)


def test_create_needs_a_channel() -> None:
    with pytest.raises(ValueError):
        CostRecord.create(
            " ",
            CostCategory.RENDER,
            provider="mock_render",
            amount=Decimal("1"),
            currency="USD",
        )


@pytest.mark.parametrize("category", ["tts", "gpu", None])
def test_category_must_be_a_cost_category(category) -> None:
    with pytest.raises(TypeError):
        new_record(category)


def test_record_rejects_an_empty_id() -> None:
    with pytest.raises(ValueError):
        rebuild(new_record(), id="")


def test_record_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_record().amount = Decimal("0")  # type: ignore[misc]


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    record = new_record(CostCategory.LLM, provider="mock_llm", ref="job-1")

    assert record.as_dict() == {
        "id": record.id,
        "channel_id": "channel-1",
        "category": "llm",
        "provider": "mock_llm",
        "amount": "0.0421",
        "currency": "USD",
        "incurred_at": "2026-09-29T10:00:00+00:00",
        "content_item_id": None,
        "ref": "job-1",
    }
