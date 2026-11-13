# ADR 0003: Three expectation actions, and a quarantine table for drops

## Status

Accepted

## Context

An expectation that only counts violations changes nothing downstream. One that stops the pipeline
on every bad row makes the pipeline fragile. Dropping rows silently hides data loss.

## Decision

Each silver expectation has one of three actions.

- `warn`: keep the row and count the violation. For things worth watching that do not make a row wrong.
- `drop`: remove the row from silver and write it, with the names of the rules it failed, to
  `<name>_quarantine`. For rows that are unusable: no key, no sequence, an unparsable amount.
- `fail`: stop the update. Only for breaks of the source contract where continuing would corrupt
  downstream tables, such as an unknown change operation that might be a delete.

Lint requires every key and sequence column to be covered by a `drop` or `fail` expectation that
requires it to be non-null, and a table that applies deletes to have a `fail` expectation on its
operation column.

## Consequences

- Bad data is visible and replayable: correct it at the source and send a new event with a later
  sequence value.
- `fail` can stop a pipeline, so it is used sparingly and each use is listed in the
  [quality register](../generated/quality.md).
- Quarantine holds raw-quality data, including personal data, so it is masked and granted like silver.
