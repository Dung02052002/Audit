import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_youtube_agent.content.analytics import MetricScope, MetricSnapshot

START = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
END = START + timedelta(days=1)
RETRIEVED = END + timedelta(hours=2)
METRICS = {"views": Decimal("1520"), "subscribers_net": Decimal("-3")}


def at(moment: datetime):
    return lambda: moment


def new_snapshot(**overrides) -> MetricSnapshot:
    values = {
        "source": "youtube_analytics",
        "period_start": START,
        "period_end": END,
        "metrics": METRICS,
        "clock": at(RETRIEVED),
    }
    return MetricSnapshot.create(
        MetricScope.VIDEO, "dQw4w9WgXcQ", **{**values, **overrides}
    )


def rebuild(entity, **changes):
    values = {f.name: getattr(entity, f.name) for f in dataclasses.fields(entity)}
    return type(entity)(**{**values, **changes})


# Creating a snapshot


def test_scopes() -> None:
    assert [s.value for s in MetricScope] == ["channel", "video"]


def test_create_records_source_period_and_retrieval_time() -> None:
    snapshot = new_snapshot()

    assert len(snapshot.id) == 32
    assert snapshot.scope is MetricScope.VIDEO
    assert snapshot.subject_id == "dQw4w9WgXcQ"
    assert snapshot.source == "youtube_analytics"
    assert (snapshot.period_start, snapshot.period_end) == (START, END)
    assert snapshot.retrieved_at == RETRIEVED
    assert dict(snapshot.metrics) == METRICS


def test_channel_scope() -> None:
    snapshot = MetricSnapshot.create(
        MetricScope.CHANNEL,
        "channel-1",
        source="mock",
        period_start=START,
        period_end=END,
        metrics={"subscribers": Decimal("1000")},
        clock=at(RETRIEVED),
    )
    assert snapshot.scope is MetricScope.CHANNEL


def test_create_defaults_to_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    snapshot = new_snapshot(clock=None)
    assert before <= snapshot.retrieved_at <= datetime.now(UTC)


def test_each_snapshot_gets_its_own_id() -> None:
    assert new_snapshot().id != new_snapshot().id


def test_retrieval_during_the_period_is_allowed() -> None:
    snapshot = new_snapshot(clock=at(START + timedelta(hours=1)))
    assert snapshot.retrieved_at < snapshot.period_end


# Metrics


def test_missing_metrics_stay_absent() -> None:
    snapshot = new_snapshot(metrics={"views": Decimal("10")})
    assert "ctr" not in snapshot.metrics
    assert snapshot.metrics.get("retention") is None


def test_metrics_are_read_only_and_copied() -> None:
    source = dict(METRICS)
    snapshot = new_snapshot(metrics=source)
    source["views"] = Decimal("0")

    assert snapshot.metrics["views"] == Decimal("1520")
    with pytest.raises(TypeError):
        snapshot.metrics["views"] = Decimal("1")  # type: ignore[index]


@pytest.mark.parametrize(
    "metrics",
    [
        {},
        {"Views": Decimal("1")},
        {"watch time": Decimal("1")},
        {"1views": Decimal("1")},
        {"views": 10},
        {"views": 1.5},
        {"views": "10"},
        {"views": Decimal("NaN")},
        {"views": Decimal("Infinity")},
    ],
)
def test_create_rejects_bad_metrics(metrics) -> None:
    with pytest.raises(ValueError):
        new_snapshot(metrics=metrics)


def test_zero_and_negative_values_are_real_values() -> None:
    snapshot = new_snapshot(
        metrics={"views": Decimal("0"), "subscribers_net": Decimal("-12.5")}
    )
    assert snapshot.metrics["views"] == 0
    assert snapshot.metrics["subscribers_net"] == Decimal("-12.5")


# Validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": ""},
        {"source": "YouTube"},
        {"source": "you tube"},
        {"period_end": START},
        {"period_end": START - timedelta(hours=1)},
        {"period_start": datetime(2026, 9, 28)},
        {"period_end": END.astimezone(timezone(timedelta(hours=7)))},
        {"clock": at(START - timedelta(seconds=1))},
        {"clock": at(datetime(2026, 9, 29, 12))},
    ],
)
def test_create_rejects_bad_source_period_or_time(overrides) -> None:
    with pytest.raises(ValueError):
        new_snapshot(**overrides)


def test_create_needs_a_subject() -> None:
    with pytest.raises(ValueError):
        MetricSnapshot.create(
            MetricScope.VIDEO,
            " ",
            source="mock",
            period_start=START,
            period_end=END,
            metrics=METRICS,
        )


@pytest.mark.parametrize("changes", [{"id": ""}, {"scope": "video"}])
def test_snapshot_rejects_invalid_state(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        rebuild(new_snapshot(), **changes)


def test_snapshot_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        new_snapshot().source = "other"  # type: ignore[misc]


# Freshness


def test_age_is_measured_from_retrieval() -> None:
    snapshot = new_snapshot()
    assert snapshot.age_at(RETRIEVED) == timedelta(0)
    assert snapshot.age_at(RETRIEVED + timedelta(hours=5)) == timedelta(hours=5)


@pytest.mark.parametrize(
    ("elapsed", "stale"),
    [
        (timedelta(0), False),
        (timedelta(hours=6), False),
        (timedelta(hours=6, seconds=1), True),
        (timedelta(days=2), True),
    ],
)
def test_is_stale_compares_age_with_max_age(elapsed, stale) -> None:
    snapshot = new_snapshot()
    assert snapshot.is_stale(RETRIEVED + elapsed, timedelta(hours=6)) is stale


def test_age_needs_a_later_utc_time() -> None:
    snapshot = new_snapshot()
    with pytest.raises(ValueError):
        snapshot.age_at(RETRIEVED - timedelta(seconds=1))
    with pytest.raises(ValueError):
        snapshot.age_at(datetime(2026, 9, 30, 12))


@pytest.mark.parametrize("max_age", [timedelta(0), timedelta(seconds=-1)])
def test_max_age_must_be_positive(max_age) -> None:
    with pytest.raises(ValueError):
        new_snapshot().is_stale(RETRIEVED, max_age)


def test_freshness_is_not_stored() -> None:
    names = {f.name for f in dataclasses.fields(MetricSnapshot)}
    assert not names & {"freshness", "is_fresh", "stale"}


# Serialisation


def test_as_dict_is_json_friendly() -> None:
    snapshot = new_snapshot()

    assert snapshot.as_dict() == {
        "id": snapshot.id,
        "scope": "video",
        "subject_id": "dQw4w9WgXcQ",
        "source": "youtube_analytics",
        "period_start": "2026-09-28T00:00:00+00:00",
        "period_end": "2026-09-29T00:00:00+00:00",
        "retrieved_at": "2026-09-29T02:00:00+00:00",
        "metrics": {"views": "1520", "subscribers_net": "-3"},
    }
