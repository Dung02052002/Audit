import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.revenue import RevenueRecord, RevenueStage

START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 29, tzinfo=UTC)
DURING = END - timedelta(days=1)
AFTER = END + timedelta(days=3)
ESTIMATED = RevenueStage.ESTIMATED
FINAL = RevenueStage.FINAL


def at(moment: datetime):
    return lambda: moment


def new_record(stage: RevenueStage = ESTIMATED, **overrides) -> RevenueRecord:
    values = {
        "revenue_type": "ads",
        "source": "youtube_analytics",
        "amount": Decimal("12.34"),
        "currency": "USD",
        "period_start": START,
        "period_end": END,
        "clock": at(DURING if stage is ESTIMATED else AFTER),
    }
    return RevenueRecord.create(
        stage, MetricScope.CHANNEL, "channel-1", **{**values, **overrides}
    )


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


# Stages


def test_stages() -> None:
    assert [s.value for s in RevenueStage] == ["estimated", "final"]


def test_create_records_an_estimate() -> None:
    record = new_record()

    assert len(record.id) == 32
    assert record.stage is ESTIMATED
    assert not record.is_final
    assert record.scope is MetricScope.CHANNEL
    assert record.subject_id == "channel-1"
    assert record.revenue_type == "ads"
    assert record.source == "youtube_analytics"
    assert record.amount == Decimal("12.34")
    assert record.currency == "USD"
    assert (record.period_start, record.period_end) == (START, END)
    assert record.retrieved_at == DURING


def test_final_is_a_separate_record_next_to_the_estimate() -> None:
    estimate = new_record()
    final = new_record(FINAL, amount=Decimal("11.90"))

    assert final.is_final
    assert final.id != estimate.id
    assert (estimate.stage, estimate.amount) == (ESTIMATED, Decimal("12.34"))
    assert (final.stage, final.amount) == (FINAL, Decimal("11.90"))


def test_an_estimate_cannot_be_turned_into_a_final() -> None:
    names = {"finalize", "finalise", "to_final", "with_stage", "with_amount"}
    assert not names & set(dir(RevenueRecord))
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_record().stage = FINAL  # type: ignore[misc]


def test_final_revenue_needs_a_finished_period() -> None:
    with pytest.raises(ValueError, match="after the period ends"):
        new_record(FINAL, clock=at(DURING))


def test_final_revenue_may_be_read_at_the_period_end() -> None:
    assert new_record(FINAL, clock=at(END)).retrieved_at == END


def test_an_estimate_may_be_read_during_the_period() -> None:
    assert new_record(clock=at(START)).retrieved_at == START


# Values


@pytest.mark.parametrize("scope", list(MetricScope))
def test_revenue_for_channel_or_video(scope: MetricScope) -> None:
    record = RevenueRecord.create(
        ESTIMATED,
        scope,
        "subject-1",
        revenue_type="memberships",
        source="mock",
        amount=Decimal("0"),
        currency="EUR",
        period_start=START,
        period_end=END,
        clock=at(DURING),
    )
    assert record.scope is scope
    assert record.amount == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"revenue_type": ""},
        {"revenue_type": "Ads"},
        {"revenue_type": "super thanks"},
        {"source": ""},
        {"source": "YouTube"},
        {"amount": Decimal("-0.01")},
        {"amount": Decimal("NaN")},
        {"amount": Decimal("Infinity")},
        {"amount": 12},
        {"amount": 12.34},
        {"currency": "usd"},
        {"currency": "US"},
        {"currency": ""},
        {"period_end": START},
        {"period_end": START - timedelta(days=1)},
        {"period_start": datetime(2026, 9, 1)},
        {"period_end": END.astimezone(timezone(timedelta(hours=7)))},
        {"clock": at(START - timedelta(seconds=1))},
        {"clock": at(datetime(2026, 9, 28))},
    ],
)
def test_create_rejects_bad_values(overrides) -> None:
    with pytest.raises(ValueError):
        new_record(**overrides)


def test_create_needs_a_subject() -> None:
    with pytest.raises(ValueError):
        RevenueRecord.create(
            ESTIMATED,
            MetricScope.VIDEO,
            " ",
            revenue_type="ads",
            source="mock",
            amount=Decimal("1"),
            currency="USD",
            period_start=START,
            period_end=END,
        )


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    record = new_record(
        period_start=before - timedelta(days=1),
        period_end=before + timedelta(days=1),
        clock=None,
    )
    assert before <= record.retrieved_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    "changes", [{"id": ""}, {"stage": "final"}, {"scope": "channel"}]
)
def test_record_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(new_record(), **changes)


# Freshness


def test_age_and_staleness_come_from_retrieval_time() -> None:
    record = new_record(FINAL)

    assert record.age_at(AFTER + timedelta(hours=3)) == timedelta(hours=3)
    assert not record.is_stale(AFTER + timedelta(days=1), timedelta(days=1))
    assert record.is_stale(AFTER + timedelta(days=1, seconds=1), timedelta(days=1))


def test_age_needs_a_later_utc_time() -> None:
    record = new_record()
    with pytest.raises(ValueError):
        record.age_at(DURING - timedelta(seconds=1))
    with pytest.raises(ValueError):
        record.age_at(datetime(2026, 9, 30))


def test_max_age_must_be_positive() -> None:
    with pytest.raises(ValueError):
        new_record().is_stale(DURING, timedelta(0))


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    record = new_record(FINAL)

    assert record.as_dict() == {
        "id": record.id,
        "stage": "final",
        "scope": "channel",
        "subject_id": "channel-1",
        "revenue_type": "ads",
        "source": "youtube_analytics",
        "amount": "12.34",
        "currency": "USD",
        "period_start": "2026-09-01T00:00:00+00:00",
        "period_end": "2026-09-29T00:00:00+00:00",
        "retrieved_at": "2026-10-02T00:00:00+00:00",
    }
