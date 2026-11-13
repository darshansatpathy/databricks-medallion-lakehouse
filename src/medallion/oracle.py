"""A reference executor for the specification, used to test semantics without Spark.

It builds the lakehouse from sample files and applies the same rules the generated SQL
expresses: text-only bronze, typed silver with expectations, quarantine, key-and-sequence change
handling (type 1 and type 2), and gold aggregates.

How it is honest about what it is: expressions run on SQLite, which is why the expression
subset in :mod:`medallion.exprcheck` exists, and ``try_cast`` is a Python port of the rules the
SQL relies on. It is a model of the pipeline, not the pipeline. The assumptions it makes about
the Databricks runtime are listed in ``docs/oracle-assumptions.md`` and in :data:`ASSUMPTIONS`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import exprcheck, spec, types
from .spec import Lakehouse, Table

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ASSUMPTIONS = [
    "An expectation passes only when its expression is true; NULL counts as a violation.",
    "A drop expectation removes the row; a fail expectation stops the update; warn keeps the row and counts it.",
    "Events for a key are applied in sequence order whatever their arrival order. An exact redelivery (same key, sequence and values) is collapsed; two different events with the same key and sequence are ambiguous and the oracle refuses them.",
    "A type 2 event whose tracked columns equal the open version does not open a new version.",
    "A delete closes the open version of a type 2 key and removes a type 1 key; a later insert starts again.",
    "Timestamps without a zone are read as UTC (the pipeline sets spark.sql.session.timeZone to UTC).",
    "Decimals are held as floats, rounded half-up to scale on cast, so aggregates are compared to the cent.",
]


class ExpectationFailure(Exception):
    def __init__(self, table: str, rule: str, count: int) -> None:
        super().__init__(f"{table}: expectation {rule} (fail) violated by {count} row(s)")
        self.table, self.rule, self.count = table, rule, count


class AmbiguousOrdering(Exception):
    pass


@dataclass(frozen=True)
class Metric:
    table: str
    name: str
    action: str
    passed: int
    failed: int


@dataclass
class Result:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metrics: list[Metric] = field(default_factory=list)

    def rows(self, fqn: str) -> list[dict[str, Any]]:
        return self.tables[fqn]

    def metric(self, table: str, name: str) -> Metric:
        return next(m for m in self.metrics if m.table == table and m.name == name)


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def _sqlite_value(v: Any) -> Any:
    return int(v) if isinstance(v, bool) else v


class Engine:
    """SQLite with one attached database per layer and Spark-like scalar functions."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for layer in spec.LAYERS:
            self.conn.execute(f"ATTACH DATABASE ':memory:' AS {layer}")
        c = self.conn
        c.create_function("to_date", 1, _to_date, deterministic=True)
        c.create_function(
            "lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True
        )
        c.create_function(
            "upper", 1, lambda s: s.upper() if isinstance(s, str) else s, deterministic=True
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create(self, fqn: str, columns: list[str], rows: list[list[Any]]) -> None:
        layer, name = fqn.split(".")
        cols = ", ".join(_ident(x) for x in columns)
        self.conn.execute(f"CREATE TABLE {_ident(layer)}.{_ident(name)} ({cols})")
        marks = ", ".join("?" for _ in columns)
        self.conn.executemany(
            f"INSERT INTO {layer}.{name} VALUES ({marks})",
            [[_sqlite_value(v) for v in r] for r in rows],
        )

    def create_temp(self, name: str, columns: list[str], rows: list[list[Any]]) -> None:
        self.conn.execute(
            f"CREATE TEMP TABLE {_ident(name)} ({', '.join(_ident(x) for x in columns)})"
        )
        marks = ", ".join("?" for _ in columns)
        self.conn.executemany(
            f"INSERT INTO temp.{name} VALUES ({marks})",
            [[_sqlite_value(v) for v in r] for r in rows],
        )

    def drop_temp(self, name: str) -> None:
        self.conn.execute(f"DROP TABLE temp.{_ident(name)}")

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        return list(self.conn.execute(sql))


def _to_date(v: Any) -> str | None:
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


# ----------------------------------------------------------------------------- bronze


def _as_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict | list):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    return str(v)


def read_source(root: Path, t: Table) -> list[tuple[str, dict[str, Any] | str]]:
    """Records for a bronze table in arrival order: (file name, parsed object or raw bad line)."""
    assert t.source is not None
    folder = root / t.source.path
    out: list[tuple[str, dict[str, Any] | str]] = []
    for f in sorted(folder.glob(f"*.{'jsonl' if t.source.format == 'json' else t.source.format}")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out.append((f.name, line))
                continue
            out.append((f.name, obj if isinstance(obj, dict) else line))
    return out


def build_bronze(
    t: Table, records: list[tuple[str, dict[str, Any] | str]], ingested_at: str, catalog: str
) -> list[dict[str, Any]]:
    assert t.source is not None
    declared = [c.name for c in t.columns]
    rows = []
    for fname, rec in records:
        row: dict[str, Any] = dict.fromkeys(declared)
        rescued: dict[str, Any] = {}
        if isinstance(rec, dict):
            for k, v in rec.items():
                if k in row:
                    row[k] = _as_text(v)
                else:
                    rescued[k] = v
        else:
            rescued["_corrupt_record"] = rec
        row["_source_file"] = f"/Volumes/{catalog}/landing/raw/{t.source.path}/{fname}"
        row["_source_modified_at"] = ingested_at
        row["_ingested_at"] = ingested_at
        row["_rescued_data"] = (
            json.dumps(rescued, sort_keys=True, separators=(",", ":")) if rescued else None
        )
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- silver


def _eval_column(eng: Engine, fqn: str, exprs: list[str], extra: str = "") -> list[tuple[Any, ...]]:
    for e in exprs:
        if exprcheck.check(e):
            raise ValueError(f"unsafe expression {e!r}")
    items = ", ".join(f"({e})" for e in exprs)
    return eng.query(f"SELECT {items}{extra} FROM {fqn} ORDER BY rowid")


def build_silver(
    eng: Engine, t: Table, bronze_rows: list[dict[str, Any]], result: Result
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assert t.cdc is not None and t.upstream is not None
    # typed rows
    raw = _eval_column(eng, t.upstream, [c.expr or c.name for c in t.columns])
    typed: list[dict[str, Any]] = []
    for r, src in zip(raw, bronze_rows, strict=True):
        row = {c.name: types.try_cast(v, c.type) for c, v in zip(t.columns, r, strict=True)}
        row["_source_file"] = src["_source_file"]
        row["_ingested_at"] = src["_ingested_at"]
        typed.append(row)
    names = [c.name for c in t.columns] + ["_source_file", "_ingested_at"]
    tmp = f"_t_{t.layer}_{t.name}"
    eng.create_temp(tmp, names, [[r[n] for n in names] for r in typed])
    # expectations
    outcomes: dict[str, list[bool]] = {}
    try:
        for e in t.expectations:
            if exprcheck.check(e.rule):
                raise ValueError(f"unsafe expectation {e.rule!r}")
            vals = eng.query(f"SELECT ({e.rule}) FROM temp.{tmp} ORDER BY rowid")
            outcomes[e.name] = [v[0] == 1 for v in vals]
            failed = outcomes[e.name].count(False)
            result.metrics.append(Metric(t.fqn, e.name, e.action, len(typed) - failed, failed))
            if e.action == "fail" and failed:
                raise ExpectationFailure(t.fqn, e.name, failed)
        deletes = (
            [
                bool(v[0] == 1)
                for v in eng.query(f"SELECT ({t.cdc.delete_when}) FROM temp.{tmp} ORDER BY rowid")
            ]
            if t.cdc.delete_when and not exprcheck.check(t.cdc.delete_when)
            else [False] * len(typed)
        )
    finally:
        eng.drop_temp(tmp)
    drop_rules = [e for e in t.expectations if e.action == "drop"]
    valid: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    valid_deletes: list[bool] = []
    for i, row in enumerate(typed):
        failed_rules = [e.name for e in drop_rules if not outcomes[e.name][i]]
        if failed_rules:
            quarantine.append({**row, "failed_rules": failed_rules})
        else:
            valid.append(row)
            valid_deletes.append(deletes[i])
    return _apply_changes(t, valid, valid_deletes), quarantine


def _apply_changes(
    t: Table, rows: list[dict[str, Any]], deletes: list[bool]
) -> list[dict[str, Any]]:
    assert t.cdc is not None
    cdc = t.cdc
    dropped = set(cdc.drop_columns)
    out_cols = [c.name for c in t.columns if c.name not in dropped]
    keep = [*out_cols, "_source_file", "_ingested_at"]
    groups: dict[tuple[Any, ...], list[tuple[Any, dict[str, Any], bool]]] = {}
    for row, is_delete in zip(rows, deletes, strict=True):
        groups.setdefault(tuple(row[k] for k in cdc.keys), []).append(
            (row[cdc.sequence_by], row, is_delete)
        )
    result: list[dict[str, Any]] = []
    tracked = [c for c in out_cols if c != cdc.sequence_by]
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        events = _collapse_duplicates(t, key, sorted(groups[key], key=lambda e: e[0]), tracked)
        if cdc.scd == 1:
            _seq, row, is_delete = events[-1]
            if not is_delete:
                result.append({k: row[k] for k in keep})
            continue
        open_row: dict[str, Any] | None = None
        for seq, row, is_delete in events:
            if is_delete:
                if open_row is not None:
                    open_row["__END_AT"] = seq
                    open_row = None
                continue
            if open_row is not None and all(open_row[c] == row[c] for c in tracked):
                continue
            if open_row is not None:
                open_row["__END_AT"] = seq
            open_row = {k: row[k] for k in keep}
            open_row["__START_AT"] = seq
            open_row["__END_AT"] = None
            result.append(open_row)
    return result


def _collapse_duplicates(
    t: Table,
    key: tuple[Any, ...],
    events: list[tuple[Any, dict[str, Any], bool]],
    tracked: list[str],
) -> list[tuple[Any, dict[str, Any], bool]]:
    """Merge redelivered events; refuse two different events that share a sequence value."""
    out: list[tuple[Any, dict[str, Any], bool]] = []
    for seq, row, is_delete in events:
        if out and out[-1][0] == seq:
            prev = out[-1]
            same = prev[2] == is_delete and all(prev[1][c] == row[c] for c in tracked)
            if not same:
                raise AmbiguousOrdering(
                    f"{t.fqn}: key {key} has two different events with sequence {seq}; ordering is undefined"
                )
            continue  # an exact redelivery: keep the first
        out.append((seq, row, is_delete))
    return out


# ----------------------------------------------------------------------------- gold


def gold_query(t: Table) -> str:
    """The gold SELECT in the portable subset, with schema-qualified names."""
    assert t.query is not None
    q = t.query
    sel = ", ".join(f"({c.expr}) AS {c.name}" for c in t.columns)
    sql = f"SELECT {sel} FROM {q.table} AS {q.alias}"
    for j in q.joins:
        sql += f" {j.kind.upper()} JOIN {j.table} AS {j.alias} ON {j.on}"
    if q.where:
        sql += f" WHERE {q.where}"
    group = [c.expr for c in t.columns if c.group and c.expr]
    if group:
        sql += " GROUP BY " + ", ".join(group)
    return sql


def build_gold(eng: Engine, t: Table) -> list[dict[str, Any]]:
    for _, ex in [(c.name, c.expr or "") for c in t.columns]:
        if exprcheck.check(ex):
            raise ValueError(f"unsafe expression {ex!r}")
    rows = []
    for r in eng.query(gold_query(t)):
        rows.append({c.name: types.try_cast(v, c.type) for c, v in zip(t.columns, r, strict=True)})
    grain = [c.name for c in t.columns if c.group]
    rows.sort(key=lambda r: tuple(str(r[g]) for g in grain))
    return rows


# ----------------------------------------------------------------------------- run


def run(
    lh: Lakehouse,
    data_root: Path,
    env: str = "dev",
    ingested_at: str = "2026-11-13T00:00:00.000000Z",
) -> Result:
    """Build every table from the sample data under ``data_root`` and return the result."""
    catalog = lh.platform.catalog(env)
    with Engine() as eng:
        result = Result()
        bronze: dict[str, list[dict[str, Any]]] = {}
        for t in lh.layer("bronze"):
            rows = build_bronze(t, read_source(data_root, t), ingested_at, catalog)
            bronze[t.fqn] = rows
            names = [n for n, _ in spec.target_columns(t)]
            eng.create(t.fqn, names, [[r[n] for n in names] for r in rows])
            bad = sum(1 for r in rows if r["_rescued_data"] is not None)
            result.metrics.append(Metric(t.fqn, "rescued_data_empty", "warn", len(rows) - bad, bad))
            result.tables[t.fqn] = rows
        for t in lh.layer("silver"):
            assert t.upstream is not None
            current, quarantine = build_silver(eng, t, bronze[t.upstream], result)
            names = [n for n, _ in spec.target_columns(t)]
            eng.create(t.fqn, names, [[r.get(n) for n in names] for r in current])
            result.tables[t.fqn] = current
            result.tables[f"{t.fqn}_quarantine"] = quarantine
        for t in lh.layer("gold"):
            rows = build_gold(eng, t)
            names = [c.name for c in t.columns]
            eng.create(t.fqn, names, [[r[n] for n in names] for r in rows])
            result.tables[t.fqn] = rows
        return result


# ----------------------------------------------------------------------------- masks


def apply_mask(kind: str, value: str | None, privileged: bool) -> str | None:
    """What a reader sees: the real value for the privileged and runner groups, else the mask."""
    if value is None or privileged:
        return value
    if kind == "email":
        return "***@" + value.rsplit("@", 1)[-1]
    if kind == "redact":
        return "[REDACTED]"
    if kind == "last4":
        return "*" * max(len(value) - 4, 0) + value[-4:]
    if kind == "hash":
        return hashlib.sha256(value.encode()).hexdigest()
    raise ValueError(f"unknown mask {kind!r}")


def view_as(
    lh: Lakehouse, fqn: str, rows: list[dict[str, Any]], privileged: bool
) -> list[dict[str, Any]]:
    t = lh.table(fqn.removesuffix("_quarantine").removesuffix("_validated"))
    if t is None:
        raise KeyError(fqn)
    masks = {c.name: c.mask for c in t.columns if c.mask}
    return [
        {k: (apply_mask(masks[k], v, privileged) if k in masks else v) for k, v in r.items()}
        for r in rows
    ]
