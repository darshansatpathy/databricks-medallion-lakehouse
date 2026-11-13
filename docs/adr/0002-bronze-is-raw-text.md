# ADR 0002: Bronze keeps every value as text

## Status

Accepted

## Context

Sources change without notice: a number arrives as `"12.50"`, then as `12.5`, then as `"n/a"`.
Typing at ingestion turns each of these into a failed or silently altered load, and the original is
lost.

## Decision

Bronze declares every column as `string`, reads files with an explicit schema, and rescues anything
that does not fit into `_rescued_data`. It adds the source file, the file's modification time and the
ingestion time. It takes no expectations that drop or fail rows; a warn-level `rescued_data_empty`
counts rows with rescued data. The tables set `pipelines.reset.allowed` to `false`, so a full refresh
cannot delete raw history.

## Consequences

- Ingestion does not break on bad data; typing and validation happen in silver, where there is a
  quarantine table.
- Bronze is roughly the size of the input and needs a retention policy of its own.
- Reprocessing raw files needs a deliberate change to the protection property, reviewed like any other.
