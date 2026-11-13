from __future__ import annotations

from pathlib import Path

from medallion import spec
from tests.conftest import SPEC


def test_shipped_spec_loads_without_problems(lakehouse):
    assert len(lakehouse.tables) == 9
    assert [t.fqn for t in lakehouse.layer("gold")] == [
        "gold.customer_value",
        "gold.daily_revenue",
        "gold.payments_by_method_daily",
    ]
    assert lakehouse.platform.catalog("prod") == "retail_prod"
    assert lakehouse.table("silver.orders") is not None
    assert lakehouse.table("silver.nope") is None
    assert [g.name for g in lakehouse.platform.group_by_role("runner")] == [
        "lakehouse_pipeline_runners"
    ]


def test_table_fields_are_read(lakehouse):
    t = lakehouse.table("silver.customers")
    assert t is not None
    assert t.cdc is not None and t.cdc.scd == 2 and t.cdc.keys == ("customer_id",)
    assert t.cdc.drop_columns == ("op",)
    assert t.column("email") is not None and t.column("email").mask == "email"  # type: ignore[union-attr]
    assert t.column("nope") is None
    q = lakehouse.table("gold.daily_revenue").query  # type: ignore[union-attr]
    assert q.alias == "o" and q.joins[0].kind == "left" and q.joins[0].alias == "c"


def test_target_columns_include_system_columns(lakehouse):
    bronze = dict(spec.target_columns(lakehouse.table("bronze.orders")))  # type: ignore[arg-type]
    assert bronze["_rescued_data"] == "string" and bronze["order_id"] == "string"
    silver1 = [n for n, _ in spec.target_columns(lakehouse.table("silver.orders"))]  # type: ignore[arg-type]
    assert "op" not in silver1 and silver1[-2:] == ["_source_file", "_ingested_at"]
    silver2 = dict(spec.target_columns(lakehouse.table("silver.customers")))  # type: ignore[arg-type]
    assert silver2["__START_AT"] == "timestamp" and silver2["__END_AT"] == "timestamp"
    gold = [n for n, _ in spec.target_columns(lakehouse.table("gold.daily_revenue"))]  # type: ignore[arg-type]
    assert gold == ["order_date", "country", "orders_count", "revenue", "avg_order_value"]


def test_every_problem_is_reported_not_just_the_first(tmp_path: Path):
    root = tmp_path / "spec"
    (root / "tables").mkdir(parents=True)
    (root / "platform.toml").write_text(
        '[platform]\nname = 5\n[[groups]]\nname = "g"\n', encoding="utf-8"
    )
    (root / "tables" / "a.toml").write_text(
        '[table]\nname = "a"\nlayer = "bronze"\n[cdc]\nscd = "two"\n', encoding="utf-8"
    )
    (root / "tables" / "b.toml").write_text("this is not toml", encoding="utf-8")
    lh, problems = spec.load(root)
    assert lh is not None
    text = "\n".join(problems)
    assert "'name' must be a string" in text
    assert "group[0]: 'role' is required" in text
    assert "a.toml [table]: 'owner' is required" in text
    assert "'scd' must be 1 or 2" in text
    assert "b.toml" in text
    assert len(problems) >= 8


def test_missing_platform_file(tmp_path: Path):
    lh, problems = spec.load(tmp_path)
    assert lh is None and "does not exist" in problems[0]


def test_unreadable_platform_file(tmp_path: Path):
    (tmp_path / "platform.toml").write_text("= nope", encoding="utf-8")
    lh, problems = spec.load(tmp_path)
    assert lh is None and problems


def test_wrong_shapes_are_reported(tmp_path: Path):
    root = tmp_path
    (root / "tables").mkdir()
    (root / "platform.toml").write_text(
        'grants = "x"\nowners = 3\n[platform]\nname = "p"\ndescription = "d"\n'
        'catalog_pattern = "c_{env}"\nenvironments = "dev"\nlanding_volume = "raw"\n',
        encoding="utf-8",
    )
    (root / "tables" / "t.toml").write_text(
        '[table]\nname = "t"\nlayer = "silver"\nowner = "o"\ndomain = "d"\n'
        'classification = "internal"\ncomment = "c"\ncolumns = 5\n'
        '[source]\nformat = "json"\npath = "p"\noptions = 4\n'
        '[[columns]]\nname = "x"\ntype = "string"\ncomment = "c"\npii = "yes"\n'
        '[[expectations]]\nname = "e"\nrule = "x IS NOT NULL"\n',
        encoding="utf-8",
    )
    _, problems = spec.load(root)
    text = "\n".join(problems)
    for fragment in (
        "'grants' must be an array",
        "'environments' must be a list",
        "'options' must be a table",
        "'pii' must be true or false",
    ):
        assert fragment in text, fragment


def test_spec_directory_contents_match_the_loaded_tables(lakehouse):
    files = sorted(p.name for p in (SPEC / "tables").glob("*.toml"))
    assert sorted(t.file for t in lakehouse.tables) == files
