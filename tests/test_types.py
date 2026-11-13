from __future__ import annotations

import pytest

from medallion import types as t


@pytest.mark.parametrize(
    "name",
    [
        "string",
        "int",
        "bigint",
        "double",
        "boolean",
        "date",
        "timestamp",
        "decimal(18,2)",
        "DECIMAL(10, 0)",
        " Decimal(38,38) ",
    ],
)
def test_valid_types(name):
    assert t.is_valid(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "varchar",
        "decimal",
        "decimal(0,0)",
        "decimal(39,2)",
        "decimal(5,6)",
        "float",
        "array<string>",
    ],
)
def test_invalid_types(name):
    assert not t.is_valid(name)


def test_spark_type_rendering():
    assert t.spark_type("decimal(18, 2)") == "DECIMAL(18,2)"
    assert t.spark_type("Timestamp") == "TIMESTAMP"


@pytest.mark.parametrize(
    ("value", "typ", "expected"),
    [
        (None, "int", None),
        (None, "string", None),
        ("abc", "string", "abc"),
        (5, "string", "5"),
        (True, "string", "true"),
        (" 42 ", "int", 42),
        ("+7", "int", 7),
        ("4.0", "int", None),
        ("abc", "int", None),
        (2**31, "int", None),
        (2**31 - 1, "int", 2**31 - 1),
        (2**31, "bigint", 2**31),
        (2**63, "bigint", None),
        (3.0, "int", 3),
        (3.5, "int", None),
        (True, "int", None),
        ("1.5", "double", 1.5),
        ("nan", "double", None),
        ("inf", "double", None),
        ("x", "double", None),
        (True, "double", None),
        ("TRUE", "boolean", True),
        ("0", "boolean", False),
        (1, "boolean", True),
        (2, "boolean", None),
        ("yes", "boolean", None),
        ("2024-02-29", "date", "2024-02-29"),
        ("2023-02-29", "date", None),
        ("20240229", "date", None),
        ("2024-2-9", "date", None),
        (20240229, "date", None),
    ],
)
def test_try_cast_simple(value, typ, expected):
    assert t.try_cast(value, typ) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-02-01T10:00:00Z", "2026-02-01T10:00:00.000000Z"),
        ("2026-02-01 10:00:00", "2026-02-01T10:00:00.000000Z"),
        ("2026-02-01T10:00:00.5Z", "2026-02-01T10:00:00.500000Z"),
        ("2026-02-01T10:00:00.123456789Z", None),
        ("2026-02-03T15:30:00+01:00", "2026-02-03T14:30:00.000000Z"),
        ("2026-02-03T23:30:00-05:00", "2026-02-04T04:30:00.000000Z"),
        ("2026-02-03T23:30:00+24:00", None),
        ("2026-13-01T00:00:00Z", None),
        ("2026-02-01", None),
        ("yesterday", None),
        (5, None),
    ],
)
def test_try_cast_timestamp_normalises_to_utc(value, expected):
    assert t.try_cast(value, "timestamp") == expected


def test_timestamps_sort_chronologically_as_text():
    a = t.try_cast("2026-02-03T15:30:00+01:00", "timestamp")  # 14:30Z
    b = t.try_cast("2026-02-03T14:45:00Z", "timestamp")
    assert isinstance(a, str) and isinstance(b, str)
    assert a < b


@pytest.mark.parametrize(
    ("value", "typ", "expected"),
    [
        ("12.345", "decimal(18,2)", 12.35),
        ("12.344", "decimal(18,2)", 12.34),
        ("-12.345", "decimal(18,2)", -12.35),
        (" 15.999 ", "decimal(18,2)", 16.0),
        (59.9, "decimal(18,2)", 59.9),
        ("1e2", "decimal(18,2)", 100.0),
        ("abc", "decimal(18,2)", None),
        ("nan", "decimal(18,2)", None),
        ("1234", "decimal(5,2)", None),
        ("123.45", "decimal(5,2)", 123.45),
        (True, "decimal(5,2)", None),
    ],
)
def test_try_cast_decimal(value, typ, expected):
    assert t.try_cast(value, typ) == expected


def test_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown type"):
        t.try_cast("x", "varchar")
