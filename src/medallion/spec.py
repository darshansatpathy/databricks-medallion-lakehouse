"""The lakehouse specification: typed model and loader.

A specification is a ``platform.toml`` plus one TOML file per table under ``tables/``. The loader
never raises on bad input; it returns the problems it found so they can be reported together
(rule MED001) instead of one at a time.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LAYERS = ("bronze", "silver", "gold")
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
ACTIONS = ("warn", "drop", "fail")
MASKS = ("email", "redact", "last4", "hash")


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    comment: str
    expr: str | None = None
    pii: bool = False
    mask: str | None = None
    group: bool = False
    nullable: bool = True


@dataclass(frozen=True)
class Expectation:
    name: str
    rule: str
    action: str


@dataclass(frozen=True)
class Source:
    format: str
    path: str
    options: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Cdc:
    keys: tuple[str, ...]
    sequence_by: str
    scd: int = 1
    delete_when: str | None = None
    drop_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Join:
    table: str
    alias: str
    on: str
    kind: str = "inner"


@dataclass(frozen=True)
class Query:
    table: str
    alias: str
    joins: tuple[Join, ...] = ()
    where: str | None = None


@dataclass(frozen=True)
class Table:
    name: str
    layer: str
    owner: str
    domain: str
    classification: str
    comment: str
    columns: tuple[Column, ...]
    expectations: tuple[Expectation, ...] = ()
    source: Source | None = None
    upstream: str | None = None
    cdc: Cdc | None = None
    query: Query | None = None
    file: str = ""

    @property
    def fqn(self) -> str:
        return f"{self.layer}.{self.name}"

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


BRONZE_METADATA = (
    ("_source_file", "string"),
    ("_source_modified_at", "timestamp"),
    ("_ingested_at", "timestamp"),
    ("_rescued_data", "string"),
)
SCD2_COLUMNS = ("__START_AT", "__END_AT")


@dataclass(frozen=True)
class Group:
    name: str
    role: str  # consumer | engineer | privileged | runner | admin


@dataclass(frozen=True)
class Grant:
    group: str
    schema: str
    privileges: tuple[str, ...]
    envs: tuple[str, ...]


@dataclass(frozen=True)
class Platform:
    name: str
    description: str
    catalog_pattern: str
    environments: tuple[str, ...]
    landing_volume: str
    owners: tuple[str, ...]
    groups: tuple[Group, ...]
    grants: tuple[Grant, ...]

    def catalog(self, env: str) -> str:
        return self.catalog_pattern.replace("{env}", env)

    def group_by_role(self, role: str) -> list[Group]:
        return [g for g in self.groups if g.role == role]


@dataclass(frozen=True)
class Lakehouse:
    platform: Platform
    tables: tuple[Table, ...]

    def table(self, fqn: str) -> Table | None:
        return next((t for t in self.tables if t.fqn == fqn), None)

    def layer(self, layer: str) -> list[Table]:
        return [t for t in self.tables if t.layer == layer]


class _Reader:
    """Typed access to a TOML mapping that records problems with their location."""

    def __init__(self, data: Any, where: str, problems: list[str]) -> None:
        self.data = data if isinstance(data, dict) else {}
        self.where = where
        self.problems = problems
        if not isinstance(data, dict):
            problems.append(f"{where}: expected a table")

    def _p(self, key: str, msg: str) -> None:
        self.problems.append(f"{self.where}: '{key}' {msg}")

    def text(self, key: str, default: str | None = None) -> str:
        v = self.data.get(key, default)
        if v is None:
            self._p(key, "is required")
            return ""
        if not isinstance(v, str):
            self._p(key, "must be a string")
            return ""
        return v

    def opt_str(self, key: str) -> str | None:
        v = self.data.get(key)
        if v is None:
            return None
        if not isinstance(v, str):
            self._p(key, "must be a string")
            return None
        return v

    def flag(self, key: str, default: bool = False) -> bool:
        v = self.data.get(key, default)
        if not isinstance(v, bool):
            self._p(key, "must be true or false")
            return default
        return v

    def strs(self, key: str, required: bool = True) -> tuple[str, ...]:
        v = self.data.get(key)
        if v is None:
            if required:
                self._p(key, "is required")
            return ()
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            self._p(key, "must be a list of strings")
            return ()
        return tuple(v)

    def rows(self, key: str) -> list[Any]:
        v = self.data.get(key, [])
        if not isinstance(v, list):
            self._p(key, "must be an array of tables")
            return []
        return v

    def sub(self, key: str) -> _Reader | None:
        v = self.data.get(key)
        if v is None:
            return None
        return _Reader(v, f"{self.where}.{key}", self.problems)


def _columns(r: _Reader) -> tuple[Column, ...]:
    out = []
    for i, raw in enumerate(r.rows("columns")):
        c = _Reader(raw, f"{r.where} column[{i}]", r.problems)
        out.append(
            Column(
                name=c.text("name"),
                type=c.text("type", "string"),
                comment=c.text("comment"),
                expr=c.opt_str("expr"),
                pii=c.flag("pii"),
                mask=c.opt_str("mask"),
                group=c.flag("group"),
                nullable=c.flag("nullable", True),
            )
        )
    return tuple(out)


def _table(path: Path, problems: list[str]) -> Table | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problems.append(f"{path.name}: {exc}")
        return None
    root = _Reader(data, path.name, problems)
    t = _Reader(data.get("table"), f"{path.name} [table]", problems)
    layer = t.text("layer")
    source = cdc = query = None
    if (s := root.sub("source")) is not None:
        opts = s.data.get("options", {})
        if not isinstance(opts, dict) or not all(isinstance(v, str) for v in opts.values()):
            problems.append(f"{s.where}: 'options' must be a table of strings")
            opts = {}
        source = Source(s.text("format"), s.text("path"), dict(opts))
    if (c := root.sub("cdc")) is not None:
        scd = c.data.get("scd", 1)
        if isinstance(scd, bool) or not isinstance(scd, int):
            problems.append(f"{c.where}: 'scd' must be 1 or 2")
            scd = 1
        cdc = Cdc(
            keys=c.strs("keys"),
            sequence_by=c.text("sequence_by"),
            scd=scd,
            delete_when=c.opt_str("delete_when"),
            drop_columns=c.strs("drop_columns", required=False),
        )
    if (q := root.sub("query")) is not None:
        joins = []
        for i, raw in enumerate(q.rows("joins")):
            j = _Reader(raw, f"{q.where}.joins[{i}]", problems)
            joins.append(
                Join(j.text("table"), j.text("alias"), j.text("on"), j.text("kind", "inner"))
            )
        query = Query(q.text("from"), q.text("alias"), tuple(joins), q.opt_str("where"))
    expectations = []
    for i, raw in enumerate(root.rows("expectations")):
        e = _Reader(raw, f"{path.name} expectation[{i}]", problems)
        expectations.append(Expectation(e.text("name"), e.text("rule"), e.text("action", "drop")))
    return Table(
        name=t.text("name"),
        layer=layer,
        owner=t.text("owner"),
        domain=t.text("domain"),
        classification=t.text("classification"),
        comment=t.text("comment"),
        columns=_columns(root),
        expectations=tuple(expectations),
        source=source,
        upstream=t.opt_str("from"),
        cdc=cdc,
        query=query,
        file=path.name,
    )


def _platform(path: Path, problems: list[str]) -> Platform | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problems.append(f"{path.name}: {exc}")
        return None
    root = _Reader(data, path.name, problems)
    p = _Reader(data.get("platform"), f"{path.name} [platform]", problems)
    groups = []
    for i, raw in enumerate(root.rows("groups")):
        g = _Reader(raw, f"{path.name} group[{i}]", problems)
        groups.append(Group(g.text("name"), g.text("role")))
    grants = []
    for i, raw in enumerate(root.rows("grants")):
        g = _Reader(raw, f"{path.name} grant[{i}]", problems)
        grants.append(
            Grant(g.text("group"), g.text("schema"), g.strs("privileges"), g.strs("envs"))
        )
    owners = []
    for i, raw in enumerate(root.rows("owners")):
        owners.append(_Reader(raw, f"{path.name} owner[{i}]", problems).text("id"))
    return Platform(
        name=p.text("name"),
        description=p.text("description"),
        catalog_pattern=p.text("catalog_pattern"),
        environments=p.strs("environments"),
        landing_volume=p.text("landing_volume"),
        owners=tuple(owners),
        groups=tuple(groups),
        grants=tuple(grants),
    )


def load(root: Path) -> tuple[Lakehouse | None, list[str]]:
    """Load ``root/platform.toml`` and ``root/tables/*.toml``; return (spec, problems)."""
    problems: list[str] = []
    plat_path = root / "platform.toml"
    if not plat_path.exists():
        return None, [f"{plat_path} does not exist"]
    platform = _platform(plat_path, problems)
    tables: list[Table] = []
    for f in sorted((root / "tables").glob("*.toml")):
        t = _table(f, problems)
        if t is not None:
            tables.append(t)
    if platform is None:
        return None, problems
    return Lakehouse(platform, tuple(tables)), problems


def target_columns(t: Table) -> list[tuple[str, str]]:
    """Name and type of every column the table has once built, including system columns."""
    if t.layer == "bronze":
        return (
            [(c.name, c.type) for c in t.columns] + list(BRONZE_METADATA[:3]) + [BRONZE_METADATA[3]]
        )
    if t.layer == "silver":
        dropped = set(t.cdc.drop_columns) if t.cdc else set()
        cols = [(c.name, c.type) for c in t.columns if c.name not in dropped]
        cols += [("_source_file", "string"), ("_ingested_at", "timestamp")]
        if t.cdc and t.cdc.scd == 2:
            seq = next((c.type for c in t.columns if c.name == t.cdc.sequence_by), "timestamp")
            cols += [(n, seq) for n in SCD2_COLUMNS]
        return cols
    return [(c.name, c.type) for c in t.columns]
