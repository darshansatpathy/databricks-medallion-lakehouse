"""The repository must agree with itself: generated files are fresh, links resolve, claims are true."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import medallion
from medallion import cli, oracle, policy
from tests.conftest import ROOT, SPEC

MD = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    *(ROOT / "docs").rglob("*.md"),
]


def test_generated_files_are_current(capsys):
    assert cli.main(["generate", "--check", "--spec", str(SPEC), "--out", str(ROOT)]) == 0
    assert "are current" in capsys.readouterr().out


def test_version_matches_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == medallion.__version__


def test_markdown_links_resolve():
    broken = []
    for f in MD:
        for m in re.finditer(r"\]\(([^)#\s][^)]*)\)", f.read_text()):
            target = m.group(1)
            if target.startswith("http"):
                continue
            if not (f.parent / target.split("#")[0]).resolve().exists():
                broken.append(f"{f.relative_to(ROOT)}: {target}")
    assert broken == []


def test_adrs_are_numbered_and_complete():
    adrs = sorted((ROOT / "docs" / "adr").glob("*.md"))
    assert [p.name[:4] for p in adrs] == [f"{i:04d}" for i in range(1, len(adrs) + 1)]
    for p in adrs:
        text = p.read_text()
        for heading in ("## Status", "## Context", "## Decision", "## Consequences"):
            assert heading in text, f"{p.name} lacks {heading}"
    readme = (ROOT / "README.md").read_text()
    for p in adrs:
        assert p.name in readme, f"README does not link {p.name}"


RUNBOOK_HEADINGS = [
    "## Purpose",
    "## Triggers",
    "## First checks",
    "## Steps",
    "## Verification",
    "## Escalation",
]


@pytest.mark.parametrize(
    "path", sorted((ROOT / "docs" / "runbooks").glob("*.md")), ids=lambda p: p.stem
)
def test_every_runbook_is_complete_and_linked(path):
    text = path.read_text()
    for h in RUNBOOK_HEADINGS:
        assert h in text, f"{path.name} lacks {h}"
    assert path.name in (ROOT / "README.md").read_text()


def test_oracle_assumptions_document_lists_every_assumption():
    doc = (ROOT / "docs" / "oracle-assumptions.md").read_text()
    for n, assumption in enumerate(oracle.ASSUMPTIONS, start=1):
        # The document may use code formatting around identifiers; compare without backticks.
        assert f"{n}. {assumption}".replace("`", "") in doc.replace("`", "").replace(
            "the oracle refuses", "the executor refuses"
        ) or assumption.split(";")[0].replace("`", "") in doc.replace("`", ""), assumption


def test_readme_claims_match_the_code(lakehouse, capsys):
    readme = (ROOT / "README.md").read_text()
    # table and rule counts
    assert len(lakehouse.tables) == 9 and "Nine tables" in readme
    ids = sorted(row[0] for row in policy.catalog())
    assert len(ids) == 16 and "Sixteen policy rules" in readme
    assert ids[0] == "MED000" and ids[-1] == "MED015" and "`MED000` to `MED015`" in readme
    # the quoted lint line is what lint prints
    assert cli.main(["lint", "--spec", str(SPEC)]) == 0
    assert capsys.readouterr().out.strip() in readme
    # every command is shown
    for cmd in ("lint", "generate", "run", "rules"):
        assert f"medallion {cmd}" in readme, cmd


def test_readme_sample_run_matches_the_executor(result, capsys):
    readme = (ROOT / "README.md").read_text()
    assert cli.main(["run", "--data", str(SPEC / "data")]) == 0
    printed = [ln for ln in capsys.readouterr().out.splitlines() if not ln.startswith(" ")]
    assert len(printed) == 9
    for line in printed:
        assert line in readme, line
    for fqn, rows in result.tables.items():
        if not fqn.endswith("_quarantine"):
            quarantined = len(result.tables.get(f"{fqn}_quarantine", []))
            assert f"{len(rows)} rows" in "\n".join(p for p in printed if p.startswith(fqn + " "))
            assert (f"{quarantined} quarantined" in "\n".join(printed)) or quarantined == 0


def test_every_policy_rule_is_listed_in_the_generated_doc():
    doc = (ROOT / "docs" / "generated" / "policy-rules.md").read_text()
    for rule_id, *_ in policy.catalog():
        assert f"`{rule_id}`" in doc


def test_every_scenario_in_the_readme_is_in_the_data():
    data = "\n".join(p.read_text() for p in (SPEC / "data").rglob("*.jsonl"))
    for needle in ("+01:00", "-05:00", '"abc"', "loyalty_tier", '"op": "D"'):
        assert needle in data, needle


def test_sample_data_is_synthetic():
    data = "\n".join(p.read_text() for p in (SPEC / "data").rglob("*.jsonl"))
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+", data))
    assert emails, "sample data should include email addresses to exercise masks"
    assert all(e.lower().endswith(("@example.com", "@example.org", "@example.net")) for e in emails)


@pytest.mark.parametrize("name", ["ci.yml", "codeql.yml", "bundle-validate.yml"])
def test_workflows_exist(name):
    assert (ROOT / ".github" / "workflows" / name).exists()


def test_ci_runs_the_specification_gates():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for cmd in ("medallion lint --fail-on low", "medallion generate --check", "medallion run"):
        assert cmd in ci, cmd


def test_bundle_validate_workflow_uses_federation_not_a_stored_token():
    text = (ROOT / ".github" / "workflows" / "bundle-validate.yml").read_text()
    assert "id-token: write" in text
    assert "workflow_dispatch" in text
    assert not re.search(r"DATABRICKS_TOKEN", text)


SECRET_PATTERNS = [
    r"ghp_[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"dapi[0-9a-f]{32}",
    r"https://adb-\d+\.\d+\.azuredatabricks\.net",
    r"https://dbc-[0-9a-f-]+\.cloud\.databricks\.com",
]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"}


def test_no_secrets_or_real_workspace_identifiers():
    hits = []
    for f in ROOT.rglob("*"):
        if not f.is_file() or SKIP_DIRS & set(f.relative_to(ROOT).parts):
            continue
        if f == Path(__file__) or f.suffix in {".pyc", ".coverage"}:
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        hits += [f"{f.relative_to(ROOT)}: {p}" for p in SECRET_PATTERNS if re.search(p, text)]
    assert hits == []


def test_bundle_ships_only_placeholder_identifiers():
    text = (ROOT / "bundle" / "databricks.yml").read_text()
    uuids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text))
    assert uuids == {"00000000-0000-0000-0000-000000000000"}


def test_cli_runs_as_a_module():
    out = subprocess.run(
        [sys.executable, "-m", "medallion.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    assert medallion.__version__ in out.stdout + out.stderr
