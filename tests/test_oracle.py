"""The reference executor, checked against hand-derived results for the shipped sample data."""

from __future__ import annotations

import itertools
import random
from pathlib import Path
from typing import Any

import pytest

from medallion import gen_sql, oracle, spec
from tests.conftest import SPEC, write_jsonl


def ids(rows: list[dict[str, Any]], key: str) -> list[Any]:
    return [r[key] for r in rows]


# ---- the shipped sample, by hand -----------------------------------------------------------------


def test_row_counts(result):
    counts = {k: len(v) for k, v in result.tables.items()}
    assert counts == {
        "bronze.customers": 9, "bronze.orders": 17, "bronze.payments": 8,
        "silver.customers": 6, "silver.customers_quarantine": 1,
        "silver.orders": 8, "silver.orders_quarantine": 5,
        "silver.payments": 4, "silver.payments_quarantine": 1,
        "gold.customer_value": 3, "gold.daily_revenue": 7, "gold.payments_by_method_daily": 4,
    }  # fmt: skip


def test_bronze_keeps_raw_text_and_rescues_what_does_not_fit(result):
    cust = result.rows("bronze.customers")
    assert cust[6]["customer_id"] == " C004 " and cust[6]["email"] == " DENNIS@Example.com "
    assert cust[8]["_rescued_data"] == '{"loyalty_tier":"gold"}'
    orders = result.rows("bronze.orders")
    assert (
        next(r for r in orders if r["order_id"] == "O1001" and r["op"] == "D")["amount"] == "120.50"
    )
    corrupt = [r for r in orders if r["_rescued_data"]]
    assert len(corrupt) == 1 and corrupt[0]["order_id"] is None
    assert "_corrupt_record" in corrupt[0]["_rescued_data"]
    assert all(
        isinstance(r[c], str) or r[c] is None for r in orders for c in ("amount", "order_ts")
    )


def test_bronze_numbers_become_text_and_source_file_records_the_volume_path(result):
    row = next(r for r in result.rows("bronze.orders") if r["order_id"] == "O1010")
    assert row["amount"] == "59.9"
    assert row["_source_file"] == "/Volumes/retail_dev/landing/raw/orders/2026-11-12.jsonl"


def test_silver_orders_current_state(result):
    rows = result.rows("silver.orders")
    assert ids(rows, "order_id") == [
        "O1002",
        "O1003",
        "O1004",
        "O1005",
        "O1006",
        "O1010",
        "O1011",
        "O1013",
    ]
    by = {r["order_id"]: r for r in rows}
    assert "O1001" not in by, "deleted by the later D event"
    assert by["O1002"]["status"] == "completed", "the update replaced the insert"
    assert by["O1003"]["status"] == "completed", (
        "a late, older event must not overwrite a newer one"
    )
    assert by["O1005"]["amount"] == 16.0 and by["O1005"]["currency"] == "USD", (
        "trimmed, cast half-up, upper-cased"
    )
    assert by["O1004"]["order_ts"] == "2026-02-03T14:30:00.000000Z", "+01:00 converted to UTC"
    assert by["O1011"]["order_ts"] == "2026-02-04T04:30:00.000000Z", "-05:00 crosses midnight"
    assert by["O1010"]["amount"] == 59.9
    assert by["O1013"]["status"] == "shipped", "an unknown status only warns"
    assert "op" not in by["O1002"], "the operation column is not carried into the target"


def test_silver_orders_quarantine_reasons(result):
    q = result.rows("silver.orders_quarantine")
    reasons = {r["order_id"]: r["failed_rules"] for r in q}
    assert reasons["O1007"] == ["amount_valid"]
    assert reasons[""] == ["order_id_present"]
    assert reasons["O1008"] == ["amount_valid"]
    assert reasons["O1009"] == ["order_ts_valid"]
    assert reasons[None] == [
        "order_id_present", "customer_id_present", "amount_valid", "order_ts_valid",
        "updated_at_valid", "op_present",
    ]  # fmt: skip


def test_quarantine_and_current_state_partition_the_valid_input(result):
    bronze = len(result.rows("bronze.orders"))
    quarantined = len(result.rows("silver.orders_quarantine"))
    # 17 events - 5 quarantined = 12 valid events for 8 live keys plus the deleted O1001,
    # and the superseded O1002 insert and older O1003 event
    assert bronze - quarantined == 12


def test_customers_are_a_type_2_history(result):
    rows = result.rows("silver.customers")
    by_key: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_key.setdefault(r["customer_id"], []).append(r)
    c1 = by_key["C001"]
    assert [r["email"] for r in c1] == ["ada@example.com", "ada.lovelace@example.com"]
    assert c1[0]["__END_AT"] == c1[1]["__START_AT"] == "2026-03-01T12:00:00.000000Z"
    assert c1[1]["__END_AT"] is None
    assert len(by_key["C002"]) == 1, (
        "an update that changes nothing business-relevant opens no version"
    )
    assert by_key["C002"][0]["__END_AT"] is None
    c3 = by_key["C003"]
    assert [(r["country"], r["__END_AT"]) for r in c3] == [
        ("FI", "2026-04-01T08:00:00.000000Z"),
        ("SE", None),
    ]
    assert (
        by_key["C004"][0]["customer_id"] == "C004"
        and by_key["C004"][0]["email"] == "dennis@example.com"
    )
    assert by_key["C004"][0]["segment"] == "premium"
    assert sum(1 for r in rows if r["__END_AT"] is None) == 4, "one open version per live customer"


def test_customer_quarantine(result):
    (row,) = result.rows("silver.customers_quarantine")
    assert row["customer_id"] is None and row["failed_rules"] == ["customer_id_present"]


def test_payments_current_state(result):
    rows = result.rows("silver.payments")
    assert ids(rows, "payment_id") == ["P1", "P2", "P4", "P6"], "P3 deleted, P5 quarantined"
    p2 = next(r for r in rows if r["payment_id"] == "P2")
    assert p2["paid_at"] == "2026-02-03T09:06:00.000000Z"
    (q,) = result.rows("silver.payments_quarantine")
    assert q["payment_id"] == "P5" and q["failed_rules"] == ["amount_positive"]


def test_gold_daily_revenue(result):
    got = [
        (r["order_date"], r["country"], r["orders_count"], r["revenue"], r["avg_order_value"])
        for r in result.rows("gold.daily_revenue")
    ]
    assert got == [
        ("2026-02-02", "US", 1, 80.0, 80.0),
        ("2026-02-03", "GB", 1, 45.25, 45.25),
        ("2026-02-03", "SE", 1, 300.0, 300.0),
        ("2026-02-04", "US", 1, 30.0, 30.0),
        ("2026-02-05", "US", 1, 16.0, 16.0),
        ("2026-02-05", "ZZ", 1, 10.0, 10.0),
        ("2026-02-07", "GB", 1, 59.9, 59.9),
    ]  # fmt: skip


def test_gold_daily_revenue_uses_the_current_customer_version_only(result):
    # C003 has a closed FI version and an open SE version; the order must count once, as SE
    rows = [r for r in result.rows("gold.daily_revenue") if r["order_date"] == "2026-02-03"]
    assert sum(r["orders_count"] for r in rows) == 2
    assert {r["country"] for r in rows} == {"GB", "SE"}


def test_gold_customer_value(result):
    got = {r["customer_id"]: r for r in result.rows("gold.customer_value")}
    assert set(got) == {"C001", "C002", "C003"}, (
        "C999 has no customer row and C004 has no completed order"
    )
    assert (got["C001"]["orders_count"], got["C001"]["lifetime_value"]) == (2, 105.15)
    assert (got["C002"]["orders_count"], got["C002"]["lifetime_value"]) == (3, 126.0)
    assert (
        got["C001"]["first_order_date"] == "2026-02-03"
        and got["C001"]["last_order_date"] == "2026-02-07"
    )
    assert got["C001"]["email"] == "ada.lovelace@example.com", "the current email, not the first"


def test_gold_payments_by_method(result):
    got = [
        (r["paid_date"], r["method"], r["payments_count"], r["paid_amount"])
        for r in result.rows("gold.payments_by_method_daily")
    ]
    assert got == [
        ("2026-02-01", "card", 1, 120.5),
        ("2026-02-03", "wallet", 1, 45.25),
        ("2026-02-05", "card", 1, 16.0),
        ("2026-02-06", "cash", 1, 20.0),
    ]  # fmt: skip


def test_gold_totals_agree_with_an_independent_calculation(result):
    completed = [r for r in result.rows("silver.orders") if r["status"] == "completed"]
    assert round(sum(r["revenue"] for r in result.rows("gold.daily_revenue")), 2) == round(
        sum(r["amount"] for r in completed), 2
    )
    assert sum(r["orders_count"] for r in result.rows("gold.daily_revenue")) == len(completed)
    assert round(
        sum(r["paid_amount"] for r in result.rows("gold.payments_by_method_daily")), 2
    ) == round(sum(r["amount"] for r in result.rows("silver.payments")), 2)
    customers = {r["customer_id"] for r in result.rows("silver.customers") if r["__END_AT"] is None}
    known = [r for r in completed if r["customer_id"] in customers]
    assert sum(r["orders_count"] for r in result.rows("gold.customer_value")) == len(known)


def test_gold_grain_is_unique(lakehouse, result):
    for t in lakehouse.layer("gold"):
        grain = [c.name for c in t.columns if c.group]
        keys = [tuple(r[g] for g in grain) for r in result.rows(t.fqn)]
        assert len(keys) == len(set(keys)), t.fqn


def test_silver_keys_are_unique_and_open_versions_unique(lakehouse, result):
    for t in lakehouse.layer("silver"):
        assert t.cdc is not None
        rows = result.rows(t.fqn)
        if t.cdc.scd == 1:
            keys = [tuple(r[k] for k in t.cdc.keys) for r in rows]
        else:
            keys = [tuple(r[k] for k in t.cdc.keys) for r in rows if r["__END_AT"] is None]
        assert len(keys) == len(set(keys)), t.fqn


def test_metrics(result):
    def m(table: str, name: str) -> tuple[int, int]:
        x = result.metric(table, name)
        return x.passed, x.failed

    assert m("bronze.orders", "rescued_data_empty") == (16, 1)
    assert m("bronze.customers", "rescued_data_empty") == (8, 1)
    assert m("silver.orders", "amount_valid") == (14, 3)
    assert m("silver.orders", "status_known") == (15, 2)
    assert m("silver.orders", "op_known") == (17, 0)
    assert m("silver.payments", "method_known") == (6, 2)
    assert m("silver.customers", "country_is_iso_length") == (9, 0)
    for x in result.metrics:
        assert x.passed + x.failed in {9, 17, 8}, x


def test_every_expectation_has_a_metric(lakehouse, result):
    have = {(m.table, m.name) for m in result.metrics}
    for t in lakehouse.layer("silver"):
        for e in t.expectations:
            assert (t.fqn, e.name) in have


def test_masks_show_real_values_only_to_privileged_readers(lakehouse, result):
    rows = [r for r in result.rows("gold.customer_value") if r["customer_id"] == "C001"]
    plain = oracle.view_as(lakehouse, "gold.customer_value", rows, privileged=False)[0]
    real = oracle.view_as(lakehouse, "gold.customer_value", rows, privileged=True)[0]
    assert plain["email"] == "***@example.com" and real["email"] == "ada.lovelace@example.com"
    assert plain["lifetime_value"] == real["lifetime_value"], "only masked columns change"
    cust = oracle.view_as(
        lakehouse, "silver.customers", result.rows("silver.customers"), privileged=False
    )
    assert {r["full_name"] for r in cust} == {"[REDACTED]"}
    q = oracle.view_as(
        lakehouse,
        "silver.customers_quarantine",
        result.rows("silver.customers_quarantine"),
        privileged=False,
    )
    assert q[0]["email"] == "***@example.com", "quarantine is masked too"
    with pytest.raises(KeyError):
        oracle.view_as(lakehouse, "silver.nope", [], privileged=True)


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("email", "ada@example.com", "***@example.com"),
        ("email", "no-at-sign", "***@no-at-sign"),
        ("redact", "anything", "[REDACTED]"),
        ("last4", "4111111111111111", "************1111"),
        ("last4", "12", "12"),
        ("hash", "x", "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"),
    ],
)
def test_mask_values(kind, value, expected):
    assert oracle.apply_mask(kind, value, privileged=False) == expected
    assert oracle.apply_mask(kind, value, privileged=True) == value
    assert oracle.apply_mask(kind, None, privileged=False) is None


def test_unknown_mask_is_an_error():
    with pytest.raises(ValueError, match="unknown mask"):
        oracle.apply_mask("scramble", "x", privileged=False)


# ---- scenarios on small inputs -----------------------------------------------------------------------


def order(
    oid: str,
    ts: str,
    status: str = "completed",
    op: str = "I",
    amount: str = "10.00",
    cust: str = "C1",
) -> dict[str, str]:
    return {
        "order_id": oid,
        "customer_id": cust,
        "status": status,
        "amount": amount,
        "currency": "usd",
        "order_ts": "2026-01-01T00:00:00Z",
        "updated_at": ts,
        "op": op,
    }


def customer(
    cid: str, ts: str, email: str = "a@x.com", op: str = "I", country: str = "gb"
) -> dict[str, str]:
    return {
        "customer_id": cid,
        "email": email,
        "full_name": "N",
        "country": country,
        "segment": "s",
        "updated_at": ts,
        "op": op,
    }


@pytest.fixture
def run_data(lakehouse, tmp_path):
    def _run(
        orders: list[Any] = (), customers: list[Any] = (), payments: list[Any] = ()
    ) -> oracle.Result:  # type: ignore[assignment]
        root = tmp_path / f"d{next(_counter)}"
        for name, rows in (("orders", orders), ("customers", customers), ("payments", payments)):
            (root / name).mkdir(parents=True)
            if rows:
                write_jsonl(root / name, "f.jsonl", list(rows))
        return oracle.run(lakehouse, root)

    return _run


_counter = itertools.count()


def test_empty_input_builds_empty_tables(run_data):
    res = run_data()
    assert all(rows == [] for rows in res.tables.values())
    assert all(m.failed == 0 for m in res.metrics)


def test_arrival_order_does_not_change_the_result(run_data):
    events = [
        order("A", "2026-01-01T00:00:00Z", status="new"),
        order("A", "2026-01-02T00:00:00Z", status="completed"),
        order("A", "2026-01-03T00:00:00Z", status="cancelled"),
        order("B", "2026-01-01T00:00:00Z"),
        order("B", "2026-01-05T00:00:00Z", op="D"),
        order("C", "2026-01-01T00:00:00Z"),
    ]
    reference = None
    for perm in itertools.islice(itertools.permutations(events), 40):
        got = run_data(orders=list(perm)).rows("silver.orders")
        simple = [(r["order_id"], r["status"]) for r in got]
        reference = reference or simple
        assert simple == reference == [("A", "cancelled"), ("C", "completed")]


def test_type_2_history_does_not_depend_on_arrival_order(run_data):
    events = [
        customer("K", "2026-01-01T00:00:00Z", email="one@x.com"),
        customer("K", "2026-02-01T00:00:00Z", email="two@x.com"),
        customer("K", "2026-03-01T00:00:00Z", email="three@x.com"),
    ]
    random.Random(7).shuffle(events)
    rows = run_data(customers=events).rows("silver.customers")
    assert [(r["email"], r["__END_AT"] is None) for r in rows] == [
        ("one@x.com", False),
        ("two@x.com", False),
        ("three@x.com", True),
    ]
    for a, b in itertools.pairwise(rows):
        assert a["__END_AT"] == b["__START_AT"], "versions are contiguous"


def test_delete_then_reinsert_in_type_1_and_type_2(run_data):
    o = run_data(
        orders=[
            order("A", "2026-01-01T00:00:00Z"),
            order("A", "2026-01-02T00:00:00Z", op="D"),
            order("A", "2026-01-03T00:00:00Z", status="new"),
        ]
    )
    assert [(r["order_id"], r["status"]) for r in o.rows("silver.orders")] == [("A", "new")]
    c = run_data(
        customers=[
            customer("K", "2026-01-01T00:00:00Z"),
            customer("K", "2026-01-02T00:00:00Z", op="D"),
            customer("K", "2026-01-03T00:00:00Z", country="se"),
        ]
    )
    assert [
        (r["country"], r["__START_AT"][:10], (r["__END_AT"] or "open")[:10])
        for r in c.rows("silver.customers")
    ] == [("GB", "2026-01-01", "2026-01-02"), ("SE", "2026-01-03", "open")]


def test_delete_of_an_unknown_key_is_harmless(run_data):
    res = run_data(
        orders=[order("Z", "2026-01-01T00:00:00Z", op="D")],
        customers=[customer("Z", "2026-01-01T00:00:00Z", op="D")],
    )
    assert res.rows("silver.orders") == [] and res.rows("silver.customers") == []


def test_exact_redelivery_is_collapsed(run_data):
    e = order("A", "2026-01-01T00:00:00Z")
    res = run_data(orders=[e, e, e], customers=[customer("K", "2026-01-01T00:00:00Z")] * 2)
    assert len(res.rows("silver.orders")) == 1 and len(res.rows("silver.customers")) == 1


def test_two_different_events_with_the_same_sequence_are_refused(run_data):
    with pytest.raises(oracle.AmbiguousOrdering, match="two different events"):
        run_data(
            orders=[
                order("A", "2026-01-01T00:00:00Z", status="new"),
                order("A", "2026-01-01T00:00:00Z", status="completed"),
            ]
        )
    with pytest.raises(oracle.AmbiguousOrdering):
        run_data(
            orders=[order("A", "2026-01-01T00:00:00Z"), order("A", "2026-01-01T00:00:00Z", op="D")]
        )


def test_a_fail_expectation_stops_the_update(run_data):
    with pytest.raises(oracle.ExpectationFailure) as exc:
        run_data(
            orders=[order("A", "2026-01-01T00:00:00Z"), order("B", "2026-01-01T00:00:00Z", op="T")]
        )
    assert (
        exc.value.table == "silver.orders" and exc.value.rule == "op_known" and exc.value.count == 1
    )
    assert "(fail)" in str(exc.value)


def test_a_missing_operation_is_quarantined_not_fatal(run_data):
    bad = order("A", "2026-01-01T00:00:00Z")
    del bad["op"]
    res = run_data(orders=[bad])
    assert res.rows("silver.orders") == []
    assert res.rows("silver.orders_quarantine")[0]["failed_rules"] == ["op_present"]


def test_a_row_failing_two_rules_lists_both_in_declaration_order(run_data):
    res = run_data(orders=[order("A", "2026-01-01T00:00:00Z", amount="-1", cust="")])
    assert res.rows("silver.orders_quarantine")[0]["failed_rules"] == [
        "customer_id_present",
        "amount_valid",
    ]


def test_warn_expectations_keep_the_row_and_count_it(run_data):
    res = run_data(orders=[order("A", "2026-01-01T00:00:00Z", status="mystery")])
    assert len(res.rows("silver.orders")) == 1
    assert (res.metric("silver.orders", "status_known").failed) == 1


def test_trailing_whitespace_and_case_are_normalised_in_keys(run_data):
    res = run_data(
        orders=[
            order(" A ", "2026-01-01T00:00:00Z"),
            order("A", "2026-01-02T00:00:00Z", status="new"),
        ]
    )
    assert [(r["order_id"], r["status"]) for r in res.rows("silver.orders")] == [("A", "new")]


def test_no_completed_orders_means_no_revenue_rows(run_data):
    res = run_data(orders=[order("A", "2026-01-01T00:00:00Z", status="new")])
    assert res.rows("gold.daily_revenue") == [] and res.rows("gold.customer_value") == []


def test_fan_out_is_absent_when_a_customer_has_one_open_version(run_data):
    res = run_data(
        orders=[order("A", "2026-01-01T00:00:00Z"), order("B", "2026-01-01T00:00:00Z")],
        customers=[
            customer("C1", "2026-01-01T00:00:00Z", country="gb"),
            customer("C1", "2026-01-02T00:00:00Z", country="fr"),
        ],
    )
    assert [(r["country"], r["orders_count"]) for r in res.rows("gold.daily_revenue")] == [
        ("FR", 2)
    ]


def test_ingestion_time_and_environment_are_parameters(lakehouse, tmp_path):
    write_jsonl(tmp_path / "orders", "a.jsonl", [order("A", "2026-01-01T00:00:00Z")])
    for n in ("customers", "payments"):
        (tmp_path / n).mkdir()
    res = oracle.run(lakehouse, tmp_path, env="prod", ingested_at="2030-01-01T00:00:00.000000Z")
    row = res.rows("silver.orders")[0]
    assert row["_ingested_at"] == "2030-01-01T00:00:00.000000Z"
    assert row["_source_file"] == "/Volumes/retail_prod/landing/raw/orders/a.jsonl"


# ---- source reading and safety -------------------------------------------------------------------------


def test_read_source_orders_files_by_name_skips_blanks_and_flags_non_objects(lakehouse, tmp_path):
    t = lakehouse.table("bronze.orders")
    folder = tmp_path / "orders"
    folder.mkdir()
    (folder / "b.jsonl").write_text('{"order_id": "B"}\n\n', encoding="utf-8")
    (folder / "a.jsonl").write_text('{"order_id": "A"}\n[1, 2]\nnot json\n', encoding="utf-8")
    (folder / "ignored.csv").write_text("x", encoding="utf-8")
    got = oracle.read_source(tmp_path, t)  # type: ignore[arg-type]
    assert [(f, r if isinstance(r, str) else r["order_id"]) for f, r in got] == [
        ("a.jsonl", "A"),
        ("a.jsonl", "[1, 2]"),
        ("a.jsonl", "not json"),
        ("b.jsonl", "B"),
    ]


def test_bronze_stringifies_values_and_rescues_extras(lakehouse):
    t = lakehouse.table("bronze.orders")
    rows = oracle.build_bronze(
        t, [("f", {"order_id": 5, "status": True, "amount": {"a": 1}, "extra": [1]})], "ts", "c"
    )  # type: ignore[arg-type]
    r = rows[0]
    assert (r["order_id"], r["status"], r["amount"]) == ("5", "true", '{"a":1}')
    assert r["customer_id"] is None and r["_rescued_data"] == '{"extra":[1]}'


def test_the_engine_refuses_unsafe_identifiers_and_expressions(lakehouse):
    with oracle.Engine() as eng:
        with pytest.raises(ValueError, match="unsafe identifier"):
            eng.create("bronze.x; DROP", ["a"], [])
        with pytest.raises(ValueError, match="unsafe identifier"):
            eng.create_temp("t", ["a b"], [])
        eng.create("bronze.t", ["a"], [["1"]])
        with pytest.raises(ValueError, match="unsafe expression"):
            oracle._eval_column(eng, "bronze.t", ["a; DROP TABLE t"])


def test_unsafe_gold_expression_is_refused(lakehouse):
    import dataclasses

    t = lakehouse.table("gold.daily_revenue")
    assert t is not None
    bad = dataclasses.replace(
        t, columns=(dataclasses.replace(t.columns[0], expr="a; b"), *t.columns[1:])
    )
    with oracle.Engine() as eng, pytest.raises(ValueError, match="unsafe expression"):
        oracle.build_gold(eng, bad)


def test_unicode_case_mapping_matches_spark_not_sqlite(run_data):
    res = run_data(orders=[order("A", "2026-01-01T00:00:00Z", status="ÉTÉ")])
    assert res.rows("silver.orders")[0]["status"] == "été"


# ---- the oracle and the generated SQL describe the same query -------------------------------------------


def test_gold_query_in_the_oracle_matches_the_generated_sql(lakehouse):
    for t in lakehouse.layer("gold"):
        spark = gen_sql.gold_sql(t)
        body = spark[spark.index("\nFROM ") + 1 :].rstrip().rstrip(";").replace("${catalog}.", "")
        oracle_sql = oracle.gold_query(t)
        tail = oracle_sql[oracle_sql.index(" FROM ") + 1 :]
        assert " ".join(tail.split()) == " ".join(body.split()), t.fqn


def test_silver_select_in_sql_wraps_the_same_expressions_the_oracle_evaluates(lakehouse):
    for t in lakehouse.layer("silver"):
        sql = gen_sql.silver_sql(t)
        for c in t.columns:
            assert f"try_cast({c.expr} AS {c.type.upper().replace(' ', '')}) AS {c.name}" in sql


def test_oracle_assumptions_are_listed():
    assert len(oracle.ASSUMPTIONS) >= 7 and all(a.endswith(".") for a in oracle.ASSUMPTIONS)


def test_run_does_not_touch_the_spec_directory(lakehouse):
    before = sorted(p.name for p in Path(SPEC).rglob("*"))
    oracle.run(lakehouse, SPEC / "data")
    assert sorted(p.name for p in Path(SPEC).rglob("*")) == before


def test_target_columns_agree_with_what_the_oracle_builds(lakehouse, result):
    for t in lakehouse.tables:
        expected = [n for n, _ in spec.target_columns(t)]
        rows = result.rows(t.fqn)
        if rows:
            assert list(rows[0]) == expected or set(rows[0]) == set(expected), t.fqn
