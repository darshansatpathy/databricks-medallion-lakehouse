from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from medallion import policy, spec
from tests.conftest import Draft

TODAY = date(2026, 11, 13)


def lint(spec_dir: Path, waivers: list[policy.Waiver] | None = None) -> policy.Report:
    lh, problems = spec.load(spec_dir)
    return policy.run(lh, problems, TODAY, waivers)


def test_shipped_spec_passes_every_rule(lakehouse):
    report = policy.run(lakehouse, [], TODAY)
    assert report.findings == [] and report.waived == []
    assert report.tables == 9 and report.rules_run == len(policy.REGISTRY) + 1


# ---- mutations: (rule, case id, mutator, fragment expected in the message) --------------------


def _copy_table(d: Draft, src: str, filename: str, **table: object) -> None:
    import copy

    clone = copy.deepcopy(d.table(src))
    clone["table"].update(table)
    d.files[f"{filename}.toml"] = clone


def _extra_expectation_all_warn(d: Draft) -> None:
    for e in d.table("silver.payments")["expectations"]:
        if e["action"] != "warn":
            e["action"] = "warn"


def _no_fail(d: Draft) -> None:
    d.table("silver.orders")["expectations"] = [
        e for e in d.table("silver.orders")["expectations"] if e["action"] != "fail"
    ]


def _two_runners(d: Draft) -> None:
    d.platform["groups"].append({"name": "other_runner", "role": "runner"})


def _no_privileged(d: Draft) -> None:
    d.platform["groups"] = [g for g in d.platform["groups"] if g["role"] != "privileged"]
    d.platform["grants"] = [
        g for g in d.platform["grants"] if g["group"] != "lakehouse_pii_readers"
    ]


CASES: list[tuple[str, str, object, str]] = [
    # MED002 references
    (
        "MED002",
        "grant-group",
        lambda d: d.platform["grants"].append(
            {"group": "ghost", "schema": "gold", "privileges": ["SELECT"], "envs": ["dev"]}
        ),
        "not declared",
    ),
    (
        "MED002",
        "grant-schema",
        lambda d: d.grant("lakehouse_analysts", "gold").update(schema="platinum"),
        "unknown schema",
    ),
    (
        "MED002",
        "grant-env",
        lambda d: d.grant("lakehouse_analysts", "gold").update(envs=["qa"]),
        "unknown environment",
    ),
    (
        "MED002",
        "owner",
        lambda d: d.table("silver.orders")["table"].update(owner="nobody"),
        "not declared in platform.toml",
    ),
    (
        "MED002",
        "upstream",
        lambda d: d.table("silver.orders")["table"].update(**{"from": "bronze.nope"}),
        "does not exist",
    ),
    (
        "MED002",
        "cdc-key",
        lambda d: d.table("silver.orders")["cdc"].update(keys=["nope"]),
        "cdc key",
    ),
    (
        "MED002",
        "cdc-seq",
        lambda d: d.table("silver.orders")["cdc"].update(sequence_by="nope"),
        "sequence_by",
    ),
    (
        "MED002",
        "cdc-drop",
        lambda d: d.table("silver.orders")["cdc"].update(drop_columns=["nope"]),
        "drop_columns",
    ),
    (
        "MED002",
        "query-table",
        lambda d: d.table("gold.daily_revenue")["query"].update(**{"from": "silver.nope"}),
        "does not exist",
    ),
    (
        "MED002",
        "silver-expr-column",
        lambda d: d.col("silver.orders", "status").update(expr="lower(trim(state))"),
        "'state'",
    ),
    # MED003 layering
    (
        "MED003",
        "silver-from-silver",
        lambda d: d.table("silver.orders")["table"].update(**{"from": "silver.payments"}),
        "silver must read from bronze",
    ),
    (
        "MED003",
        "bronze-upstream",
        lambda d: d.table("bronze.orders")["table"].update(**{"from": "bronze.payments"}),
        "bronze reads from landing",
    ),
    (
        "MED003",
        "gold-reads-bronze",
        lambda d: d.table("gold.payments_by_method_daily")["query"].update(
            **{"from": "bronze.payments"}
        ),
        "may not read bronze",
    ),
    (
        "MED003",
        "gold-from",
        lambda d: d.table("gold.daily_revenue")["table"].update(**{"from": "silver.orders"}),
        "uses [query]",
    ),
    # MED004 bronze
    (
        "MED004",
        "type",
        lambda d: d.col("bronze.orders", "amount").update(type="decimal(18,2)"),
        "bronze columns are string",
    ),
    (
        "MED004",
        "reserved-prefix",
        lambda d: d.col("bronze.orders", "op").update(name="_op"),
        "reserved for system",
    ),
    (
        "MED004",
        "expr",
        lambda d: d.col("bronze.orders", "op").update(expr="trim(op)"),
        "bronze does not transform",
    ),
    (
        "MED004",
        "expectations",
        lambda d: d.table("bronze.orders").update(
            expectations=[{"name": "x", "rule": "op IS NOT NULL", "action": "warn"}]
        ),
        "validate in silver",
    ),
    (
        "MED004",
        "format",
        lambda d: d.table("bronze.orders")["source"].update(format="xml"),
        "source format",
    ),
    (
        "MED004",
        "path",
        lambda d: d.table("bronze.orders")["source"].update(path="Orders/Raw"),
        "source path",
    ),
    ("MED004", "no-source", lambda d: d.table("bronze.orders").pop("source"), "needs a [source]"),
    # MED005 silver
    ("MED005", "no-cdc", lambda d: d.table("silver.orders").pop("cdc"), "needs a [cdc]"),
    ("MED005", "scd", lambda d: d.table("silver.orders")["cdc"].update(scd=3), "must be 1 or 2"),
    (
        "MED005",
        "empty-keys",
        lambda d: d.table("silver.orders")["cdc"].update(keys=[]),
        "must not be empty",
    ),
    (
        "MED005",
        "seq-type",
        lambda d: d.col("silver.orders", "updated_at").update(type="string"),
        "it must sort",
    ),
    ("MED005", "no-expr", lambda d: d.col("silver.orders", "status").pop("expr"), "no expr"),
    (
        "MED005",
        "key-not-required",
        lambda d: d.exp("silver.orders", "order_id_present").update(rule="length(order_id) > 0"),
        "requires order_id IS NOT NULL",
    ),
    (
        "MED005",
        "sequence-not-required",
        lambda d: d.exp("silver.orders", "updated_at_valid").update(action="warn"),
        "requires updated_at IS NOT NULL",
    ),
    ("MED005", "deletes-without-fail", _no_fail, "needs a fail expectation"),
    # MED006 gold
    ("MED006", "no-query", lambda d: d.table("gold.daily_revenue").pop("query"), "needs a [query]"),
    (
        "MED006",
        "alias-twice",
        lambda d: d.table("gold.daily_revenue")["query"]["joins"][0].update(alias="o"),
        "aliases must be unique",
    ),
    (
        "MED006",
        "join-kind",
        lambda d: d.table("gold.daily_revenue")["query"]["joins"][0].update(kind="full"),
        "inner or left",
    ),
    (
        "MED006",
        "grain-aggregate",
        lambda d: d.col("gold.daily_revenue", "country").update(expr="max(c.country)"),
        "grain column country is an aggregate",
    ),
    (
        "MED006",
        "measure-not-aggregate",
        lambda d: d.col("gold.daily_revenue", "revenue").update(expr="o.amount"),
        "neither a grain column nor an aggregate",
    ),
    (
        "MED006",
        "unqualified",
        lambda d: d.col("gold.daily_revenue", "revenue").update(expr="sum(amount)"),
        "must be qualified",
    ),
    (
        "MED006",
        "unknown-alias",
        lambda d: d.col("gold.daily_revenue", "revenue").update(expr="sum(x.amount)"),
        "alias 'x'",
    ),
    (
        "MED006",
        "unknown-column",
        lambda d: d.col("gold.daily_revenue", "revenue").update(expr="sum(o.nope)"),
        "has no column 'nope'",
    ),
    (
        "MED006",
        "no-expr",
        lambda d: d.col("gold.daily_revenue", "revenue").pop("expr"),
        "has no expr",
    ),
    # MED007 personal data
    ("MED007", "no-mask", lambda d: d.col("silver.customers", "email").pop("mask"), "has no mask"),
    (
        "MED007",
        "unknown-mask",
        lambda d: d.col("silver.customers", "email").update(mask="scramble"),
        "unknown mask",
    ),
    (
        "MED007",
        "non-string",
        lambda d: d.col("silver.customers", "updated_at").update(pii=True, mask="hash"),
        "string columns only",
    ),
    (
        "MED007",
        "internal-table",
        lambda d: d.table("gold.customer_value")["table"].update(classification="internal"),
        "confidential or restricted",
    ),
    (
        "MED007",
        "mask-without-pii",
        lambda d: d.col("silver.customers", "country").update(mask="redact"),
        "not marked pii",
    ),
    (
        "MED007",
        "gold-leak",
        lambda d: d.table("gold.customer_value")["columns"].append(
            {
                "name": "email_copy",
                "type": "string",
                "expr": "max(c.email)",
                "comment": "Copy of the email",
            }
        ),
        "derives from personal data",
    ),
    # MED008 metadata
    (
        "MED008",
        "classification",
        lambda d: d.table("silver.orders")["table"].update(classification="secret"),
        "classification 'secret'",
    ),
    (
        "MED008",
        "short-comment",
        lambda d: d.table("silver.orders")["table"].update(comment="Orders"),
        "at least 30 characters",
    ),
    (
        "MED008",
        "domain",
        lambda d: d.table("silver.orders")["table"].update(domain="Sales"),
        "domain must be",
    ),
    (
        "MED008",
        "column-comment",
        lambda d: d.col("silver.orders", "status").update(comment="x"),
        "column comment",
    ),
    # MED009 expectations
    (
        "MED009",
        "duplicate",
        lambda d: d.table("silver.orders")["expectations"].append(
            dict(d.exp("silver.orders", "op_known"))
        ),
        "used twice",
    ),
    (
        "MED009",
        "bad-name",
        lambda d: d.exp("silver.orders", "amount_valid").update(name="AmountValid"),
        "lower_snake_case",
    ),
    (
        "MED009",
        "bad-action",
        lambda d: d.exp("silver.orders", "amount_valid").update(action="ignore"),
        "action must be one of",
    ),
    (
        "MED009",
        "unknown-column",
        lambda d: d.exp("silver.orders", "amount_valid").update(rule="amount_gross >= 0"),
        "not a column",
    ),
    ("MED009", "all-warn", _extra_expectation_all_warn, "needs at least one drop or fail"),
    # MED010 type 2 consumers
    (
        "MED010",
        "current-version",
        lambda d: d.table("gold.customer_value")["query"]["joins"][0].update(
            on="o.customer_id = c.customer_id"
        ),
        "without c.__END_AT IS NULL",
    ),
    # MED011 expression safety
    (
        "MED011",
        "division",
        lambda d: d.col("gold.daily_revenue", "avg_order_value").update(
            expr="sum(o.amount) / count(*)"
        ),
        "not allowed",
    ),
    (
        "MED011",
        "like",
        lambda d: d.exp("silver.orders", "status_known").update(rule="status LIKE 'comp%'"),
        "not allowed",
    ),
    (
        "MED011",
        "comment",
        lambda d: d.table("gold.daily_revenue")["query"].update(
            where="o.status = 'completed' -- all"
        ),
        "comments",
    ),
    (
        "MED011",
        "semicolon",
        lambda d: d.table("silver.orders")["cdc"].update(delete_when="op = 'D'; DROP TABLE x"),
        "separators",
    ),
    (
        "MED011",
        "unknown-function",
        lambda d: d.col("silver.orders", "status").update(expr="regexp_replace(status, 'a', 'b')"),
        "not in the portable subset",
    ),
    (
        "MED011",
        "grain-needs-expr",
        lambda d: d.col("gold.daily_revenue", "country").pop("expr"),
        "grain column needs an expr",
    ),
    # MED012 grants
    (
        "MED012",
        "person",
        lambda d: d.platform["groups"].append({"name": "ada@example.com", "role": "consumer"}),
        "not people",
    ),
    ("MED012", "role", lambda d: d.platform["groups"][0].update(role="wizard"), "unknown role"),
    (
        "MED012",
        "privilege",
        lambda d: d.grant("lakehouse_analysts", "gold").update(privileges=["SELECT", "DROP"]),
        "unknown privilege",
    ),
    (
        "MED012",
        "all-privileges",
        lambda d: d.grant("lakehouse_engineers", "gold").update(privileges=["ALL PRIVILEGES"]),
        "runner and admin",
    ),
    (
        "MED012",
        "consumer-silver",
        lambda d: d.grant("lakehouse_analysts", "gold").update(schema="silver"),
        "gold only",
    ),
    (
        "MED012",
        "consumer-modify",
        lambda d: d.grant("lakehouse_analysts", "gold").update(privileges=["SELECT", "MODIFY"]),
        "gold only",
    ),
    (
        "MED012",
        "engineer-bronze-prod",
        lambda d: d.grant("lakehouse_engineers", "bronze").update(envs=["dev", "prod"]),
        "do not read bronze in prod",
    ),
    (
        "MED012",
        "engineer-modify",
        lambda d: d.grant("lakehouse_engineers", "silver").update(privileges=["SELECT", "MODIFY"]),
        "SELECT only",
    ),
    (
        "MED012",
        "governance",
        lambda d: d.platform["grants"].append(
            {
                "group": "lakehouse_engineers",
                "schema": "governance",
                "privileges": ["SELECT"],
                "envs": ["dev"],
            }
        ),
        "only the runner role",
    ),
    (
        "MED012",
        "privileged-bronze",
        lambda d: d.platform["grants"].append(
            {
                "group": "lakehouse_pii_readers",
                "schema": "bronze",
                "privileges": ["SELECT"],
                "envs": ["dev"],
            }
        ),
        "curated layers only",
    ),
    ("MED012", "two-runners", _two_runners, "exactly one runner"),
    ("MED012", "no-privileged", _no_privileged, "declare a privileged group"),
    # MED013 naming
    (
        "MED013",
        "table",
        lambda d: d.table("silver.orders")["table"].update(name="Orders"),
        "lower_snake_case",
    ),
    (
        "MED013",
        "layer",
        lambda d: d.table("silver.orders")["table"].update(layer="platinum"),
        "must be one of",
    ),
    (
        "MED013",
        "column",
        lambda d: d.col("silver.orders", "status").update(name="OrderStatus"),
        "lower_snake_case",
    ),
    (
        "MED013",
        "type",
        lambda d: d.col("silver.orders", "status").update(type="varchar(10)"),
        "not supported",
    ),
    (
        "MED013",
        "catalog",
        lambda d: d.platform["platform"].update(catalog_pattern="Retail-{env}"),
        "catalog name",
    ),
    # MED014 classification
    (
        "MED014",
        "silver-below-bronze",
        lambda d: d.table("silver.customers")["table"].update(classification="internal"),
        "below its source",
    ),
    (
        "MED014",
        "gold-below-source",
        lambda d: d.table("gold.daily_revenue")["columns"].append(
            {"name": "top_email", "type": "string", "expr": "max(c.email)", "comment": "Some email"}
        ),
        "only internal",
    ),
    # MED015 uniqueness
    (
        "MED015",
        "duplicate-table",
        lambda d: _copy_table(d, "silver.orders", "dup"),
        "defined twice",
    ),
    (
        "MED015",
        "duplicate-column",
        lambda d: d.table("silver.orders")["columns"].append(
            dict(d.col("silver.orders", "status"))
        ),
        "defined twice",
    ),
    (
        "MED015",
        "generated-name",
        lambda d: _copy_table(d, "silver.orders", "clash", name="orders_quarantine"),
        "collides with a generated table",
    ),
]


@pytest.mark.parametrize(
    ("rule", "case", "mutate", "fragment"), CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES]
)
def test_rule_fires_on_a_broken_copy(make_spec, rule, case, mutate, fragment):
    report = lint(make_spec(mutate))
    hits = [f for f in report.findings if f.rule == rule and fragment in f.message]
    assert hits, (
        f"{rule}/{case}: expected {fragment!r}; got {[(f.rule, f.message) for f in report.findings]}"
    )


def test_every_rule_has_a_failing_case():
    exercised = {c[0] for c in CASES} | {"MED000", "MED001"}
    declared = {r.id for r in policy.REGISTRY} | {"MED000", "MED001"}
    assert exercised == declared


def test_rule_ids_are_unique_and_severities_valid():
    ids = [r.id for r in policy.REGISTRY]
    assert len(ids) == len(set(ids))
    assert all(r.severity in policy.SEVERITIES and r.why and r.title for r in policy.REGISTRY)
    assert [row[0] for row in policy.catalog()] == sorted(row[0] for row in policy.catalog())


# ---- spec problems, waivers, reporting ----------------------------------------------------------


def test_load_problems_become_critical_findings(make_spec):
    def mutate(d: Draft) -> None:
        d.extra["tables/broken.toml"] = "not = [valid"

    report = lint(make_spec(mutate))
    assert any(f.rule == "MED001" and f.severity == "critical" for f in report.findings)


def test_unloadable_spec_stops_after_the_schema_rule():
    report = policy.run(None, ["platform.toml does not exist"], TODAY)
    assert [f.rule for f in report.findings] == ["MED001"] and report.rules_run == 1


def _broken_waivable(d: Draft) -> None:
    d.col("silver.orders", "status").update(comment="x")


def test_waiver_silences_exactly_one_subject(make_spec):
    spec_dir = make_spec(_broken_waivable)
    base = lint(spec_dir)
    assert [f.rule for f in base.findings] == ["MED008"]
    w = policy.Waiver(
        "MED008",
        "silver.orders.status",
        "Comment lands with the next release train",
        date(2027, 1, 1),
    )
    waived = lint(spec_dir, [w])
    assert waived.findings == [] and len(waived.waived) == 1
    other = policy.Waiver(
        "MED008",
        "silver.orders.amount",
        "Comment lands with the next release train",
        date(2027, 1, 1),
    )
    assert len(lint(spec_dir, [other]).findings) == 1


def test_expired_waiver_is_a_finding_and_no_longer_silences(make_spec):
    spec_dir = make_spec(_broken_waivable)
    w = policy.Waiver(
        "MED008",
        "silver.orders.status",
        "Comment lands with the next release train",
        date(2026, 11, 12),
    )
    report = lint(spec_dir, [w])
    assert sorted(f.rule for f in report.findings) == ["MED000", "MED008"]
    assert report.waived == []


def test_waiver_expiring_today_is_still_in_force(make_spec):
    spec_dir = make_spec(_broken_waivable)
    w = policy.Waiver(
        "MED008", "silver.orders.status", "Comment lands with the next release train", TODAY
    )
    assert lint(spec_dir, [w]).findings == []


def test_waiver_file_is_validated(tmp_path: Path):
    f = tmp_path / "waivers.toml"
    assert policy.load_waivers(f) == ([], [])
    f.write_text(
        "[[waivers]]\nrule = 'MED008'\nsubject = 'x'\nreason = 'short'\nexpires = '2027-01-01'\n",
        encoding="utf-8",
    )
    waivers, problems = policy.load_waivers(f)
    assert len(waivers) == 1 and "at least 20 characters" in problems[0]
    f.write_text("[[waivers]]\nrule = 'MED008'\n", encoding="utf-8")
    assert "KeyError" in policy.load_waivers(f)[1][0]
    f.write_text(
        "[[waivers]]\nrule='a'\nsubject='b'\nreason='long enough reason here'\nexpires='tomorrow'\n",
        encoding="utf-8",
    )
    assert "ValueError" in policy.load_waivers(f)[1][0]
    f.write_text("not toml [", encoding="utf-8")
    assert policy.load_waivers(f)[0] == []


def test_findings_are_ordered_by_severity_and_threshold_filters(make_spec):
    def mutate(d: Draft) -> None:
        d.col("silver.orders", "status").update(comment="x")  # medium
        d.col("silver.customers", "email").pop("mask")  # critical

    report = lint(make_spec(mutate))
    sev = [f.severity for f in report.findings]
    assert sev == sorted(sev, key=policy.SEVERITIES.index, reverse=True)
    assert {f.severity for f in report.failing("critical")} == {"critical"}
    assert len(report.failing("medium")) == len(report.findings)
