from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_youtube_agent.core.db.codec import (
    format_datetime,
    format_decimal,
    parse_datetime,
    parse_decimal,
)

# Datetimes


def test_datetime_is_fixed_width_utc_text() -> None:
    moment = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    text = format_datetime(moment)
    assert text == "2026-09-29T10:00:00.000000Z"
    assert len(text) == 27


def test_datetime_keeps_microseconds() -> None:
    moment = datetime(2026, 9, 29, 10, 0, 1, 42, tzinfo=UTC)
    assert format_datetime(moment) == "2026-09-29T10:00:01.000042Z"


def test_datetime_round_trips() -> None:
    moment = datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    assert parse_datetime(format_datetime(moment)) == moment
    assert parse_datetime(format_datetime(moment)).tzinfo is UTC


def test_datetime_text_sorts_in_time_order() -> None:
    base = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    moments = [
        base,
        base + timedelta(microseconds=1),
        base + timedelta(seconds=1),
        base + timedelta(days=40),
        base - timedelta(days=400),
    ]
    texts = [format_datetime(m) for m in moments]
    assert sorted(texts) == [format_datetime(m) for m in sorted(moments)]


def test_other_utc_representations_are_normalised() -> None:
    zero_offset = timezone(timedelta(0))
    moment = datetime(2026, 9, 29, 10, 0, tzinfo=zero_offset)
    assert format_datetime(moment) == "2026-09-29T10:00:00.000000Z"


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 9, 29, 10, 0),
        datetime(2026, 9, 29, 17, 0, tzinfo=timezone(timedelta(hours=7))),
        "2026-09-29T10:00:00.000000Z",
    ],
)
def test_only_utc_datetimes_can_be_stored(moment) -> None:
    with pytest.raises(ValueError):
        format_datetime(moment)


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-29T10:00:00+00:00",
        "2026-09-29T10:00:00Z",
        "2026-09-29T10:00:00.000000",
        "2026-09-29 10:00:00.000000Z",
        "",
        None,
    ],
)
def test_parse_datetime_accepts_only_the_canonical_form(text) -> None:
    with pytest.raises(ValueError):
        parse_datetime(text)


# Decimals


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("12.34"), "12.34"),
        (Decimal("5.00"), "5.00"),
        (Decimal("0"), "0"),
        (Decimal("-3"), "-3"),
        (Decimal("1E+2"), "100"),
        (Decimal("1E-7"), "0.0000001"),
        (Decimal("-0"), "0"),
        (Decimal("-0.00"), "0.00"),
    ],
)
def test_decimal_is_plain_text(value: Decimal, text: str) -> None:
    assert format_decimal(value) == text


@pytest.mark.parametrize(
    "value", [12.34, 12, "12.34", Decimal("NaN"), Decimal("Infinity"), None]
)
def test_only_finite_decimals_can_be_stored(value) -> None:
    with pytest.raises(ValueError):
        format_decimal(value)


@pytest.mark.parametrize("text", ["12.34", "5.00", "0", "-3", "0.0000001"])
def test_decimal_round_trips(text: str) -> None:
    assert format_decimal(parse_decimal(text)) == text


@pytest.mark.parametrize(
    "text", ["1e3", "1E+2", "", " 1", "+1", "1.", ".5", "1,5", "NaN", None]
)
def test_parse_decimal_accepts_only_the_canonical_form(text) -> None:
    with pytest.raises(ValueError):
        parse_decimal(text)
