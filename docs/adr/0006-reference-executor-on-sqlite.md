# ADR 0006: A reference executor on SQLite, with a portable expression subset

## Status

Accepted

## Context

Spark and Databricks cannot run in every developer environment or in a fast test job. Without
something executable, tests would only compare generated text with expected text, which proves the
generator is stable, not that the pipeline means what the specification says.

## Decision

`medallion run` builds the lakehouse from sample files with a small executor: Python for typing and
change handling, SQLite for the expressions in expectations, columns and gold queries. To make that
honest, specification expressions are limited to a subset whose meaning is the same on both engines
(`MED011`), and the gold query the executor runs is compared in a test with the Spark SQL the
generator emits.

## Consequences

- Semantics (late events, deletes, history, quarantine, masks, totals) are tested with hand-derived
  expected results, and a failing generator change shows up as a changed result.
- Some SQL features are unavailable in specifications. That is the price of the guarantee.
- The executor encodes assumptions about the runtime. They are listed in
  [oracle-assumptions](../oracle-assumptions.md) and must be confirmed on a workspace.
- It does not replace a run on Databricks. It narrows what a first run can reveal.
