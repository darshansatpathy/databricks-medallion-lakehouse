# ADR 0005: Column masks are bound in the table definition, with the pipeline exempt

## Status

Accepted

## Context

A column mask is applied whenever a table is read, including by the pipeline that builds the next
table. If the pipeline's identity is masked, it copies masked values downstream and the data is
silently destroyed. Masks bound by a separate script after the fact are also easy to forget.

## Decision

- Mask functions live in the `governance` schema and are created before the pipeline runs.
- A column marked `pii` names a mask (`email`, `redact`, `last4`, `hash`), and the generated table
  definition binds it with `MASK`. This happens on bronze, the validated staging table, the silver
  target, the quarantine table and gold, because schema-level grants cover every table in a schema.
- Every mask function returns the real value to two groups: the privileged readers and the pipeline's
  runner group. Exactly one runner group must be declared (rule `MED012`).
- Rule `MED007` fails the build if a gold column derives from a personal column without being marked
  `pii` itself, unless it only counts.

## Consequences

- The runner identity can read real values; it must be a service principal with no interactive use.
- Row filters and column masks are not applied to Delta Sharing recipients. Do not share these tables
  directly; share a view built for the purpose.
- Changing a mask is `CREATE OR REPLACE FUNCTION` and takes effect on the next read.
