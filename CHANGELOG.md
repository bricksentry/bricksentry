# Changelog

## 0.1.4 (2026-11-10)

From a second end-to-end run on Databricks serverless against a 2-million-row table:

- Fix: a sampled diff (`sample_pct`) could report "no change" when the change sat outside the sample, for example one deleted row in 2 million at a 10% sample. A full-table checksum now guards every sampled diff: if the sample looks identical but the tables differ, the diff re-runs on all rows and says so in the summary (`sample_note`). Identical tables stay sampled and cheap. Turn it off with `sample_guard: false`.
- Fix: `explain_diff` and `safe_to_ship` returned "unknown assessment id" when called from a different `Sentry` instance or process. Assessments are now saved as JSON (default `~/.bricksentry/assessments`, or `assessment_store:` in bricksentry.yml, or env `BRICKSENTRY_STORE`) and loaded by id. Set `assessment_store: false` to keep them in memory only.

## 0.1.3 (2026-11-10)

- Blast radius no longer lists bricksentry's own `preview_write` clones, or tables that Unity Catalog lineage remembers but that no longer exist (when `information_schema` can confirm it). Before, every preview added a stale downstream entry. New lineage settings: `exclude` (name patterns, default `["*__preview_*"]`) and `exclude_dropped` (default true).
- Databricks quickstart notebook (`examples/databricks/bricksentry_quickstart.py`, plus an `.ipynb` copy) that runs the full loop on a demo table in your workspace.

## 0.1.2 (2026-11-10)

- Fix: rows whose key is NULL are now matched null-safely. Before, each such row was counted as both added and removed (found on a real Databricks table with a NULL `state`).
- New `null_keys` policy condition and default `null-keys` rule (warn) when a change introduces rows with a NULL key; summaries mention NULL keys.

## 0.1.1 (2026-11-10)

- New `bricksentry demo` command: the full agent safety loop on a local 20,000-row example in about 30 seconds (`pip install "bricksentry[duckdb]"`). `--report FILE` also writes the pull-request comment.
- Fix: the "Review requested from" line in PR comments no longer runs into the line above it.
- Project links (website, docs, source, issues, changelog) in package metadata.
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
