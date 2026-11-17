# Databricks notebook source
# MAGIC %md
# MAGIC # Validate bricksentry on your own workspace
# MAGIC
# MAGIC **14 risky changes, about 3 minutes, nothing outside a scratch schema touched.** This notebook runs the same
# MAGIC validation battery we publish in [VALIDATION.md](https://github.com/bricksentry/bricksentry/blob/main/VALIDATION.md),
# MAGIC so you can check the results yourself instead of taking our word for them.
# MAGIC
# MAGIC It generates an HR-style employees table (200,000 rows) and a department mart in the scratch schema, then tries the
# MAGIC changes that go wrong in real pipelines: silent row loss, a MERGE that duplicates a department, one change with
# MAGIC everything wrong, five bad rows hidden outside a 5% sample, a doubled total on a certified table, 2,000 NULL keys,
# MAGIC exact totals over a million decimal rows, a bad change that already landed in Delta, and a mistyped table name.
# MAGIC Every table it creates is dropped at the end.
# MAGIC
# MAGIC **You need:** a SQL warehouse you can use and `CREATE TABLE` on the scratch schema (it is created if missing).
# MAGIC
# MAGIC Please share what you see, whether everything passes or not:
# MAGIC [open a validation report](https://github.com/bricksentry/bricksentry/issues/new?template=validation_report.yml).
# MAGIC Results from other clouds, classic compute and bigger tables are the most useful.

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade "bricksentry[databricks]"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC - **warehouse_id:** SQL Warehouses → your warehouse → Connection details.
# MAGIC - **scratch_schema:** `catalog.schema` where the test tables are created and dropped. Use a schema with nothing else in it.
# MAGIC - **rows:** size of the generated employees table (default 200,000).

# COMMAND ----------

import re

dbutils.widgets.text("warehouse_id", "")
dbutils.widgets.text("scratch_schema", "main.bricksentry_validation")
dbutils.widgets.text("rows", "200000")

WAREHOUSE_ID = dbutils.widgets.get("warehouse_id").strip()
SCRATCH = dbutils.widgets.get("scratch_schema").strip()
ROWS = int(dbutils.widgets.get("rows").strip() or "200000")

NAME = r"[A-Za-z0-9_]+"
if not re.fullmatch(r"[A-Za-z0-9]+", WAREHOUSE_ID):
    raise ValueError("Set the 'warehouse_id' widget to your SQL warehouse ID (SQL Warehouses → Connection details).")
if not re.fullmatch(rf"{NAME}\.{NAME}", SCRATCH):
    raise ValueError(f"Set 'scratch_schema' to catalog.schema; got {SCRATCH!r}")

import bricksentry
from bricksentry.backends.base import make_backend

backend = make_backend({"type": "databricks", "warehouse_id": WAREHOUSE_ID})
print(f"bricksentry {bricksentry.__version__}, scratch schema {SCRATCH}, {ROWS:,} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the battery

# COMMAND ----------

from bricksentry.validation import print_results, run_validation

results = run_validation(backend, SCRATCH, rows=ROWS)
print_results(results)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Results as a table
# MAGIC Copy this into a [validation report](https://github.com/bricksentry/bricksentry/issues/new?template=validation_report.yml),
# MAGIC with your cloud and compute type. Leave out anything private (workspace URLs, warehouse IDs, emails).

# COMMAND ----------

import pandas as pd

display(pd.DataFrame([r.to_dict() for r in results])[["scenario", "result", "expected", "actual", "seconds"]])

# COMMAND ----------

# MAGIC %md
# MAGIC ## What the results mean
# MAGIC - **PASS** on every row means bricksentry caught each change the way VALIDATION.md describes.
# MAGIC - `certified_totals_default` passes when the verdict is only a **warning**: that's the documented default for a
# MAGIC   certified table itself. `certified_totals_strict` shows the same change under `policy: builtin:certified`.
# MAGIC - Any **FAIL** is a bug we want to hear about. Please include the `actual` column in your report.
# MAGIC
# MAGIC The scratch tables were dropped when the battery finished. The scratch schema itself is left in place.
