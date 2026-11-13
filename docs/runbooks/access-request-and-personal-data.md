# Runbook: Access requests and personal data

> A starting point. Adapt the names, dashboards and contacts to your workspace; keep the structure.

## Purpose

Grant, change or review access without breaking the least-privilege model.

## Triggers

- A person or team asks for access to data
- A quarterly access review
- A suspected exposure of personal data

## First checks

1. What layer do they need? Consumers get gold only.
2. Do they need real values of personal columns? That needs the privileged group.
3. Is the access for a person or for a workload? Workloads use a service principal in the runner group, never a person's account.

## Steps

1. Consumers: add the person to the consumer group in the identity provider. No change to this repository.
2. Real values of personal columns: the table owner approves; add the person to the privileged group in the identity provider, with an expiry or review date. Record the approval.
3. A new kind of access (a new group, a new schema grant): change `spec/platform.toml`, run `medallion lint` (rule `MED012` rejects individual grants and over-broad privileges), and deploy. The governance task applies the grants.
4. Quarterly review: read [access](../generated/access.md) and [classification](../generated/classification.md), and check group membership in the identity provider against them.
5. Suspected exposure: remove the access first, then check Unity Catalog audit logs for what the principal read. Do not delete data until the review is done.

## Verification

- The person can read what they should and not more; test with a query as that person or a test user.
- The generated access matrix and the live grants agree.

## Escalation

The table owner for approvals; the security team for a suspected exposure.
