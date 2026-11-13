from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from medallion import cli
from tests.conftest import ROOT, SPEC, Draft, write_jsonl


def test_lint_passes_on_the_shipped_spec(capsys):
    assert cli.main(["lint", "--spec", str(SPEC)]) == 0
    assert capsys.readouterr().out.strip() == "9 tables, 15 rules: 0 findings, 0 waived"


def break_a_comment(d: Draft) -> None:
    d.col("silver.orders", "status").update(comment="x")


def test_lint_fails_and_prints_findings(make_spec, capsys):
    assert cli.main(["lint", "--spec", str(make_spec(break_a_comment))]) == 1
    out = capsys.readouterr().out
    assert (
        "MEDIUM   MED008 silver.orders.status: column comment must be at least 5 characters" in out
    )
    assert "1 findings" in out


def test_fail_on_threshold(make_spec):
    spec_dir = str(make_spec(break_a_comment))
    assert cli.main(["lint", "--spec", spec_dir, "--fail-on", "high"]) == 0
    assert cli.main(["lint", "--spec", spec_dir, "--fail-on", "low"]) == 1


def test_lint_markdown_includes_findings_and_waivers(make_spec, capsys):
    def mutate(d: Draft) -> None:
        break_a_comment(d)
        d.extra["waivers.toml"] = (
            '[[waivers]]\nrule = "MED008"\nsubject = "silver.orders.status"\n'
            'reason = "Comment lands with the next release train"\nexpires = "2027-01-01"\n'
        )

    spec_dir = str(make_spec(mutate))
    assert (
        cli.main(["lint", "--spec", spec_dir, "--format", "markdown", "--today", "2026-11-13"]) == 0
    )
    out = capsys.readouterr().out
    assert out.startswith("## Specification lint") and "Waivers in force" in out
    assert "| MED008 | silver.orders.status | 2027-01-01 |" in out
    assert cli.main(["lint", "--spec", spec_dir, "--format", "text", "--today", "2026-11-13"]) == 0
    assert "WAIVED   MED008" in capsys.readouterr().out
    assert cli.main(["lint", "--spec", spec_dir, "--today", "2027-06-01"]) == 1
    capsys.readouterr()
    assert (
        cli.main(["lint", "--spec", spec_dir, "--format", "markdown", "--today", "2027-06-01"]) == 1
    )
    assert "| high | MED000 |" in capsys.readouterr().out


def test_lint_markdown_table_for_findings(make_spec, capsys):
    cli.main(["lint", "--spec", str(make_spec(break_a_comment)), "--format", "markdown"])
    assert "| medium | MED008 | silver.orders.status |" in capsys.readouterr().out


def test_missing_spec_is_reported(tmp_path, capsys):
    assert cli.main(["lint", "--spec", str(tmp_path)]) == 1
    assert "does not exist" in capsys.readouterr().out


def repo_copy(tmp_path: Path) -> Path:
    for part in ("spec",):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def test_generate_writes_then_check_passes_then_detects_drift(tmp_path, capsys):
    root = repo_copy(tmp_path)
    args = ["--spec", str(root / "spec"), "--out", str(root)]
    assert cli.main(["generate", *args]) == 0
    assert capsys.readouterr().out.strip() == "wrote 22 files"
    assert cli.main(["generate", "--check", *args]) == 0
    assert "22 generated files are current" in capsys.readouterr().out

    target = root / "bundle/src/pipelines/gold/daily_revenue.sql"
    target.write_text("-- edited by hand\n", encoding="utf-8")
    assert cli.main(["generate", "--check", *args]) == 1
    assert "stale: bundle/src/pipelines/gold/daily_revenue.sql" in capsys.readouterr().err

    assert cli.main(["generate", *args]) == 0
    (root / "docs/generated/lineage.md").unlink()
    capsys.readouterr()
    assert cli.main(["generate", "--check", *args]) == 1
    assert "stale: docs/generated/lineage.md" in capsys.readouterr().err

    cli.main(["generate", *args])
    extra = root / "bundle/src/pipelines/gold/leftover.sql"
    extra.write_text("select 1;", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["generate", "--check", *args]) == 1
    assert "leftover.sql (not generated any more)" in capsys.readouterr().err


def test_generate_removes_files_for_tables_that_no_longer_exist(tmp_path, capsys):
    root = repo_copy(tmp_path)
    args = ["--spec", str(root / "spec"), "--out", str(root)]
    cli.main(["generate", *args])
    stale = root / "bundle/src/pipelines/gold/old_table.sql"
    stale.write_text("select 1;", encoding="utf-8")
    cli.main(["generate", *args])
    assert not stale.exists()


def test_generate_refuses_a_spec_with_serious_findings(make_spec, tmp_path, capsys):
    def mutate(d: Draft) -> None:
        d.col("silver.customers", "email").pop("mask")

    spec_dir = make_spec(mutate)
    assert cli.main(["generate", "--spec", str(spec_dir), "--out", str(tmp_path / "out")]) == 1
    captured = capsys.readouterr()
    assert "MED007" in captured.out and "fix the findings" in captured.err
    assert not (tmp_path / "out").exists()


def test_generate_tolerates_low_and_medium_findings(make_spec, tmp_path):
    assert (
        cli.main(
            ["generate", "--spec", str(make_spec(break_a_comment)), "--out", str(tmp_path / "o")]
        )
        == 0
    )


def test_generate_without_sample_data_skips_the_sample_run(tmp_path):
    root = repo_copy(tmp_path)
    shutil.rmtree(root / "spec" / "data")
    assert cli.main(["generate", "--spec", str(root / "spec"), "--out", str(root)]) == 0
    assert not (root / "docs/generated/sample-run.md").exists()


def test_run_prints_tables_and_violations(capsys):
    assert cli.main(["run", "--spec", str(SPEC)]) == 0
    out = capsys.readouterr().out
    assert "silver.orders                           8 rows, 5 quarantined" in out
    assert "silver.orders amount_valid (drop): 3 of 17 rows violate" in out
    assert "gold.daily_revenue" in out


def test_run_reports_a_failed_expectation_and_ambiguous_ordering(tmp_path, capsys):
    root = repo_copy(tmp_path)
    data = root / "spec" / "data"
    shutil.rmtree(data)
    base = {
        "customer_id": "C",
        "status": "new",
        "amount": "1",
        "currency": "usd",
        "order_ts": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    write_jsonl(data / "orders", "a.jsonl", [{**base, "order_id": "A", "op": "X"}])
    for n in ("customers", "payments"):
        (data / n).mkdir()
    assert cli.main(["run", "--spec", str(root / "spec")]) == 1
    assert "expectation op_known (fail)" in capsys.readouterr().err
    write_jsonl(
        data / "orders",
        "a.jsonl",
        [
            {**base, "order_id": "A", "op": "I"},
            {**base, "order_id": "A", "op": "I", "status": "completed"},
        ],
    )
    assert cli.main(["run", "--spec", str(root / "spec")]) == 1
    assert "ordering is undefined" in capsys.readouterr().err


def test_run_accepts_a_data_directory_and_environment(tmp_path, capsys):
    data = tmp_path / "d"
    for n in ("orders", "customers", "payments"):
        (data / n).mkdir(parents=True)
    assert cli.main(["run", "--spec", str(SPEC), "--data", str(data), "--env", "prod"]) == 0
    assert "silver.orders" in capsys.readouterr().out


def test_run_refuses_a_broken_spec(make_spec, capsys):
    spec_dir = make_spec(lambda d: d.col("silver.customers", "email").pop("mask"))
    assert cli.main(["run", "--spec", str(spec_dir)]) == 1
    assert "MED007" in capsys.readouterr().out


def test_rules_lists_every_rule(capsys):
    assert cli.main(["rules"]) == 0
    out = capsys.readouterr().out
    assert "MED000" in out and "MED015" in out and len(out.strip().splitlines()) == 16


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "medallion 0.1.0" in capsys.readouterr().out


def test_every_command_is_documented_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sub = cli.build_parser()._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    for name in sub:
        assert f"medallion {name}" in readme, name
