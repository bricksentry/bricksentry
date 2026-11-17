# Validation

What bricksentry has been tested against on real Databricks, release by release, with the numbers. It includes the
results that weren't perfect and every bug the live tests found. The unit tests (240, run on every push) use DuckDB
and a mocked Databricks SDK; this page is about the live runs.

**Run it yourself.** The [validation notebook](examples/databricks/bricksentry_validation.py) runs the same risky
changes on your workspace in about two minutes, inside a scratch schema, and drops everything it creates. The same
battery runs from Python:

```python
from bricksentry.backends.base import make_backend
from bricksentry.validation import run_validation, print_results

backend = make_backend({"type": "databricks", "warehouse_id": "<your warehouse id>"})
print_results(run_validation(backend, scratch="main.bricksentry_validation"))
```

Please share what you see, especially from other clouds, classic compute and bigger tables:
[open a validation report](https://github.com/bricksentry/bricksentry/issues/new?template=validation_report.yml).

## How these runs were done

- **Where:** a Databricks Free Edition workspace (serverless, one 2X-Small SQL warehouse), with an HR pipeline: a
  200,000-row employees table feeding a certified compensation mart read by three notebooks. One run used a
  2-million-row table.
- **Who:** the maintainer, with Databricks' Genie Code assistant running the steps in the workspace. Each run installs
  the released package from PyPI, not a local copy.
- **Safety rules for every run:** production tables are only read; every write runs on a shallow clone or a table in a
  scratch schema; the scratch schema is dropped at the end, and the run confirms that the production tables kept their
  row counts and Delta versions and that the pipeline's scheduled guard jobs didn't fire.
- Workspace specifics (warehouse IDs, job IDs, users, internal table names) are left out on purpose.

## 0.1.23 and 0.1.24 (November 16, 2026)

### Release test of 0.1.23: 8 of 8 passed

| Test | Result |
|---|---|
| 2,000 NULL keys on each side of an unchanged table | unchanged 3,000, nothing added or removed, copies paired one to one; 4.6 s (before 0.1.23 the join produced 4,000,000 pairs) |
| A second copy of 3 existing keys | 3 added, 3 duplicate keys, verdict `block` |
| A changed copy of a duplicated key | 1 changed, 2 unchanged, nothing added or removed |
| 1,000,000 rows of 0.10 as `DECIMAL(18,2)` | totals exactly 100,000.0 on both sides; 5.4 s (summing as `DOUBLE` gives 100,000.00000022353) |
| Audit trail | time, version, policy fingerprint and `triggered_by` in the JSON and the report; same policy gives the same fingerprint, a changed rule a different one |
| `check-last-change` after a duplicating MERGE | `block`, "3 rows added", the `RESTORE`, and the newer-changes warning after a later write |
| Outbound URL checks (0.1.22) | `http://`, localhost, 127.0.0.1, 10.x, 192.168.x and the cloud metadata address all refused with a one-line reason; the escape hatch works |
| Standing regression | MCP server lists 6 tools (5 read-only); refused statements and missing keys exit 2; lineage; rollback plan |

### An AI agent tries risky changes on the live pipeline: nothing got through unnoticed

| Attack | Verdict | Rules |
|---|---|---|
| Ask what reads the mart before editing | 4 downstream entities (the mart and 3 notebooks) | |
| Delete the aggregate row for employees with no department | `needs_human` | row-loss, numeric-total-moved |
| A case-sensitive MERGE re-inserts a department | `block` | duplicate-keys |
| One change that drops a department, duplicates keys, adds a NULL key, drops a column, changes a type and zeroes the totals | `block`, 6 rules at once | duplicate-keys; dropped-column, row-loss; type-change, many-rows-changed, numeric-total-moved |
| Multiply 5 of 200,000 salaries by 5, behind a 5% sample | `needs_human`; the sample missed all five, the full-table checksum disagreed and the re-run found exactly 5 | certified-downstream, pii-table |
| A bad MERGE committed to a Delta table, then `check-last-change` | `block`, with `RESTORE … TO VERSION AS OF 0` | duplicate-keys |
| **Double the Sales totals in the certified mart** | **`warn` only** | many-rows-changed, numeric-total-moved |

The last row is a deliberate default, not a bug: the default policy asks a human when a change reaches something
certified *downstream*, and treats a change *to* a certified table like any other, because many certified marts are
rebuilt daily. `policy: builtin:certified` makes it `needs_human`. The README now says so.

### The paths new users hit first: 9 of 9 passed

| Scenario | Result |
|---|---|
| Job task (`python -m bricksentry.job`) after a bad change | exit 1, `block`, the `RESTORE` in the log, downstream tasks stopped |
| Job task after a good change | exit 0, `warn` |
| Job task with `--fail-on never` and a `block` | exit 0 |
| Job task run by hand with no tables, and with `--default-tables` | "no tables to check", and the defaults used |
| Pull request with candidates found by `candidate_pattern` | a good change ships with a warning; a missing candidate gives `needs_human` and "did the CI build run?" |
| `--since yesterday-ish` | one line with an ISO 8601 example |
| `check-last-change` on a version removed by `VACUUM` | one line naming VACUUM or permissions; no clone left behind |
| Version 0 | "Version 0 is the table's creation; there is nothing before it" |
| The DuckDB demo | all five steps correct in 0.3 s |

### The job task inside a real Databricks job

A temporary job using the same wrapper as a production guard, pinned to 0.1.23, ran once against a scratch table
with a duplicating MERGE: the run **failed as designed** (task exit 1) and the task log showed the verdict, the rules,
the `RESTORE` and "failing this task so downstream tasks don't run".

## Earlier releases

| Release | Date | What was checked live |
|---|---|---|
| 0.1.21 | Nov 13 | All five refused statement types print one line and exit 2; the guard against a write still naming the production table; exit codes 2 (refused), 1 (block), 0 (pass/warn). Source-distribution tests: 206 passed. |
| 0.1.20 | Nov 13 | The "newer data changes exist" warning in the CLI, the pull-request report, the JSON and Slack. |
| up to 0.1.18 | Nov 11–12 | Pull-request gating on the HR pipeline with comment updates; the bundle demo job. |
| 0.1.12, 0.1.13 | Nov 11 | Column-level lineage: a new column edge appeared about a minute after the `CREATE TABLE`; a `certified` column tag read from Unity Catalog. |
| 0.1.4 | Nov 10 | A 2-million-row table: lineage, rollback plans matching `DESCRIBE HISTORY`, previews on shallow clones (production unchanged), every verdict level, and the sampling guard catching a single changed row. |

## Bugs found by live tests, and where they were fixed

| Found in | Problem | Fixed in |
|---|---|---|
| This battery, Nov 16 | A sampled diff skipped the full-table check when the schema had also changed, so changed values in a retyped column outside the sample could go uncounted (the verdict still flagged the type change) | 0.1.25 (unreleased) |
| User-path test of 0.1.23 | A mistyped table name printed the engine's full error, including an unrelated hint about dropping views | 0.1.24 |
| Real job run of 0.1.23 | The job task log ended with IPython's "To exit: use 'exit'…" line | 0.1.24 |
| Release test of 0.1.23 | `test-alert` help said exit 1 when no webhook is configured; it exits 2 | 0.1.24 |
| Live test of 0.1.21 | `preview` with no keys configured exited 1 instead of 2 | 0.1.22 |
| Live test of 0.1.20 | `preview` printed a Python traceback when it refused a statement | 0.1.21 |
| Live job runs | Every job task run was marked failed, even a pass: Databricks treats any `SystemExit` from a Python task as an error | 0.1.15 |
| Live test of 0.1.12 | Column-level results listed columns of tables dropped long ago (lineage history keeps them) | 0.1.13 |
| Live test of 0.1.9 | The CLI showed the SDK's raw error, or a cryptic `parent_header` error in a notebook, when it couldn't sign in | 0.1.11 |
| 2-million-row run | A sampled diff could report "no change" when the change sat outside the sample | 0.1.4 |
| First Databricks run | Rows with a NULL key were counted as both added and removed | 0.1.2 |

The full list of changes is in [CHANGELOG.md](CHANGELOG.md).

## What hasn't been validated live yet

- Other clouds (AWS, Azure, GCP workspaces beyond Free Edition), classic compute, and workspaces with restricted
  system-table access.
- Tables much larger than 2 million rows. See "What a check costs" in the README; for big tables use `sample_pct`.
- A full `databricks bundle deploy` of the job template in a fresh workspace, and the container image outside CI.

If you can run the notebook in one of these setups, a [validation report](https://github.com/bricksentry/bricksentry/issues/new?template=validation_report.yml)
helps more than anything else.
