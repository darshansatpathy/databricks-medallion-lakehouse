"""The logical type system and a Python reference implementation of ``try_cast``.

Bronze is always ``string``. Silver and gold use the types below. ``try_cast`` mirrors the
behaviour the generated SQL relies on: a value that cannot be converted becomes NULL and is
then caught by an expectation, instead of failing the pipeline.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SIMPLE = {"string", "int", "bigint", "double", "boolean", "date", "timestamp"}
_DECIMAL = re.compile(r"^decimal\((\d{1,2}),\s*(\d{1,2})\)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$")
INT_RANGE = (-(2**31), 2**31 - 1)
BIGINT_RANGE = (-(2**63), 2**63 - 1)


def normalise(t: str) -> str:
    return t.strip().lower().replace(" ", "")


def is_valid(t: str) -> bool:
    t = normalise(t)
    m = _DECIMAL.match(t)
    if m:
        p, s = int(m.group(1)), int(m.group(2))
        return 1 <= p <= 38 and 0 <= s <= p
    return t in SIMPLE


def spark_type(t: str) -> str:
    """Render a logical type as a Spark SQL type name."""
    return normalise(t).upper()


def try_cast(value: object, t: str) -> object:
    """Convert ``value`` to logical type ``t`` or return None. Never raises."""
    t = normalise(t)
    if value is None:
        return None
    if t == "string":
        return _to_string(value)
    text = value.strip() if isinstance(value, str) else value
    if t in ("int", "bigint"):
        return _to_int(text, INT_RANGE if t == "int" else BIGINT_RANGE)
    if t == "double":
        return _to_double(text)
    if t == "boolean":
        return _to_bool(text)
    if t == "date":
        return _to_date(text)
    if t == "timestamp":
        return _to_timestamp(text)
    m = _DECIMAL.match(t)
    if m:
        return _to_decimal(text, int(m.group(1)), int(m.group(2)))
    raise ValueError(f"unknown type {t!r}")


def _to_string(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(v)
    return str(v)


def _to_int(v: object, rng: tuple[int, int]) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, float):
        if not v.is_integer():
            return None
        n = int(v)
    elif isinstance(v, str) and re.fullmatch(r"[+-]?\d+", v):
        n = int(v)
    else:
        return None
    return n if rng[0] <= n <= rng[1] else None


def _to_double(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _to_bool(v: object) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        low = v.lower()
        if low in ("true", "1"):
            return True
        if low in ("false", "0"):
            return False
    return None


def _to_date(v: object) -> str | None:
    if not isinstance(v, str) or not _DATE.match(v):
        return None
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def _to_timestamp(v: object) -> str | None:
    """Return a normalised UTC string, ``YYYY-MM-DDTHH:MM:SS.ffffffZ``, which sorts correctly."""
    if not isinstance(v, str):
        return None
    m = _TS.match(v)
    if not m:
        return None
    day, clock, frac, zone = m.groups()
    micro = ((frac or ".0")[1:] + "000000")[:6]
    try:
        dt = datetime.fromisoformat(f"{day}T{clock}.{micro}")
    except ValueError:
        return None
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        hh, mm = int(zone[1:3]), int(zone[4:6])
        if hh > 23 or mm > 59:
            return None
        from datetime import timedelta

        dt = dt - sign * timedelta(hours=hh, minutes=mm)
    return dt.replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _to_decimal(v: object, precision: int, scale: int) -> float | None:
    """Decimals are held as floats in the oracle; they are rounded half-up to ``scale``."""
    if isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    q = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
    if len(q.as_tuple().digits) > precision:
        return None
    return float(q)
