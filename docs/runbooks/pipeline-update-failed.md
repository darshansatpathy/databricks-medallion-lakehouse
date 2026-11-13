# Runbook: A pipeline update failed

> A starting point. Adapt the names, dashboards and contacts to your workspace; keep the structure.

## Purpose

Find why the scheduled update failed and get the lakehouse current again.

## Triggers

- The job named `[<target>] retail-lakehouse daily` failed or timed out
- An alert on pipeline failure fired

## First checks

1. Which task failed: `apply_governance` or `refresh_pipeline`?
2. In the pipeline's event log, which flow and which error? Look for expectation failures first.
3. Did the failure follow a change to the specification, or only new data?

## Steps

- **An expectation with `fail` was violated.** The event log names the expectation and the flow. This means the source broke its contract (for example, a new change operation code). Do not weaken the expectation to get green. Find the offending rows in bronze, agree the fix with the source owner, and either correct the source or extend the specification deliberately.
- **`apply_governance` failed.** Read the SQL error. A missing group or a denied privilege is an environment problem; fix it there. The SQL is idempotent, so re-run the task.
- **A table or flow error after a spec change.** Revert the spec change, run `medallion generate`, redeploy, and reproduce the problem on `dev` before trying again.
- **Out of capacity or timeout.** Re-run once; if it repeats, raise the timeout in the job definition through the generator, not by editing the deployed job.

## Verification

- The next update succeeds and the freshness of the gold tables is back within the agreed window.
- Quarantine row counts for the update are in the usual range (see the expectation drop runbook).

## Escalation

The owning team of the failing table (`governance.owner` property). If a `fail` expectation tripped on production data, also tell the source system owner.
