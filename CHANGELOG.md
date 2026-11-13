# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-13

### Added
- Specification format for platform and tables; loader that reports every problem together.
- Sixteen policy rules (MED000 to MED015) covering references, layering, bronze and silver and gold shape, personal data and masks, metadata, expectations, type 2 consumption, expression safety, grants, naming, classification and uniqueness; expiring waivers.
- Generators for Lakeflow pipeline SQL (bronze, silver with quarantine and AUTO CDC, gold materialized views), Unity Catalog governance SQL per environment, and a Databricks Asset Bundle.
- Reference executor on SQLite and Python with a portable expression subset, hand-checked sample data and assumption list.
- Generated data dictionary, lineage, classification, access, quality and sample-run documents with a freshness check.
- Seven ADRs and six runbooks.

[0.1.0]: https://github.com/darshansatpathy/databricks-medallion-lakehouse/releases/tag/v0.1.0
