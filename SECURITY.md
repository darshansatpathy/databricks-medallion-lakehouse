# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/darshansatpathy/databricks-medallion-lakehouse/security/advisories/new)
for this repository. You should get an acknowledgement within three working days.

## Scope

In scope: the `medallion` tool, the SQL, Unity Catalog statements and bundle it generates, and the sample specification, in particular anything that could expose masked data or widen access. Out of scope: your workspace configuration and the specifications you write.

## Operating it safely

- The sample data in `spec/data/` is invented. Never commit real customer data to it.
- The bundle contains placeholders (service principal ID, warehouse ID). Supply real values through variables at deploy time, not in files.
- Use workload identity federation (OIDC) for deployment; do not store a Databricks token as a repository secret.
- The runner group sees real values of masked columns. Keep it to the pipeline's service principal.
- Column masks do not apply to Delta Sharing recipients; do not share these tables directly.
