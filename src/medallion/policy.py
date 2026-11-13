"""Policy checks over the specification.

These are the rules a design must satisfy before any SQL is generated. Each rule has an ID, a
severity and a failing-case test; the test suite asserts that no rule goes unexercised.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import exprcheck, spec, types
from .spec import Lakehouse, Table

SEVERITIES = ("low", "medium", "high", "critical")
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_RANK = {c: i for i, c in enumerate(spec.CLASSIFICATIONS)}
SCHEMAS = ("landing", "bronze", "silver", "gold", "governance")
PRIVILEGES = {
    "SELECT", "MODIFY", "EXECUTE", "READ VOLUME", "WRITE VOLUME", "ALL PRIVILEGES",
    "CREATE TABLE", "CREATE VOLUME", "CREATE FUNCTION",
}  # fmt: skip
SOURCE_FORMATS = ("json", "csv", "parquet")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    subject: str
    message: str


@dataclass(frozen=True)
class Waiver:
    rule: str
    subject: str
    reason: str
    expires: date


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    waived: list[tuple[Finding, Waiver]] = field(default_factory=list)
    rules_run: int = 0
    tables: int = 0

    def failing(self, threshold: str) -> list[Finding]:
        floor = SEVERITIES.index(threshold)
        return [f for f in self.findings if SEVERITIES.index(f.severity) >= floor]


RuleFunc = Callable[[Lakehouse], Iterator[tuple[str, str]]]


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    why: str
    func: RuleFunc


REGISTRY: list[Rule] = []


def rule(rule_id: str, severity: str, title: str, why: str) -> Callable[[RuleFunc], RuleFunc]:
    def deco(fn: RuleFunc) -> RuleFunc:
        REGISTRY.append(Rule(rule_id, severity, title, why, fn))
        return fn

    return deco


# ---------------------------------------------------------------------------- helpers


def _alias_map(t: Table) -> dict[str, str]:
    """alias -> table fqn for a gold table's query."""
    if t.query is None:
        return {}
    m = {t.query.alias: t.query.table}
    for j in t.query.joins:
        m[j.alias] = j.table
    return m


def _exprs(t: Table) -> Iterator[tuple[str, str]]:
    """Every expression in a table, with a label for messages."""
    for c in t.columns:
        if c.expr:
            yield f"column {c.name}", c.expr
    for e in t.expectations:
        yield f"expectation {e.name}", e.rule
    if t.cdc and t.cdc.delete_when:
        yield "cdc.delete_when", t.cdc.delete_when
    if t.query:
        if t.query.where:
            yield "query.where", t.query.where
        for j in t.query.joins:
            yield f"join {j.alias}", j.on


def _col_names(t: Table) -> set[str]:
    return {n for n, _ in spec.target_columns(t)}


def _rank(c: str) -> int:
    return _RANK.get(c, -1)


# ---------------------------------------------------------------------------- rules


@rule(
    "MED002",
    "critical",
    "Every reference must resolve",
    "A dangling reference generates SQL that fails at deploy time.",
)
def refs(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    plat = lh.platform
    names = {g.name for g in plat.groups}
    for g in plat.grants:
        if g.group not in names:
            yield g.group, "grant refers to a group that is not declared"
        if g.schema not in SCHEMAS:
            yield g.group, f"grant refers to unknown schema {g.schema!r}"
        for e in g.envs:
            if e not in plat.environments:
                yield g.group, f"grant refers to unknown environment {e!r}"
    for t in lh.tables:
        if t.owner not in plat.owners:
            yield t.fqn, f"owner {t.owner!r} is not declared in platform.toml"
        if t.upstream and lh.table(t.upstream) is None:
            yield t.fqn, f"upstream table {t.upstream!r} does not exist"
        cols = {c.name for c in t.columns}
        if t.cdc:
            for k in t.cdc.keys:
                if k not in cols:
                    yield t.fqn, f"cdc key {k!r} is not a column"
            if t.cdc.sequence_by not in cols:
                yield t.fqn, f"cdc.sequence_by {t.cdc.sequence_by!r} is not a column"
            for d in t.cdc.drop_columns:
                if d not in cols:
                    yield t.fqn, f"cdc.drop_columns names {d!r}, which is not a column"
        if t.query:
            for alias, ref in _alias_map(t).items():
                if lh.table(ref) is None:
                    yield t.fqn, f"query uses {ref!r} (alias {alias}), which does not exist"
        if t.layer == "silver" and t.upstream:
            up = lh.table(t.upstream)
            if up is not None:
                for c in t.columns:
                    for ident in exprcheck.identifiers(c.expr or ""):
                        if ident not in {x.name for x in up.columns}:
                            yield t.fqn, f"column {c.name} uses {ident!r}, which {up.fqn} lacks"


@rule(
    "MED003",
    "high",
    "Data flows bronze to silver to gold, with no cycles",
    "Layer skipping and cycles make lineage and recovery ambiguous.",
)
def layering(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    order = {"bronze": 0, "silver": 1, "gold": 2}
    for t in lh.tables:
        if t.layer not in order:
            continue
        if t.layer == "bronze" and (t.upstream or t.query):
            yield t.fqn, "bronze reads from landing files only"
        if t.layer == "silver" and t.upstream:
            up = lh.table(t.upstream)
            if up is not None and up.layer != "bronze":
                yield t.fqn, f"silver must read from bronze, not {up.layer}"
        if t.layer == "gold":
            if t.upstream:
                yield t.fqn, "gold uses [query], not 'from' in [table]"
            for ref in _alias_map(t).values():
                up = lh.table(ref)
                if up is not None and up.layer == "bronze":
                    yield t.fqn, f"gold may not read bronze directly ({ref})"
                if up is not None and up.layer == "gold" and up.fqn == t.fqn:
                    yield t.fqn, "gold table reads itself"


@rule(
    "MED004",
    "high",
    "Bronze is raw, append-only text",
    "Typing at ingestion turns source drift into pipeline failures; raw text survives it.",
)
def bronze(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.layer("bronze"):
        if t.source is None:
            yield t.fqn, "bronze needs a [source] section"
            continue
        if t.source.format not in SOURCE_FORMATS:
            yield t.fqn, f"source format {t.source.format!r} is not one of {SOURCE_FORMATS}"
        if not re.fullmatch(r"[a-z0-9_]+(/[a-z0-9_]+)*", t.source.path):
            yield t.fqn, "source path must be lower-case letters, digits, '_' and '/'"
        for c in t.columns:
            if types.normalise(c.type) != "string":
                yield t.fqn, f"column {c.name} is {c.type}; bronze columns are string"
            if c.name.startswith("_"):
                yield t.fqn, f"column {c.name} uses the prefix reserved for system columns"
            if c.expr:
                yield t.fqn, f"column {c.name} has an expr; bronze does not transform"
        if t.expectations:
            yield t.fqn, "bronze takes no expectations; validate in silver"


@rule(
    "MED005",
    "high",
    "Silver is typed, validated and keyed",
    "Silver is the trusted layer: every row has a key, a sequence and a reason to be there.",
)
def silver(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.layer("silver"):
        if t.cdc is None:
            yield t.fqn, "silver needs a [cdc] section"
            continue
        if t.upstream is None:
            yield t.fqn, "silver needs 'from' naming a bronze table"
        if t.cdc.scd not in (1, 2):
            yield t.fqn, "cdc.scd must be 1 or 2"
        if not t.cdc.keys:
            yield t.fqn, "cdc.keys must not be empty"
        seq = t.column(t.cdc.sequence_by)
        if seq is not None and types.normalise(seq.type) not in (
            "timestamp",
            "date",
            "bigint",
            "int",
        ):
            yield (
                t.fqn,
                f"sequence column {seq.name} is {seq.type}; it must sort (timestamp, date, int)",
            )
        for c in t.columns:
            if not c.expr:
                yield t.fqn, f"column {c.name} has no expr; silver states every transformation"
        strong = [e for e in t.expectations if e.action in ("drop", "fail")]
        for must in (*t.cdc.keys, t.cdc.sequence_by):
            covered = any(
                re.search(rf"\b{re.escape(must)}\s+is\s+not\s+null\b", e.rule, re.I) for e in strong
            )
            if not covered:
                yield t.fqn, f"no drop or fail expectation requires {must} IS NOT NULL"
        if not any(e.action == "fail" for e in t.expectations) and t.cdc.delete_when:
            yield (
                t.fqn,
                "a table that applies deletes needs a fail expectation on its operation column",
            )


@rule(
    "MED006",
    "high",
    "Gold is a documented aggregate at a declared grain",
    "A gold table without a grain cannot be reconciled or joined safely.",
)
def gold(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.layer("gold"):
        if t.query is None:
            yield t.fqn, "gold needs a [query] section"
            continue
        aliases = [t.query.alias] + [j.alias for j in t.query.joins]
        if len(set(aliases)) != len(aliases):
            yield t.fqn, "query aliases must be unique"
        for j in t.query.joins:
            if j.kind not in ("inner", "left"):
                yield t.fqn, f"join {j.alias}: kind must be inner or left"
        amap = _alias_map(t)
        groups = [c for c in t.columns if c.group]
        if not any(not c.group for c in t.columns) and not groups:
            yield t.fqn, "gold has no columns"
        for c in t.columns:
            if not c.expr:
                yield t.fqn, f"column {c.name} has no expr"
                continue
            agg = exprcheck.is_aggregate(c.expr)
            if c.group and agg:
                yield t.fqn, f"grain column {c.name} is an aggregate"
            if not c.group and not agg:
                yield t.fqn, f"column {c.name} is neither a grain column nor an aggregate"
        for label, ex in _exprs(t):
            if label.startswith("expectation"):
                continue
            for ident in exprcheck.identifiers(ex):
                alias, _, col = ident.partition(".")
                if not col:
                    yield t.fqn, f"{label}: {ident!r} must be qualified with a table alias"
                elif alias not in amap:
                    yield t.fqn, f"{label}: alias {alias!r} is not in the query"
                else:
                    up = lh.table(amap[alias])
                    if up is not None and col not in _col_names(up):
                        yield t.fqn, f"{label}: {amap[alias]} has no column {col!r}"


@rule(
    "MED007",
    "critical",
    "Personal data is masked and never reaches an unmasked column",
    "A mask on the source is worthless if a derived column copies the raw value.",
)
def pii(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.tables:
        for c in t.columns:
            if c.pii:
                if c.mask is None:
                    yield f"{t.fqn}.{c.name}", "personal data column has no mask"
                elif c.mask not in spec.MASKS:
                    yield f"{t.fqn}.{c.name}", f"unknown mask {c.mask!r}; use one of {spec.MASKS}"
                if types.normalise(c.type) != "string":
                    yield f"{t.fqn}.{c.name}", "masks apply to string columns only"
                if _rank(t.classification) < _rank("confidential"):
                    yield (
                        f"{t.fqn}.{c.name}",
                        f"table is {t.classification}; a table with personal data is confidential or restricted",
                    )
            elif c.mask is not None:
                yield f"{t.fqn}.{c.name}", "column has a mask but is not marked pii"
    for t in lh.layer("gold"):
        amap = _alias_map(t)
        for c in t.columns:
            if not c.expr or c.pii:
                continue
            for ident in exprcheck.identifiers(c.expr):
                alias, _, col = ident.partition(".")
                up = lh.table(amap.get(alias, ""))
                src = up.column(col) if up else None
                if src is not None and src.pii and not exprcheck.is_count_only(c.expr):
                    yield (
                        f"{t.fqn}.{c.name}",
                        f"derives from personal data {amap[alias]}.{col} but is not marked pii with a mask",
                    )


@rule(
    "MED008",
    "medium",
    "Every table has an owner, a domain, a classification and a description",
    "Unowned, undocumented data is where incidents go to wait.",
)
def metadata(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.tables:
        if t.classification not in spec.CLASSIFICATIONS:
            yield t.fqn, f"classification {t.classification!r} is not one of {spec.CLASSIFICATIONS}"
        if len(t.comment.strip()) < 30:
            yield t.fqn, "table comment must be at least 30 characters"
        if not t.domain or not _SNAKE.match(t.domain):
            yield t.fqn, "domain must be lower_snake_case"
        for c in t.columns:
            if len(c.comment.strip()) < 5:
                yield f"{t.fqn}.{c.name}", "column comment must be at least 5 characters"


@rule(
    "MED009",
    "high",
    "Expectations are named, graded and refer to real columns",
    "An expectation nobody can read or that checks nothing is false assurance.",
)
def expectations(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.tables:
        seen: set[str] = set()
        cols = {c.name for c in t.columns}
        for e in t.expectations:
            if e.name in seen:
                yield t.fqn, f"expectation name {e.name!r} is used twice"
            seen.add(e.name)
            if not _SNAKE.match(e.name):
                yield t.fqn, f"expectation name {e.name!r} must be lower_snake_case"
            if e.action not in spec.ACTIONS:
                yield t.fqn, f"expectation {e.name}: action must be one of {spec.ACTIONS}"
            for ident in exprcheck.identifiers(e.rule):
                if ident not in cols:
                    yield t.fqn, f"expectation {e.name} refers to {ident!r}, which is not a column"
        if (
            t.layer == "silver"
            and t.expectations
            and all(e.action == "warn" for e in t.expectations)
        ):
            yield t.fqn, "every expectation only warns; silver needs at least one drop or fail"


@rule(
    "MED010",
    "high",
    "Readers of a type 2 table select the current version",
    "Joining all versions multiplies rows and double-counts.",
)
def scd2_consumers(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.layer("gold"):
        if t.query is None:
            continue
        for alias, ref in _alias_map(t).items():
            up = lh.table(ref)
            if up is None or not (up.cdc and up.cdc.scd == 2):
                continue
            text = " ".join(x for _, x in _exprs(t) if x)
            pattern = rf"\b{re.escape(alias)}\.__END_AT\s+is\s+null\b"
            if not re.search(pattern, text, re.I):
                yield t.fqn, f"reads type 2 table {ref} as {alias} without {alias}.__END_AT IS NULL"


@rule(
    "MED011",
    "critical",
    "Expressions stay inside the portable subset",
    "Spec text is spliced into SQL; the subset keeps it safe and means the same thing everywhere.",
)
def expression_safety(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.tables:
        for label, ex in _exprs(t):
            for p in exprcheck.check(ex):
                yield t.fqn, f"{label}: {p}"
        if t.layer == "gold":
            for c in t.columns:
                if c.group and not c.expr:
                    yield t.fqn, f"column {c.name}: grain column needs an expr"


@rule(
    "MED012",
    "high",
    "Access is granted to groups by role, least privilege",
    "Individual grants and broad privileges are the usual audit findings.",
)
def grants(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    roles = {g.name: g.role for g in lh.platform.groups}
    for g in lh.platform.groups:
        if "@" in g.name:
            yield g.name, "grant principals must be groups, not people"
        if g.role not in ("consumer", "engineer", "privileged", "runner", "admin"):
            yield g.name, f"unknown role {g.role!r}"
    for gr in lh.platform.grants:
        role = roles.get(gr.group)
        for p in gr.privileges:
            if p not in PRIVILEGES:
                yield gr.group, f"unknown privilege {p!r}"
        if "ALL PRIVILEGES" in gr.privileges and role not in ("runner", "admin"):
            yield gr.group, "ALL PRIVILEGES is for the runner and admin roles only"
        if role == "consumer" and (gr.schema != "gold" or gr.privileges != ("SELECT",)):
            yield gr.group, "consumers get SELECT on gold only"
        if role == "engineer" and gr.schema == "bronze" and "prod" in gr.envs:
            yield gr.group, "engineers do not read bronze in prod (it holds raw personal data)"
        if role == "engineer" and any(p not in ("SELECT",) for p in gr.privileges):
            yield gr.group, "engineers get SELECT only; changes go through the pipeline"
        if gr.schema == "governance" and role != "runner":
            yield gr.group, "only the runner role needs the governance schema"
        if role == "privileged" and gr.schema in ("landing", "bronze"):
            yield gr.group, "the privileged group reads curated layers only"
    if len(lh.platform.group_by_role("runner")) != 1:
        yield (
            "platform",
            "declare exactly one runner group; masks exempt it so the pipeline reads real values",
        )
    if not lh.platform.group_by_role("privileged"):
        yield (
            "platform",
            "declare a privileged group; masks need a group allowed to see real values",
        )


@rule(
    "MED013",
    "medium",
    "Identifiers are lower_snake_case",
    "One convention keeps SQL, grants and docs predictable.",
)
def naming(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.tables:
        if not _SNAKE.match(t.name):
            yield t.fqn, "table name must be lower_snake_case"
        if t.layer not in spec.LAYERS:
            yield t.fqn, f"layer {t.layer!r} must be one of {spec.LAYERS}"
        for c in t.columns:
            if not (_SNAKE.match(c.name) or c.name.startswith("_")):
                yield f"{t.fqn}.{c.name}", "column name must be lower_snake_case"
            if not types.is_valid(c.type):
                yield f"{t.fqn}.{c.name}", f"type {c.type!r} is not supported"
    for env in lh.platform.environments:
        if not _SNAKE.match(lh.platform.catalog(env)):
            yield "platform", f"catalog name for {env} must be lower_snake_case"


@rule(
    "MED014",
    "high",
    "Classification never drops below what it is built from",
    "A confidential source cannot become an internal table by being copied.",
)
def classification(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    for t in lh.layer("silver"):
        up = lh.table(t.upstream or "")
        if up and _rank(t.classification) < _rank(up.classification):
            yield t.fqn, f"{t.classification} is below its source {up.fqn} ({up.classification})"
    for t in lh.layer("gold"):
        amap = _alias_map(t)
        for c in t.columns:
            for ident in exprcheck.identifiers(c.expr or ""):
                alias, _, col = ident.partition(".")
                up = lh.table(amap.get(alias, ""))
                src = up.column(col) if up else None
                if up and src and src.pii and _rank(t.classification) < _rank(up.classification):
                    yield (
                        t.fqn,
                        f"uses personal data from {up.fqn} ({up.classification}) but is only {t.classification}",
                    )


@rule(
    "MED015",
    "high",
    "Names are unique and do not collide with generated tables",
    "Each table also yields _validated and _quarantine tables; a clash overwrites data.",
)
def uniqueness(lh: Lakehouse) -> Iterator[tuple[str, str]]:
    seen: set[str] = set()
    for t in lh.tables:
        if t.fqn in seen:
            yield t.fqn, "table is defined twice"
        seen.add(t.fqn)
        names = [c.name for c in t.columns]
        for n in {n for n in names if names.count(n) > 1}:
            yield t.fqn, f"column {n!r} is defined twice"
    generated = {
        f"silver.{t.name}{s}" for t in lh.layer("silver") for s in ("_validated", "_quarantine")
    }
    for t in lh.tables:
        if t.fqn in generated:
            yield t.fqn, "name collides with a generated table"


# ---------------------------------------------------------------------------- running


def load_waivers(path: Path) -> tuple[list[Waiver], list[str]]:
    import tomllib

    if not path.exists():
        return [], []
    problems: list[str] = []
    out: list[Waiver] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [], [f"{path.name}: {exc}"]
    for i, w in enumerate(data.get("waivers", [])):
        try:
            if len(str(w["reason"]).strip()) < 20:
                problems.append(f"waiver[{i}]: reason must be at least 20 characters")
            out.append(
                Waiver(w["rule"], w["subject"], w["reason"], date.fromisoformat(str(w["expires"])))
            )
        except (KeyError, ValueError) as exc:
            problems.append(f"waiver[{i}]: {exc!r}")
    return out, problems


def run(
    lh: Lakehouse | None, load_problems: list[str], today: date, waivers: list[Waiver] | None = None
) -> Report:
    report = Report()
    for p in load_problems:
        report.findings.append(Finding("MED001", "critical", "spec", p))
    if lh is None:
        report.rules_run = 1
        return report
    report.tables = len(lh.tables)
    raw: list[Finding] = []
    for r in REGISTRY:
        for subject, message in r.func(lh):
            raw.append(Finding(r.id, r.severity, subject, message))
    report.rules_run = len(REGISTRY) + 1
    for w in waivers or []:
        if w.expires < today:
            report.findings.append(
                Finding("MED000", "high", w.rule, f"waiver for {w.subject} expired on {w.expires}")
            )
    active = {(w.rule, w.subject): w for w in (waivers or []) if w.expires >= today}
    for f in raw:
        waiver = active.get((f.rule, f.subject))
        if waiver is not None:
            report.waived.append((f, waiver))
        else:
            report.findings.append(f)
    report.findings.sort(
        key=lambda f: (-SEVERITIES.index(f.severity), f.rule, f.subject, f.message)
    )
    return report


def catalog() -> list[tuple[str, str, str, str]]:
    base = [
        (
            "MED000",
            "high",
            "A waiver must not be past its expiry date",
            "Exceptions must be temporary.",
        ),
        (
            "MED001",
            "critical",
            "The specification must match its schema",
            "Problems are reported together, not one at a time.",
        ),
    ]
    rows = base + [(r.id, r.severity, r.title, r.why) for r in REGISTRY]
    return sorted(rows)
