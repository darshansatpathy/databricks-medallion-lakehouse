# ADR 0001: The specification is the source of truth; SQL is generated

## Status

Accepted

## Context

A lakehouse is many near-identical tables. Written by hand, the same decisions (a bronze table is
text, every silver table has a key and a sequence, personal data is masked, the open version of a
type 2 table is the one you join) are re-made table by table and drift. Reviewers cannot see at a
glance whether a new table follows the rules.

## Decision

Each table is described in a small TOML file; the platform (environments, owners, groups, grants) is
described in `platform.toml`. `medallion lint` checks the specification against 16 policy rules.
`medallion generate` writes the pipeline SQL, the Unity Catalog SQL, the asset bundle and the
documents. A CI step regenerates and fails if the committed files differ. Nobody edits generated
files.

## Consequences

- A pull request that adds a table is a TOML file; the generated SQL shows in the diff.
- A rule such as "personal data must be masked" is enforced for every table, including ones written
  next year.
- The generator is code that must be right. It has golden-style tests, a structural comparison with
  the reference executor, and has not yet been run against a workspace (see the README).
- Anything the specification cannot express needs a new field or a deliberate escape hatch; there is
  none today.
