# Contributing

Thanks for taking the time to improve this project.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make check          # ruff, mypy --strict, pytest with 85% coverage gate
make verify         # lint the spec, check generated files are current, run the sample
```

## What a change needs

- Change `spec/`, never the generated files. Run `medallion generate` and commit the result; CI fails if they differ.
- A new or changed table passes `medallion lint`. A rule that blocks a justified change is waived in `spec/waivers.toml` with a reason and an expiry date, never switched off.
- A behaviour change in typing, expectations, change handling, masking or gold queries has a test and, where the sample data does not cover it, new sample data in `spec/data/`.
- A change that depends on how Databricks behaves updates `docs/oracle-assumptions.md`.
- Expressions in a spec stay inside the portable subset (`src/medallion/exprcheck.py`).
- Decisions that change a default or a trust boundary get an ADR in `docs/adr/`.
- Update `CHANGELOG.md` for anything a user would notice.

## Pull requests

Keep them focused, describe the behaviour change and how you tested it, and make sure
CI is green. Security issues go through private reporting, see [SECURITY.md](SECURITY.md).
