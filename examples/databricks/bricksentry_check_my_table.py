# Databricks notebook source
# MAGIC %md
# MAGIC # What did my last pipeline run do? Check one of your tables with bricksentry
# MAGIC
# MAGIC **10 minutes, on your real data, without changing it.** Pick a Delta table that a pipeline writes to.
# MAGIC bricksentry finds its most recent data change in the Delta history (a WRITE, MERGE, UPDATE or DELETE) and shows
# MAGIC what a reviewer would have seen *before* that change shipped:
# MAGIC
# MAGIC - rows added, removed and changed, duplicate or NULL keys, schema changes, and totals that moved
# MAGIC - what depends on the table (Unity Catalog lineage), with owners and tags such as `certified` or `pii`
# MAGIC - a verdict from a policy (`pass`, `warn`, `needs_human` or `block`), and the `RESTORE` command to undo it
# MAGIC
# MAGIC **How it stays safe:** your table is only read. To compare two versions, the notebook makes two *shallow clones*
# MAGIC (metadata only, no data copied) in a scratch schema you choose, and drops them at the end.
# MAGIC
# MAGIC **You need:** a SQL warehouse you can use, `SELECT` on the table, `CREATE TABLE` on the scratch schema, and for the
# MAGIC blast radius `SELECT` on `system.access.table_lineage`. The table's history must still be there (not removed by `VACUUM`).
# MAGIC
# MAGIC If it shows you something useful, or something wrong, please [tell us](https://github.com/bricksentry/bricksentry/issues/new?template=it_caught_this.yml).
# MAGIC Docs: [bricksentry.org](https://bricksentry.org)

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade "bricksentry[databricks]"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC Fill in the widgets at the top, then run all cells.
# MAGIC - **table:** `catalog.schema.table`, a Delta table your pipeline writes to.
# MAGIC - **keys:** the column(s) that identify a row, comma-separated (e.g. `order_id` or `customer_id, day`). Leave empty to use the table's primary key if it has one.
# MAGIC - **warehouse_id:** SQL Warehouses → your warehouse → Connection details.
# MAGIC - **scratch_schema:** `catalog.schema` for the two temporary clones. It is created if missing.
# MAGIC - **version:** leave empty to check the latest data change, or enter a table version number to check that one.
# MAGIC - **policy:** the rule set for the verdict (`default`, `strict`, `finance`, `pii`, `certified`, `agent-safe`).
# MAGIC - **sample_pct:** leave empty for a full comparison; on very large tables try `10` (a full-table checksum still catches any change the sample misses).

# COMMAND ----------

import re

dbutils.widgets.text("table", "")
dbutils.widgets.text("keys", "")
dbutils.widgets.text("warehouse_id", "")
dbutils.widgets.text("scratch_schema", "main.bricksentry_scratch")
dbutils.widgets.text("version", "")
dbutils.widgets.dropdown("policy", "default", ["default", "strict", "finance", "pii", "certified", "agent-safe"])
dbutils.widgets.text("sample_pct", "")

TABLE = dbutils.widgets.get("table").strip()
KEYS = [k.strip() for k in dbutils.widgets.get("keys").split(",") if k.strip()]
WAREHOUSE_ID = dbutils.widgets.get("warehouse_id").strip()
SCRATCH = dbutils.widgets.get("scratch_schema").strip()
VERSION = dbutils.widgets.get("version").strip()
POLICY = dbutils.widgets.get("policy")
SAMPLE = dbutils.widgets.get("sample_pct").strip()

NAME = r"[A-Za-z0-9_]+"
if not re.fullmatch(rf"{NAME}\.{NAME}\.{NAME}", TABLE):
    raise ValueError(f"Set the 'table' widget to catalog.schema.table (letters, digits, underscores); got {TABLE!r}")
if not re.fullmatch(rf"{NAME}\.{NAME}", SCRATCH):
    raise ValueError(f"Set 'scratch_schema' to catalog.schema; got {SCRATCH!r}")
if not re.fullmatch(r"[A-Za-z0-9]+", WAREHOUSE_ID):
    raise ValueError("Set the 'warehouse_id' widget to your SQL warehouse ID (SQL Warehouses → Connection details).")
if VERSION and not VERSION.isdigit():
    raise ValueError(f"'version' must be a number or empty; got {VERSION!r}")
SAMPLE_PCT = float(SAMPLE) if SAMPLE else None

import bricksentry
from bricksentry import Config, Sentry

sentry = Sentry(Config.from_dict({
    "backend": {"type": "databricks", "warehouse_id": WAREHOUSE_ID},
    "sandbox_schema": SCRATCH,
    "assessment_store": False,
    "lineage": {"type": "databricks", "depth": 3, "lookback_days": 90},
    "policy": f"builtin:{POLICY}",
}))
db = sentry.backend
print(f"bricksentry {bricksentry.__version__} · checking {TABLE} · policy {POLICY}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Recent history of the table
# MAGIC The last 15 versions from `DESCRIBE HISTORY`. bricksentry checks the most recent one that changed data,
# MAGIC comparing it with the version just before it.

# COMMAND ----------

DATA_OPS = ("WRITE", "MERGE", "UPDATE", "DELETE", "TRUNCATE", "STREAMING UPDATE",
            "CREATE OR REPLACE TABLE AS SELECT", "REPLACE TABLE AS SELECT", "COPY INTO")

history = db.query(f"DESCRIBE HISTORY {db.qualified(TABLE)} LIMIT 15")
for h in history:
    metrics = str(h.get("operationMetrics") or "")
    print(f"v{h['version']:<6} {str(h.get('timestamp'))[:19]}  {h.get('operation', ''):<34} {metrics[:90]}")

if VERSION:
    CHECK = int(VERSION)
else:
    changes = [h for h in history if str(h.get("operation", "")).upper() in DATA_OPS and int(h["version"]) > 0]
    if not changes:
        raise ValueError("No data change found in the last 15 versions. Enter a version number in the 'version' widget.")
    CHECK = int(changes[0]["version"])
if CHECK < 1:
    raise ValueError("Version 0 is the table's creation; there is nothing before it to compare with.")
op = next((h.get("operation") for h in history if int(h["version"]) == CHECK), "?")
print(f"\nChecking version {CHECK} ({op}) against version {CHECK - 1}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Keys
# MAGIC Rows are matched by key. If you left `keys` empty, the table's primary key constraint is used.

# COMMAND ----------

if not KEYS:
    cat, sch, tbl = TABLE.split(".")
    try:
        rows = db.query(f"""
            SELECT k.column_name
            FROM {db.quote(cat)}.information_schema.table_constraints c
            JOIN {db.quote(cat)}.information_schema.key_column_usage k
              ON k.constraint_catalog = c.constraint_catalog
             AND k.constraint_schema = c.constraint_schema
             AND k.constraint_name = c.constraint_name
            WHERE c.constraint_type = 'PRIMARY KEY'
              AND c.table_schema = '{sch}' AND c.table_name = '{tbl}'
            ORDER BY k.ordinal_position""")
        KEYS = [r["column_name"] for r in rows]
    except Exception as e:
        print("Could not read the primary key:", str(e)[:200])
if not KEYS:
    raise ValueError("No primary key found. Enter the key column(s) in the 'keys' widget and run again.")
print("Keys:", ", ".join(KEYS))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Compare the two versions and get the verdict

# COMMAND ----------

short = re.sub(r"[^A-Za-z0-9_]", "_", TABLE.split(".")[-1])[:40]
BASE = f"{SCRATCH}.bs_check_{short}_v{CHECK - 1}"
CAND = f"{SCRATCH}.bs_check_{short}_v{CHECK}"

db.ensure_schema(SCRATCH)
assessment = None
try:
    for target, v in ((BASE, CHECK - 1), (CAND, CHECK)):
        try:
            db.execute(f"CREATE OR REPLACE TABLE {db.qualified(target)} "
                       f"SHALLOW CLONE {db.qualified(TABLE)} VERSION AS OF {v}")
        except Exception as e:
            raise RuntimeError(
                f"Could not read version {v} of {TABLE}. Its files may have been removed by VACUUM, "
                f"or you may lack CREATE TABLE on {SCRATCH}. Details: {str(e)[:300]}") from None
    diff = sentry.diff(BASE, CAND, keys=KEYS, table=TABLE, sample_pct=SAMPLE_PCT)
    assessment = sentry.assess([TABLE], diffs={TABLE: diff})
finally:
    for t in (BASE, CAND):
        try:
            db.drop(t)
        except Exception as e:
            print("Could not drop", t, str(e)[:120])

t = assessment.tables[0]
UNDO = f"RESTORE TABLE {db.qualified(TABLE)} TO VERSION AS OF {CHECK - 1}"
t.rollback = UNDO  # the change already happened: undoing it means going back to the version before it
print(f"VERDICT: {assessment.verdict.value.upper()}  (risk {assessment.verdict.risk_tier})\n")
print(t.summary, "\n")
for h in assessment.hits:
    print(f"  [{h.verdict.value}] {h.rule}: {h.reason}")
if assessment.notify:
    print("\nWould ask for review from:", ", ".join(assessment.notify))
print(f"\nTo undo version {CHECK}: {UNDO}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### The pull-request comment a reviewer would have seen

# COMMAND ----------

from bricksentry.report import to_markdown

report = to_markdown(assessment)
try:
    from IPython.display import Markdown, display
    display(Markdown(report))
except Exception:
    print(report)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Tell us what it found (optional, 1 minute)
# MAGIC Did it catch something, miss something, or get it wrong? The link below opens a short GitHub form with the
# MAGIC verdict and rules filled in. **No table or column names are included**; add only what you're happy to share.

# COMMAND ----------

from urllib.parse import urlencode

rules = "; ".join(sorted({f"{h.rule} ({h.verdict.value})" for h in assessment.hits})) or "none"
d = t.diff
facts = (f"rows {d.base_rows:,} -> {d.candidate_rows:,}; added {d.added_rows:,}, removed {d.removed_rows:,}, "
         f"changed {d.changed_rows:,}; downstream assets {len(t.downstream)}") if d else "diff unavailable"
params = {
    "template": "it_caught_this.yml",
    "title": f"[check] {assessment.verdict.value} on a {op} change",
    "verdict": assessment.verdict.value,
    "rules": rules,
    "facts": f"Operation: {op}\n{facts}",
    "version": bricksentry.__version__,
}
print("Share what it found:\nhttps://github.com/bricksentry/bricksentry/issues/new?" + urlencode(params))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next steps
# MAGIC - **Check every change before it ships:** run the same check in CI on pull requests. See [bricksentry.org/docs/ci](https://bricksentry.org/docs/ci).
# MAGIC - **Let AI agents check their own changes:** `uvx bricksentry-mcp`. See [bricksentry.org/docs/agents](https://bricksentry.org/docs/agents).
# MAGIC - **Get alerts:** set a Slack or Teams webhook and run `bricksentry test-alert`. See [bricksentry.org/docs/configuration](https://bricksentry.org/docs/configuration).
