# What the reference executor assumes

`medallion run` builds the whole lakehouse from the sample files in `spec/data/` without Spark. It
exists so that the *meaning* of the specification (what a delete does, what a late event does, which
rows are quarantined, what a gold total is) is tested, not just the shape of the SQL.

It is a model. It applies the rules the generated SQL expresses, on SQLite and Python. It was not
checked against a Databricks workspace, so every assumption below is a claim about the Databricks
runtime that a real run must confirm. When a real run disagrees, change the executor and the test,
and say so in the changelog.

## Assumptions about the runtime

1. An expectation passes only when its expression is true; NULL counts as a violation.
2. A `drop` expectation removes the row; a `fail` expectation stops the update; `warn` keeps the row and counts it.
3. Events for a key are applied in sequence order whatever their arrival order. An exact redelivery (same key, sequence and values) is collapsed; two different events with the same key and sequence are ambiguous and the executor refuses them.
4. A type 2 event whose tracked columns equal the open version does not open a new version. The generated flow tracks every business column and none of `_source_file`, `_ingested_at` or the sequence column.
5. A delete closes the open version of a type 2 key and removes a type 1 key; a later insert starts again.
6. Timestamps without a zone are read as UTC (the pipeline sets `spark.sql.session.timeZone` to `UTC`).
7. Decimals are held as floats, rounded half-up to scale on cast, so aggregates are compared to the cent.

## Where SQLite and Spark differ, and what the specification does about it

| Difference | Handling |
|---|---|
| `/` is integer division on integers in SQLite, decimal in Spark | `/` and `%` are not in the portable expression subset |
| `LIKE` is case-insensitive in SQLite, case-sensitive in Spark | `LIKE` and its relatives are not in the subset |
| `lower` and `upper` map only ASCII in SQLite | The executor registers Python versions that map Unicode, as Spark does |
| `'a''b'` is one string in SQLite, two concatenated strings in Spark; backslash escapes differ | Quotes inside literals and backslashes are rejected |
| `CAST(x AS STRING)` is not a text cast in SQLite | `CAST` is not in the subset; types are applied by the spec's column types |
| Timestamps are a native type in Spark | Held as normalised UTC text, which sorts chronologically |

The subset is enforced by rule `MED011` and by `src/medallion/exprcheck.py`, which has a test for
each trap above.

## Trust

The executor builds SQLite statements from specification text, because that is how it checks what the
specification means. `medallion run` refuses to start when the policy reports a finding at high or above,
and `MED011` (the portable expression subset) is critical, so an expression outside the subset never
reaches SQLite. The specification is still *trusted input* written in this repository and reviewed through
`CODEOWNERS`. Do not point the executor at a specification from an untrusted source.

## What is not modelled

- Streaming mechanics: micro-batches, checkpoints, watermarks, retries.
- Schema evolution and Auto Loader schema inference (bronze declares its schema; unexpected fields are rescued).
- Performance, clustering, file layout and cost.
- Unity Catalog enforcement. Masks are modelled as functions of "privileged or not".
