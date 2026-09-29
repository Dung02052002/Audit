"""Canonical storage formats for the database.

- Datetimes are TEXT ``YYYY-MM-DDTHH:MM:SS.ffffffZ``: always UTC and always six
  microsecond digits, so every value has the same width and string order is
  time order.
- Decimals are TEXT in plain notation, such as ``12.34`` or ``-3``: never a
  float and never an exponent. The scale is kept, so ``5.00`` stays ``5.00``.

The schema checks the same shapes, so a text value in any other format is
refused. SQLite converts a number bound to a TEXT column into text before the
check runs, and rounds floats while doing so, so the database cannot refuse a
float by itself: every writer must store money through ``format_decimal``,
which refuses floats.
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
DECIMAL_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")


def format_datetime(moment: datetime) -> str:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError("only timezone-aware UTC datetimes can be stored")
    return moment.astimezone(UTC).strftime(DATETIME_FORMAT)


def parse_datetime(text: str) -> datetime:
    if not isinstance(text, str) or not DATETIME_PATTERN.match(text):
        raise ValueError(f"{text!r} is not a canonical UTC datetime")
    return datetime.strptime(text, DATETIME_FORMAT).replace(tzinfo=UTC)


def format_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("only finite Decimal values can be stored")
    if value.is_zero():
        value = value.copy_abs()
    return format(value, "f")


def parse_decimal(text: str) -> Decimal:
    if not isinstance(text, str) or not DECIMAL_PATTERN.match(text):
        raise ValueError(f"{text!r} is not a canonical decimal")
    return Decimal(text)
