# ADR 0007: One bundle, three targets, governance before the pipeline

## Status

Accepted

## Context

Environments that are configured separately drift. Governance objects (schemas, masks, grants) have
to exist before a pipeline that refers to them can run.

## Decision

The asset bundle has one pipeline and one job, and three targets (`dev`, `staging`, `prod`) that differ
by catalog and run-as identity. `dev` is in development mode. `staging` and `prod` are in production
mode and run as a service principal given by a variable. The job's first task runs the governance SQL
for the target on a SQL warehouse; its second refreshes the pipeline. The schedule is paused unless
the target unpauses it, and only `prod` does.

## Consequences

- The service principal needs rights to create the catalog and schemas on first deployment, or an
  administrator runs the governance SQL once; see the [first deployment runbook](../runbooks/first-deployment.md).
- Governance SQL is idempotent, so running it on every refresh also corrects drift in grants.
- The placeholders (service principal, warehouse) must be set before the first deploy.
