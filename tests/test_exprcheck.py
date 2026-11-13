from __future__ import annotations

import pytest

from medallion import exprcheck as e


@pytest.mark.parametrize(
    "expr",
    [
        "order_id IS NOT NULL AND length(order_id) > 0",
        "status IN ('new', 'completed')",
        "amount >= 0 OR amount IS NULL",
        "coalesce(c.country, 'ZZ')",
        "sum(o.amount) + 1",
        "count(*)",
        "count(DISTINCT o.customer_id)",
        "CASE WHEN a = 1 THEN 'x' ELSE 'y' END",
        "a BETWEEN 1 AND 10",
        "a || '-' || b",
        "round(avg(o.amount), 2)",
        "x <> 'a' AND y != 'b'",
        "-a < 0",
    ],
)
def test_portable_expressions_pass(expr):
    assert e.check(expr) == []


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("a / b", "not allowed"),
        ("a % b", "not allowed"),
        ("a LIKE 'x%'", "not allowed"),
        ("a ILIKE 'x'", "not allowed"),
        ("cast(a as int)", "not allowed"),
        ("a; DROP TABLE t", "separators"),
        ("a -- comment", "comments"),
        ("a /* c */", "comments"),
        ("a = 'x\\ny'", "backslashes"),
        ("a = 'it''s'", "adjacent string literals"),
        ("a = `b`", "not allowed"),
        ('a = "b"', "not allowed"),
        ("unknown_fn(a)", "not in the portable subset"),
        ("(a = 1", "unbalanced"),
        ("a = 1)", "unbalanced"),
        ("select 1", "not allowed"),
        ("a IN (select b from c)", "not allowed"),
        ("a = $x", "not allowed"),
        ("a::int", "not allowed"),
    ],
)
def test_unsafe_or_non_portable_expressions_fail(expr, fragment):
    problems = e.check(expr)
    assert problems, expr
    assert any(fragment in p for p in problems), problems


def test_identifiers_exclude_keywords_functions_and_literals():
    assert e.identifiers("o.amount + coalesce(p.amount, 0) = 'order_id' AND x IS NOT NULL") == [
        "o.amount",
        "p.amount",
        "x",
    ]
    assert e.identifiers("a AND a OR b") == ["a", "b"]
    assert e.identifiers("c.__END_AT IS NULL") == ["c.__END_AT"]


def test_functions_and_aggregate_detection():
    assert e.functions("sum(a) + COUNT(*) + lower(b)") == {"sum", "count", "lower"}
    assert e.is_aggregate("round(avg(x), 2)")
    assert not e.is_aggregate("lower(x)")
    assert e.is_count_only("count(*)")
    assert e.is_count_only("count(distinct a)")
    assert not e.is_count_only("max(a)")
    assert not e.is_count_only("a")


def test_tokeniser_reports_the_first_bad_character_and_stops():
    _, problems = e.tokenise("a = 1 @ 2")
    assert problems == ["character '@' is not allowed"]
