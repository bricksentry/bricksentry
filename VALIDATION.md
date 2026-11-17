# Validation

What bricksentry has been tested against on real Databricks, release by release, with the numbers. It includes the
results that weren't perfect and every bug the live tests found. The unit tests (271, run on every push) use DuckDB
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

To test an unreleased commit, install it with `%pip install --force-reinstall --no-deps "bricksentry @ git+https://github.com/bricksentry/bricksentry@<commit>"` and then `dbutils.library.restartPython()`; without `--force-reinstall`, pip keeps an installed release with the same version number.

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

## Pre-release check of 0.1.26 (November 17, 2026): 47 of 47 passed

Run by Genie on two Databricks Free Edition workspaces (a fresh one and the one used for earlier rounds, serverless SQL warehouses), on the release commit installed from GitHub. Both said ship; the second ran the battery in 154 s and the 1M-row diff in 5.3 s, and checked the earlier rounds' numbers directly. It re-ran the cases from two worst-case rounds on 0.1.25 that had found five problems.

| Check | Result |
|---|---|
| Validation battery, now 12 scenarios | 12 of 12 passed in 162.5 s, including the new `null_keys_changed` (changed = 2) and `pii_noop` (identical: `pass`; 1-row edit: `needs_human`) |
| 50,000 rows sharing a NULL key, 2 real changes | changed = 2 (0.1.25: 36,563); a real extra copy counted as added, a missing copy as removed, a drop and a change on one key as 1 removed and 1 changed |
| A column total going from 0 to 5 | `numeric-total-moved` fired with "sum(amt) 0 -> 5"; a column staying at 0 stayed quiet |
| Two near-max `DECIMAL(38,0)` values | the diff completed with correct counts and a note that totals are approximate (0.1.25: the whole diff failed) |
| A bad write, then RENAME COLUMN, SET TBLPROPERTIES and OPTIMIZE | the rollback warning listed the rename and the properties change, not OPTIMIZE |
| Policy mistakes and a failing `preview_write` | one clear line each; no clone left behind, production byte-identical |
| The pii and certified gates (default policy) | identical candidate: `pass`; 1 row, a dropped column or a new duplicate key: flagged with both gates named; missing candidate: `needs_human`; a custom rule without `any_change` restores review of every change |
| Regression | MCP lists 6 tools (5 read-only); refused statements exit 2; a 1M-row composite-key diff took 5.9 s and found exactly the 4 changed rows; no leaked clones |

The checks found four small issues: `whats_downstream` and `rollback_plan` answered a mistyped table name with an empty result and with snapshot advice, and a misspelled key under `backend:` was ignored (all three fixed before release), and MCP errors start with the exception type, e.g. `RuntimeError: The statement failed…` (kept: it tells an agent what kind of error it got).

## Release test of 0.1.25 (November 16, 2026): passed, with the fix reproduced A/B

| Check | Result |
|---|---|
| The shipped test suite, from the source distribution | 239 passed, 1 skipped |
| **The sampling fix, before and after.** Same 2,000-row tables, one column retyped (`INT` → `BIGINT`), 1,895 changed rows placed deliberately outside a 5% sample | **0.1.24: 0 changed rows, no note** (all 1,895 changes missed). **0.1.25: 1,895 changed**, with "The 5% sample showed no row changes … re-run on all rows" |
| The validation battery, installed from PyPI | 10 of 10 passed in 113 s, including `landed_change` and `typo_table` on real Delta history |
| The certified scenarios | the strict verdict now comes from `certified-table-changed` alone, and the default one no longer fires `type-change`, as the changelog says |
| VALIDATION.md and the validation report form | both live on GitHub |

## The validation battery on Databricks (November 16, 2026): 10 of 10 passed

The battery published in this repo (`bricksentry.validation`, the
[validation notebook](examples/databricks/bricksentry_validation.py)), installed from GitHub `main` after 0.1.24 and run
with its default sizes in a fresh scratch schema. Total time: 111 s on a 2X-Small serverless warehouse.

| Scenario | Result | Time |
|---|---|---|
| `row_loss`: delete the department-less aggregate row | `needs_human` (row-loss, numeric-total-moved) | 8.3 s |
| `bad_merge`: a MERGE re-inserts a department | `block`, 1 added, 1 duplicate key | 8.3 s |
| `everything_wrong`: six problems in one change | `block`, 7 rules | 10.0 s |
| `hidden_in_sample`: 5 of 200,000 salaries ×5, outside a 5% sample | the checksum caught them and re-ran on all rows | 14.7 s |
| `certified_totals_default`: double a certified table's totals | `warn` (the documented default) | 8.8 s |
| `certified_totals_strict`: the same under `builtin:certified` | `block`* | 8.3 s |
| `null_keys`: 2,000 NULL keys each side | 3,000 unchanged, nothing added or removed | 9.0 s |
| `exact_totals`: 1,000,000 × 0.10 DECIMAL | exactly 100,000.0 on both sides | 10.0 s |
| `landed_change`: a bad write committed to Delta, then `check-last-change` | `block` and the `RESTORE` | 20.2 s |
| `typo_table`: `check-last-change` on a mistyped name | one line: "Table … not found" | 0.3 s |

\* On Databricks, doubling a `DECIMAL(18,2)` widens its type, so this run also fired the type-change rules. Since
0.1.25 the scenario keeps the column type, so the verdict comes from `certified-table-changed` alone.

Every table the battery created, including the clones made by `check-last-change`, was gone before the scratch schema
was dropped. Also confirmed on the released 0.1.24 from PyPI: a mistyped table in `check-last-change` prints one line.

## 0.1.23 and 0.1.24 (November 16, 2026)

### Release test of 0.1.23: 8 of 8 passed

| Test | Result |
|---|---|
| 2,000 NULL keys on each side of an unchanged table | unchanged 3,000, nothing added or removed; 4.6 s (before 0.1.23 the join produced 4,000,000 pairs) |
| A second copy of 3 existing keys | 3 added, 3 duplicate keys, verdict `block` |
| A changed copy of a duplicated key | 1 changed, 2 unchanged, nothing added or removed (but see the worst-case rounds below: with many copies and a few changes, the pairing over-reported until 0.1.26) |
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
| Round 7 (leaks, data shapes, races) on 0.1.26 | `preview_write` returned example values, so an agent without access could read production values by previewing an UPDATE of the rows it wanted | 0.1.27 |
| Round 7 on 0.1.26 | Diff queries weren't pinned to one table version: a candidate replaced mid-diff gave changed=49 (old state 5, new 50) with no warning | 0.1.27 |
| Round 7 on 0.1.26 | One MAP column failed the whole diff (Spark can't compare maps) | 0.1.27 |
| Round 7 on 0.1.26 | FLOAT/DOUBLE rounding (`0.1 + 0.2` vs `0.3`) showed every row changed, with no hint why | 0.1.27 (note + opt-in `float_tolerance`) |
| Worst-case rounds of 0.1.25 | Copies of a duplicated key were paired by rank, so one changed copy shifted the rest: 50,000 rows sharing a NULL key with 2 real changes reported 36,563 changed. Over-reports, never hides | 0.1.26 |
| Worst-case rounds of 0.1.25 | A column total going from 0 to 5 didn't fire `sum_change_pct_gt` (no percentage from zero) | 0.1.26 |
| Worst-case rounds of 0.1.25 | A `DECIMAL(38)` total overflowing failed the whole diff | 0.1.26 |
| Worst-case rounds of 0.1.25 | The rollback warning ignored newer non-data changes (RENAME COLUMN, SET TBLPROPERTIES) that a `RESTORE` also undoes | 0.1.26 |
| Pre-release check of 0.1.26 | `whats_downstream` returned an empty list for a mistyped table, and `rollback_plan` suggested a snapshot | 0.1.26 |
| Pre-release check of 0.1.26 | A misspelled `backend:` key (`path:` for `database:`) was silently ignored | 0.1.26 |
| Worst-case rounds of 0.1.25 | The default `pii-table` and `certified-downstream` rules asked a human on a pull request that changed nothing (noise that trains reviewers to ignore the gate) | 0.1.26 |
| Worst-case rounds of 0.1.25 | A verdict typo gave a bare `ValueError`; `policy: strict` was read as a file path; an empty policy silently passed everything; `preview_write` showed the engine's full error | 0.1.26 |
| This battery, Nov 16 | A sampled diff skipped the full-table check when the schema had also changed, so changed values in a retyped column outside the sample could go uncounted (the verdict still flagged the type change) | 0.1.25 |
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
