# Runbook: Backfill, replay and full refresh

> A starting point. Adapt the names, dashboards and contacts to your workspace; keep the structure.

## Purpose

Rebuild silver and gold, or load history, without losing raw data.

## Triggers

- A silver or gold definition changed and existing rows must be recomputed
- Historic files must be loaded

## First checks

1. Is it only gold that needs recomputing? A materialized view refresh handles that.
2. Do silver rows need recomputing from bronze? That is a full refresh of the silver tables, which is allowed.
3. Is bronze itself wrong? That needs raw files to be read again, which is protected.

## Steps

1. Gold only: refresh the pipeline; materialized views recompute.
2. Silver from bronze: run a full refresh selecting the affected silver tables (the validated table, the target and the quarantine table together) so they stay consistent. Bronze is untouched.
3. Loading history: copy the files into `/Volumes/<catalog>/landing/raw/<source>/` and run an update. Order does not matter for correctness, because silver orders events by the sequence column.
4. Reading raw files again: bronze sets `pipelines.reset.allowed = false`. Do not change it casually. Prefer loading corrected files under new names. If a reset is truly needed, make the property change in `spec/` through a reviewed pull request, run it, and restore the protection straight afterwards.

## Verification

- Row counts per table match expectations from a reference query on bronze.
- Gold totals reconcile with silver using the checks in the quality register.

## Escalation

The data platform team. Always before running a full refresh on `prod`.
