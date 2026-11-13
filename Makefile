.PHONY: install check test verify generate run audit

install:
	python -m venv .venv && .venv/bin/pip install -e ".[dev]"

check:
	ruff check . && ruff format --check . && mypy && pytest --cov=medallion --cov-fail-under=85

test:
	pytest

verify:
	medallion lint --fail-on low && medallion generate --check && medallion run

generate:
	medallion generate

run:
	medallion run

audit:
	pip-audit --skip-editable
