"""A deliberately small, portable SQL expression subset.

Every expression in a spec (expectations, silver column expressions, gold select lists,
filters and join conditions) must stay inside this subset. That has two purposes:

* the text is safe to splice into generated SQL (no statements, comments or quoting tricks);
* it means the same text means the same thing on Spark SQL and on SQLite, which is what the
  reference executor in :mod:`medallion.oracle` uses to run it.

Operators and functions whose behaviour differs between the engines are left out on purpose:
``/`` and ``%`` (integer versus decimal division), ``LIKE`` (case sensitivity) and ``CAST``
(spec types do the casting).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KEYWORDS = {
    "and", "or", "not", "is", "null", "in", "between", "case", "when", "then", "else", "end",
    "distinct", "true", "false",
}  # fmt: skip
SCALAR_FUNCTIONS = {
    "lower", "upper", "trim", "length", "coalesce", "abs", "round", "nullif", "substr", "to_date",
}  # fmt: skip
BANNED = {
    "like", "ilike", "rlike", "regexp", "cast", "div", "mod", "select", "from", "where", "join",
    "union", "insert", "update", "delete", "drop", "create", "alter", "limit", "group", "order",
    "having", "as", "over", "exists", "all", "any", "some",
}  # fmt: skip
AGGREGATES = {"count", "sum", "avg", "min", "max"}
FUNCTIONS = SCALAR_FUNCTIONS | AGGREGATES

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<str>'[^'\\]*')
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<id>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    | (?P<op><=|>=|<>|!=|\|\||=|<|>|\+|-|\*)
    | (?P<punct>[(),])
    )""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str


def tokenise(expr: str) -> tuple[list[Token], list[str]]:
    problems: list[str] = []
    tokens: list[Token] = []
    pos = 0
    stripped = re.sub(r"'[^'\\]*'", "''", expr)
    if "\\" in expr:
        problems.append("backslashes are not allowed (Spark and SQLite escape them differently)")
    if "--" in stripped or "/*" in stripped or ";" in stripped:
        problems.append("comments and statement separators are not allowed")
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN.match(expr, pos)
        if not m:
            bad = expr[pos:].lstrip()[:1]
            problems.append(f"character {bad!r} is not allowed")
            break
        pos = m.end()
        kind = m.lastgroup or ""
        if kind == "str" and tokens and tokens[-1].kind == "str":
            problems.append(
                "adjacent string literals are not allowed "
                "(Spark concatenates them, SQLite does not)"
            )
        tokens.append(Token(kind, m.group(kind)))
    return tokens, problems


def check(expr: str) -> list[str]:
    """Return the problems with ``expr``; an empty list means it is inside the subset."""
    if not expr.strip():
        return ["expression is empty"]
    tokens, problems = tokenise(expr)
    depth = 0
    for i, t in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
            if depth < 0:
                problems.append("unbalanced parentheses")
                break
        elif t.kind == "id":
            low = t.text.lower()
            is_call = nxt is not None and nxt.text == "("
            if low in KEYWORDS:
                continue
            if low in BANNED:
                problems.append(f"{t.text!r} is not allowed in an expression")
                continue
            if is_call and low not in FUNCTIONS:
                problems.append(f"function {t.text!r} is not in the portable subset")
    if depth > 0:
        problems.append("unbalanced parentheses")
    return problems


def identifiers(expr: str) -> list[str]:
    """Column references in ``expr`` (qualified names kept whole), in order of appearance."""
    tokens, _ = tokenise(expr)
    out: list[str] = []
    for i, t in enumerate(tokens):
        if t.kind != "id" or t.text.lower() in KEYWORDS:
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is not None and nxt.text == "(":
            continue
        if t.text not in out:
            out.append(t.text)
    return out


def functions(expr: str) -> set[str]:
    tokens, _ = tokenise(expr)
    found: set[str] = set()
    for i, t in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if (
            t.kind == "id"
            and nxt is not None
            and nxt.text == "("
            and t.text.lower() not in KEYWORDS
        ):
            found.add(t.text.lower())
    return found


def is_aggregate(expr: str) -> bool:
    return bool(functions(expr) & AGGREGATES)


def is_count_only(expr: str) -> bool:
    """True when the only thing the expression exposes about its inputs is a count."""
    fns = functions(expr)
    return bool(fns) and fns <= {"count"}
