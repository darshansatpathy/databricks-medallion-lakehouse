"""Command line: ``medallion lint | generate | run | rules``."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__, docs, gen_bundle, gen_sql, gen_uc, oracle, policy, spec

GENERATED_DIRS = (
    "bundle/src/pipelines",
    "bundle/src/governance",
    "bundle/resources",
    "docs/generated",
)
GENERATED_FILES = ("bundle/databricks.yml",)


def _load(spec_dir: Path, today: date) -> tuple[spec.Lakehouse | None, policy.Report]:
    lh, problems = spec.load(spec_dir)
    waivers, wproblems = policy.load_waivers(spec_dir / "waivers.toml")
    return lh, policy.run(lh, problems + wproblems, today, waivers)


def generate_all(lh: spec.Lakehouse, data_root: Path | None) -> dict[str, str]:
    """Every generated file, keyed by path relative to the repository root."""
    out: dict[str, str] = {}
    for rel, text in {**gen_sql.render(lh), **gen_uc.render(lh), **gen_bundle.render(lh)}.items():
        out[f"bundle/{rel}"] = text
    out.update(docs.render(lh, data_root))
    return out


def _print_report(report: policy.Report, fmt: str) -> None:
    if fmt == "markdown":
        print(
            f"## Specification lint\n\n{report.tables} tables checked by {report.rules_run} rules: "
            f"**{len(report.findings)} findings**, {len(report.waived)} waived.\n"
        )
        if report.findings:
            print("| Severity | Rule | Subject | Finding |\n|---|---|---|---|")
            for f in report.findings:
                print(f"| {f.severity} | {f.rule} | {f.subject} | {f.message} |")
        if report.waived:
            print(
                "\n### Waivers in force\n\n| Rule | Subject | Expires | Reason |\n|---|---|---|---|"
            )
            for f, w in report.waived:
                print(f"| {f.rule} | {w.subject} | {w.expires} | {w.reason} |")
        return
    for f in report.findings:
        print(f"{f.severity.upper():8} {f.rule} {f.subject}: {f.message}")
    for f, w in report.waived:
        print(f"WAIVED   {f.rule} {f.subject}: {f.message} (until {w.expires}: {w.reason})")
    print(
        f"{report.tables} tables, {report.rules_run} rules: {len(report.findings)} findings, {len(report.waived)} waived"
    )


def cmd_lint(a: argparse.Namespace) -> int:
    _, report = _load(Path(a.spec), date.fromisoformat(a.today))
    _print_report(report, a.format)
    return 1 if report.failing(a.fail_on) else 0


def cmd_generate(a: argparse.Namespace) -> int:
    lh, report = _load(Path(a.spec), date.fromisoformat(a.today))
    blocking = report.failing("high")
    if lh is None or blocking:
        _print_report(report, "text")
        print("error: fix the findings above before generating", file=sys.stderr)
        return 1
    root = Path(a.out)
    files = generate_all(lh, Path(a.spec) / "data")
    if a.check:
        stale = []
        for rel, text in files.items():
            p = root / rel
            if not p.exists() or p.read_text(encoding="utf-8") != text:
                stale.append(rel)
        for d in GENERATED_DIRS:
            folder = root / d
            if folder.exists():
                for f in folder.rglob("*"):
                    if f.is_file() and f.relative_to(root).as_posix() not in files:
                        stale.append(f"{f.relative_to(root).as_posix()} (not generated any more)")
        if stale:
            for s in stale:
                print(f"stale: {s}", file=sys.stderr)
            print(
                "error: generated files are out of date; run `medallion generate`", file=sys.stderr
            )
            return 1
        print(f"{len(files)} generated files are current")
        return 0
    for d in GENERATED_DIRS:
        folder = root / d
        if folder.exists():
            for f in sorted(folder.rglob("*"), reverse=True):
                if f.is_file():
                    f.unlink()
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} files")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    lh, report = _load(Path(a.spec), date.fromisoformat(a.today))
    if lh is None or report.failing("high"):
        _print_report(report, "text")
        return 1
    data = Path(a.data) if a.data else Path(a.spec) / "data"
    try:
        res = oracle.run(lh, data, env=a.env)
    except (oracle.ExpectationFailure, oracle.AmbiguousOrdering) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for t in lh.tables:
        q = f", {len(res.tables[t.fqn + '_quarantine'])} quarantined" if t.layer == "silver" else ""
        print(f"{t.fqn:36} {len(res.tables[t.fqn]):4} rows{q}")
    for m in res.metrics:
        if m.failed:
            print(
                f"  {m.table} {m.name} ({m.action}): {m.failed} of {m.passed + m.failed} rows violate"
            )
    return 0


def cmd_rules(a: argparse.Namespace) -> int:
    for rid, sev, title, _ in policy.catalog():
        print(f"{rid}  {sev:8} {title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="medallion", description=__doc__)
    p.add_argument("--version", action="version", version=f"medallion {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--spec", default="spec", help="specification directory (default: spec)")
        sp.add_argument(
            "--today", default=date.today().isoformat(), help="date used for waiver expiry"
        )

    lint = sub.add_parser("lint", help="check the specification against the policy rules")
    common(lint)
    lint.add_argument("--fail-on", choices=policy.SEVERITIES, default="medium")
    lint.add_argument("--format", choices=["text", "markdown"], default="text")
    lint.set_defaults(func=cmd_lint)

    gen = sub.add_parser("generate", help="write pipeline SQL, governance SQL, the bundle and docs")
    common(gen)
    gen.add_argument("--out", default=".", help="repository root to write into")
    gen.add_argument("--check", action="store_true", help="fail if generated files are out of date")
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser(
        "run", help="build the lakehouse from sample data with the reference executor"
    )
    common(run)
    run.add_argument("--data", help="sample data directory (default: <spec>/data)")
    run.add_argument("--env", default="dev")
    run.set_defaults(func=cmd_run)

    rules = sub.add_parser("rules", help="list the policy rules")
    rules.set_defaults(func=cmd_rules)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
