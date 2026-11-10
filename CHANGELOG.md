# Changelog

## Unreleased

- Project links (source, issues, changelog) in package metadata.
- CI on Python 3.10 to 3.13, and release automation through PyPI Trusted Publishing.
- CONTRIBUTING, SECURITY and issue templates.

## 0.1.0 (2026-11-10)

- Key-based data diff with SQL pushdown: schema changes, added/removed/changed rows, per-column changes, nulls, numeric totals, duplicate keys, samples, segment attribution, deterministic hash sampling.
- Downstream impact from Unity Catalog lineage system tables (tables, jobs, notebooks, pipelines, dashboards, queries) with owners and tags, plus static lineage for local work.
- Policy-as-code verdicts: pass / warn / needs_human / block.
- `preview_write`: run one write against a disposable shallow clone, with SQL guards.
- Rollback plans from Delta history.
- CLI (`diff`, `impact`, `assess`, `preview`, `rollback`, `mcp`, `init`), markdown PR report, GitHub PR comment, composite GitHub Action.
- MCP server with six agent tools; the same tools as plain Python functions.
- Backends: Databricks SQL warehouse (Statement Execution API) and DuckDB.
