# Runbook: First deployment of an environment

> A starting point. Adapt the names, dashboards and contacts to your workspace; keep the structure.

## Purpose

Bring a new environment (`dev`, `staging` or `prod`) from nothing to a first successful pipeline update.

## Triggers

- A new target is being added to `spec/platform.toml`
- A new workspace or metastore is being used

## First checks

1. Is Unity Catalog enabled for the workspace, and is the metastore attached?
2. Do the groups in `spec/platform.toml` exist in the account, synced from the identity provider?
3. Does a service principal exist for staging and prod, and does it have `CAN USE` on the SQL warehouse?

## Steps

1. Replace the placeholders: the `run_as_service_principal` variable in `bundle/databricks.yml` and the `warehouse_id` you will pass with `--var`.
2. Decide who creates the catalog. The governance task needs rights to create the catalog and schemas and to grant. Either give the run-as principal those rights on the metastore, or have a metastore administrator run `bundle/src/governance/<env>.sql` once by hand.
3. Run `medallion lint` and `medallion generate --check` from the repository root; both must pass.
4. From `bundle/`: `databricks bundle validate -t <env> --var warehouse_id=<id>`, then `databricks bundle deploy -t <env> --var warehouse_id=<id>`.
5. Put sample or real files in `/Volumes/<catalog>/landing/raw/<source>/`, one folder per source table.
6. Run the job once by hand: `databricks bundle run -t <env> retail_lakehouse_daily --var warehouse_id=<id>`.
7. Leave the schedule paused until the checks below pass; only `prod` unpauses it by default.

## Verification

- Both tasks succeed; the pipeline updates all nine tables.
- `SHOW TABLES IN <catalog>.silver` lists each table with its `_validated` and `_quarantine` companions.
- As a member of the analysts group, `SELECT * FROM <catalog>.silver.orders` is denied and `SELECT * FROM <catalog>.gold.customer_value` returns masked emails.
- As a member of the privileged group, the same query returns real emails.

## Escalation

The data platform team. If a grant or catalog operation is denied, a metastore administrator.
