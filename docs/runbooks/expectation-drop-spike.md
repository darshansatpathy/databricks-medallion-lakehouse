# Runbook: Quarantine is growing or an expectation is dropping many rows

> A starting point. Adapt the names, dashboards and contacts to your workspace; keep the structure.

## Purpose

Decide whether a rise in dropped rows is a source problem, a specification problem or normal, and replay corrected data.

## Triggers

- The quarantine table's daily count rises well above its normal level
- A silver table is missing rows its owners expect

## First checks

1. In the pipeline event log, which expectation's failed-row count rose, and when?
2. In `<name>_quarantine`, group by `failed_rules`: is it one rule or several?
3. Did the source or the specification change at that time (check the commit history of `spec/`)?

## Steps

1. Look at a sample of quarantined rows and the matching bronze rows (same `_source_file`).
2. If one rule dominates, decide: the **source** is wrong (a null key, an unparsable amount), or the **rule** is wrong (a legitimate new format).
3. Source wrong: ask the source owner to send corrected events with a later sequence value. Nothing is edited in place; corrections are new events.
4. Rule wrong: change the table's expression or expectation in `spec/`, run `medallion lint` and `medallion run` on a sample that contains the new format, add that sample to `spec/data/` and a test, and deploy through the normal path.
5. Rows already quarantined are not reprocessed automatically. After a fix, request the source to re-send, or copy the affected landing files to a new file name in the landing folder so they are read as new input.

## Verification

- The dropped-row count returns to its normal level on the next update.
- The corrected rows appear in the silver table and, after the next refresh, in gold.

## Escalation

The table owner. If the cause is the source, the source system owner.
