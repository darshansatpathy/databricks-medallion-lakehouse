from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from medallion import gen_bundle, gen_uc, yamlout

ROOT = Path(__file__).resolve().parents[1]


# ---- Unity Catalog ---------------------------------------------------------------------------------


def test_one_governance_file_per_environment(lakehouse):
    files = gen_uc.render(lakehouse)
    assert sorted(files) == [
        f"src/governance/{e}.sql" for e in sorted(lakehouse.platform.environments)
    ]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_governance_sql_resolves_the_catalog_and_creates_everything_idempotently(lakehouse, env):
    sql = gen_uc.render_env(lakehouse, env)
    cat = f"retail_{env}"
    assert "${" not in sql
    assert f"CREATE CATALOG IF NOT EXISTS {cat} " in sql
    for schema in ("landing", "bronze", "silver", "gold", "governance"):
        assert f"CREATE SCHEMA IF NOT EXISTS {cat}.{schema} " in sql
    assert f"CREATE VOLUME IF NOT EXISTS {cat}.landing.raw " in sql
    for stmt in re.findall(r"^CREATE [A-Z ]+", sql, re.M):
        assert "IF NOT EXISTS" in stmt or "OR REPLACE" in stmt
    assert sql.rstrip().endswith("`lakehouse_admins`;")


def test_mask_functions_exist_for_every_mask_used(lakehouse):
    sql = gen_uc.render_env(lakehouse, "dev")
    used = {c.mask for t in lakehouse.tables for c in t.columns if c.mask}
    assert used == {"email", "redact"}
    for kind in used:
        assert f"CREATE OR REPLACE FUNCTION retail_dev.governance.mask_{kind}(v STRING)" in sql
    assert "mask_hash" not in sql  # only masks that are used are created


def test_mask_functions_exempt_privileged_and_runner_groups_only(lakehouse):
    sql = gen_uc.render_env(lakehouse, "dev")
    assert sql.count("is_account_group_member('lakehouse_pii_readers')") == 2
    assert sql.count("is_account_group_member('lakehouse_pipeline_runners')") == 2
    assert "is_account_group_member('lakehouse_analysts')" not in sql
    assert "WHEN v IS NULL THEN NULL" in sql


@pytest.mark.parametrize("kind", ["email", "redact", "last4", "hash"])
def test_every_mask_kind_renders(kind):
    sql = gen_uc.mask_function_sql("c", kind, ["g1", "g2"])
    assert f"mask_{kind}(v STRING)" in sql and "RETURNS STRING" in sql and "ELSE" in sql


def test_grants_follow_the_spec_per_environment(lakehouse):
    prod = gen_uc.render_env(lakehouse, "prod")
    dev = gen_uc.render_env(lakehouse, "dev")
    assert "ON SCHEMA retail_dev.bronze TO `lakehouse_engineers`" in dev
    assert "ON SCHEMA retail_prod.bronze TO `lakehouse_engineers`" not in prod
    assert "GRANT USE SCHEMA, SELECT ON SCHEMA retail_prod.gold TO `lakehouse_analysts`;" in prod
    assert (
        "GRANT ALL PRIVILEGES ON SCHEMA retail_prod.silver TO `lakehouse_pipeline_runners`;" in prod
    )
    assert "USE SCHEMA, ALL PRIVILEGES" not in prod
    for sql in (dev, prod):
        grants = [line for line in sql.splitlines() if line.startswith("GRANT")]
        assert grants and all(re.search(r"TO `[a-z_]+`;$", line) for line in grants)


def test_no_consumer_group_can_reach_silver_or_bronze(lakehouse):
    for env in lakehouse.platform.environments:
        sql = gen_uc.render_env(lakehouse, env)
        for group in ("lakehouse_analysts", "lakehouse_data_scientists"):
            lines = [line for line in sql.splitlines() if f"`{group}`" in line and "SCHEMA" in line]
            assert lines and all(".gold " in line for line in lines), group


# ---- YAML writer ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"a": 1, "b": True, "c": None, "d": "text", "e": 1.5},
        {"nested": {"x": {"y": [1, 2, {"z": "q"}]}}},
        {"list": [{"a": 1, "b": 2}, {"c": [3, 4]}], "empty": [], "none": {}},
        {
            "strings": [
                "yes",
                "no",
                "null",
                "true",
                "123",
                "a: b",
                "# c",
                "- d",
                "${var.x}",
                "",
                "'q'",
                'say "hi"',
            ]
        },
        {"k": "multi word value", "path": "../src/**", "u": "ü"},
        {"nested_lists": [[1, 2], [3]]},
    ],
)
def test_yaml_writer_roundtrips_through_a_real_parser(data):
    assert yaml.safe_load(yamlout.dump(data)) == data


# ---- Bundle ------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundle(lakehouse):
    return {k: yaml.safe_load(v) for k, v in gen_bundle.render(lakehouse).items()}


def test_bundle_files_are_valid_yaml_with_expected_names(bundle):
    assert sorted(bundle) == [
        "databricks.yml",
        "resources/retail_lakehouse.job.yml",
        "resources/retail_lakehouse.pipeline.yml",
    ]
    assert bundle["databricks.yml"]["bundle"]["name"] == "retail-lakehouse"
    assert bundle["databricks.yml"]["include"] == ["resources/*.yml"]


def test_targets_match_environments_and_set_the_catalog(lakehouse, bundle):
    targets = bundle["databricks.yml"]["targets"]
    assert list(targets) == list(lakehouse.platform.environments)
    for env, t in targets.items():
        assert t["variables"]["catalog"] == lakehouse.platform.catalog(env)
    assert targets["dev"]["mode"] == "development" and targets["dev"]["default"] is True
    for env in ("staging", "prod"):
        assert targets[env]["mode"] == "production"
        assert targets[env]["run_as"]["service_principal_name"] == "${var.run_as_service_principal}"
    assert targets["prod"]["variables"]["schedule_status"] == "UNPAUSED"
    assert "schedule_status" not in targets["dev"]["variables"]


def test_every_variable_reference_is_declared(bundle):
    declared = set(bundle["databricks.yml"]["variables"])
    text = yamlout.dump(bundle)
    used = set(re.findall(r"\$\{var\.(\w+)\}", text))
    assert used and used <= declared
    assert (
        "warehouse_id" in declared
        and "default" not in bundle["databricks.yml"]["variables"]["warehouse_id"]
    )


def test_the_placeholder_service_principal_is_obviously_a_placeholder(bundle):
    assert (
        bundle["databricks.yml"]["variables"]["run_as_service_principal"]["default"]
        == "00000000-0000-0000-0000-000000000000"
    )


def test_pipeline_points_at_the_generated_sql(bundle):
    p = bundle["resources/retail_lakehouse.pipeline.yml"]["resources"]["pipelines"][
        "retail_lakehouse"
    ]
    assert p["serverless"] is True and p["catalog"] == "${var.catalog}" and p["continuous"] is False
    assert p["configuration"]["catalog"] == "${var.catalog}"
    assert p["configuration"]["spark.sql.session.timeZone"] == "UTC"
    glob = p["libraries"][0]["glob"]["include"]
    resources_dir = ROOT / "bundle" / "resources"
    sql_files = list((resources_dir / glob.replace("**", "")).resolve().rglob("*.sql"))
    assert len(sql_files) == 9


def test_job_runs_governance_before_the_pipeline_and_references_real_files(lakehouse, bundle):
    job = bundle["resources/retail_lakehouse.job.yml"]["resources"]["jobs"][
        "retail_lakehouse_daily"
    ]
    keys = [t["task_key"] for t in job["tasks"]]
    assert keys == ["apply_governance", "refresh_pipeline"]
    assert job["tasks"][1]["depends_on"] == [{"task_key": "apply_governance"}]
    assert (
        job["tasks"][1]["pipeline_task"]["pipeline_id"]
        == "${resources.pipelines.retail_lakehouse.id}"
    )
    assert (
        job["max_concurrent_runs"] == 1
        and job["schedule"]["pause_status"] == "${var.schedule_status}"
    )
    path = job["tasks"][0]["sql_task"]["file"]["path"]
    for env in lakehouse.platform.environments:
        resolved = (ROOT / "bundle" / "resources" / path.replace("${bundle.target}", env)).resolve()
        assert resolved.exists(), resolved


def test_pipeline_resource_key_matches_the_job_reference(bundle):
    assert (
        "retail_lakehouse"
        in bundle["resources/retail_lakehouse.pipeline.yml"]["resources"]["pipelines"]
    )
