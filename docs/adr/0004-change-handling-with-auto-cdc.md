# ADR 0004: Change handling with AUTO CDC, not hand-written MERGE

## Status

Accepted

## Context

Source systems send inserts, updates and deletes, and they arrive late and out of order. A `MERGE`
that is correct under reordering, duplicates and deletes is long and easy to get subtly wrong.

## Decision

Silver targets are streaming tables fed by an `AUTO CDC` flow with explicit `KEYS`, `SEQUENCE BY`
and `APPLY AS DELETE WHEN`. A table is either type 1 (latest row per key) or type 2 (every version,
with `__START_AT` and `__END_AT`). For type 2 the flow tracks history on business columns only, with
`TRACK HISTORY ON * EXCEPT (sequence column, _source_file, _ingested_at)`, so metadata that changes on
every event does not open a version. Gold reads type 2 tables through their open version, enforced by
rule `MED010`.

## Consequences

- Ordering and deduplication are the platform's job, not ours.
- The sequence column must be reliable and non-null; rule `MED005` requires it.
- Two different events with the same key and sequence value are ambiguous. The reference executor
  refuses them; the source contract should rule them out.
- Behaviour on a real workspace for type 2 no-op updates is an assumption until confirmed
  ([oracle assumptions](../oracle-assumptions.md)).
