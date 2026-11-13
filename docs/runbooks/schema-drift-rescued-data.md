# Runbook: Fields are appearing in rescued data

> A starting point. Adapt the names, dashboards and contacts to your workspace; keep the structure.

## Purpose

Handle a source that has added, renamed or retyped fields.

## Triggers

- `rescued_data_empty` reports a non-zero count on a bronze table
- A source team announces a schema change

## First checks

1. Query the bronze table for rows where `_rescued_data IS NOT NULL`; what keys appear in the JSON?
2. Is the new field wanted downstream, or can it be ignored?

## Steps

1. Unwanted field: no action; it stays in `_rescued_data` and nothing breaks. Note it in the table owner's channel.
2. Wanted field: add a column to the bronze spec (type `string`), a typed column with an `expr` and a comment to the silver spec, and an expectation if it matters. Run `medallion lint`, `medallion generate`, and update the sample data and tests.
3. Renamed field: keep the old name in bronze until old files are processed; use `coalesce(new_name, old_name)` in the silver column expression.
4. Retyped field: bronze is text, so nothing breaks; adjust the silver expression and watch the quarantine count.
5. A new column on an existing bronze table does not reprocess old files. Values for files already read stay in `_rescued_data`; recover them by adding a new landing file only if the business needs the history.

## Verification

- `rescued_data_empty` returns to zero for new files.
- The new column is populated in silver for new rows.

## Escalation

The table owner and the source system owner.
