# Databricks notebook source
# MAGIC %md
# MAGIC # bricksentry quickstart on Databricks
# MAGIC
# MAGIC **Know what a change does to your data before it ships.** This notebook walks through bricksentry's safety loop on a small demo table that it creates in a schema you choose:
# MAGIC
# MAGIC 1. **Blast radius:** what depends on the table (from Unity Catalog lineage)
# MAGIC 2. **Rollback plan:** the exact `RESTORE` command, from Delta history
# MAGIC 3. **Safe preview:** run a DELETE on a disposable shallow clone, never on the real table
# MAGIC 4. **Blocked change:** a candidate table with a join bug that duplicates rows
# MAGIC 5. **Fixed change:** the corrected candidate, plus the pull-request comment a reviewer would see
# MAGIC 6. **Your own table:** blast radius and rollback plan for a real table (read-only)
# MAGIC 7. **Cleanup:** drops everything this notebook created
# MAGIC
# MAGIC **You need:** a Unity Catalog workspace, a SQL warehouse you can use, `CREATE TABLE` on the schema you pick, and (for the blast radius) `SELECT` on `system.access.table_lineage`.
# MAGIC Nothing here reads or changes your existing tables except the read-only checks in step 6.
# MAGIC
# MAGIC Docs: [bricksentry.org](https://bricksentry.org) · Source: [github.com/bricksentry/bricksentry](https://github.com/bricksentry/bricksentry)

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade "bricksentry[databricks]"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC Fill in the widgets at the top of the notebook, then run the rest of the cells in order.
# MAGIC - **catalog / schema:** where the demo tables go. The schema is created if it doesn't exist.
# MAGIC - **warehouse_id:** the ID of a SQL warehouse (SQL Warehouses → your warehouse → Connection details). bricksentry runs all its checks as SQL on it.

# COMMAND ----------

import re

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "bricksentry_demo")
dbutils.widgets.text("warehouse_id", "")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
WAREHOUSE_ID = dbutils.widgets.get("warehouse_id").strip()

for label, value in (("catalog", CATALOG), ("schema", SCHEMA)):
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Set the '{label}' widget to a plain name (letters, digits, underscores); got {value!r}")
if not re.fullmatch(r"[A-Za-z0-9]+", WAREHOUSE_ID):
    raise ValueError("Set the 'warehouse_id' widget to your SQL warehouse ID (SQL Warehouses → Connection details).")

NS = f"{CATALOG}.{SCHEMA}"
ORDERS = f"{NS}.bs_demo_orders"                # the "production" table
REVENUE = f"{NS}.bs_demo_daily_revenue"        # a downstream table, tagged certified
CANDIDATE = f"{NS}.bs_demo_orders_pr_42"       # what a pull request would build

import bricksentry
from bricksentry import Config, Sentry, agent_tools

sentry = Sentry(Config.from_dict({
    "backend": {"type": "databricks", "warehouse_id": WAREHOUSE_ID},
    "sandbox_schema": NS,                      # disposable preview clones go here
    "tables": {ORDERS: {"keys": ["order_id"], "segment_by": ["region"]}},
    "lineage": {"type": "databricks", "depth": 3, "lookback_days": 30},
}))
agent_tools.set_sentry(sentry)                 # the same tools an AI agent calls over MCP
run = sentry.backend.execute
print(f"bricksentry {bricksentry.__version__} · demo tables in {NS} · warehouse {WAREHOUSE_ID}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create the demo tables
# MAGIC A 20,000-row `bs_demo_orders` table, and a `bs_demo_daily_revenue` table built from it and tagged `certified`.
# MAGIC Building the second table from the first is what gives Unity Catalog a lineage edge to find in step 1.

# COMMAND ----------

run(f"CREATE SCHEMA IF NOT EXISTS {NS}")
run(f"""
CREATE OR REPLACE TABLE {ORDERS} AS
SELECT CAST(id AS BIGINT) AS order_id,
       CASE id % 3 WHEN 0 THEN 'APAC' WHEN 1 THEN 'EMEA' ELSE 'AMER' END AS region,
       CAST(round(20 + (id % 97) * 3.1, 2) AS DOUBLE) AS amount,
       CASE WHEN id % 10 = 0 THEN 'refunded' ELSE 'paid' END AS status
FROM range(1, 20001)
""")
run(f"""
CREATE OR REPLACE TABLE {REVENUE} AS
SELECT region, CAST(sum(amount) AS DOUBLE) AS revenue
FROM {ORDERS}
GROUP BY region
""")
try:
    run(f"ALTER TABLE {REVENUE} SET TAGS ('certified' = 'true')")
    print("Tagged", REVENUE, "as certified.")
except Exception as e:
    print("Could not tag the downstream table (needs APPLY TAG); the certified rule won't fire.", str(e)[:200])

print(ORDERS, "rows:", sentry.backend.scalar(f"SELECT COUNT(*) FROM {ORDERS}"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Blast radius: what depends on `bs_demo_orders`?
# MAGIC `whats_downstream` walks `system.access.table_lineage` and attaches owners and tags from Unity Catalog.
# MAGIC Lineage is recorded when queries run and can take a few minutes to appear. If this comes back empty, carry on and re-run this cell later.

# COMMAND ----------

ds = agent_tools.whats_downstream(ORDERS)
if "error" in ds:
    print("Lineage lookup failed (do you have SELECT on system.access.table_lineage?):", ds["error"])
elif not ds["downstream"]:
    print("No downstream assets found yet. Lineage can take a few minutes to appear; re-run this cell later.")
else:
    for n in ds["downstream"]:
        tags = ", ".join(n["tags"]) or "-"
        print(f"depth {n['depth']}  {n['kind']:<10} {n['name']:<55} owner: {n['owner'] or '-'}  tags: {tags}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Rollback plan
# MAGIC Captured *before* anything changes. This is the command that would undo a bad change to the table.

# COMMAND ----------

plan = agent_tools.rollback_plan(ORDERS)
print(plan["rollback_sql"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Safe preview: what would this DELETE do?
# MAGIC `preview_write` runs the statement on a disposable **shallow clone** in your schema, diffs it against the real table, applies the policy, and drops the clone.
# MAGIC It accepts one INSERT, MERGE, UPDATE or DELETE and refuses DROP, TRUNCATE, ALTER and GRANT.

# COMMAND ----------

def show(result):
    """Print a verdict, its summary and the rules that fired."""
    if "error" in result:
        print("ERROR:", result["error"])
        return
    print(f"VERDICT: {result['verdict'].upper()}  (risk {result['risk_tier']})")
    for t in result["tables"]:
        print(" ", t["summary"])
    for h in result["rule_hits"]:
        print(f"  [{h['verdict']}] {h['rule']}: {h['reason']}")


before = sentry.backend.scalar(f"SELECT COUNT(*) FROM {ORDERS}")
preview = agent_tools.preview_write(f"DELETE FROM {ORDERS} WHERE status <> 'paid'", ORDERS)
show(preview)
after = sentry.backend.scalar(f"SELECT COUNT(*) FROM {ORDERS}")
print(f"\nReal table rows before: {before:,} · after: {after:,} (the DELETE only ran on the clone)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. A pull request with a bug
# MAGIC Suppose a pull request applies an 8% APAC discount, but its join duplicates 40 orders.
# MAGIC In CI you'd build this into a per-PR schema; here we build it as `bs_demo_orders_pr_42` and assess it against the real table.

# COMMAND ----------

run(f"""
CREATE OR REPLACE TABLE {CANDIDATE} AS
SELECT o.order_id, o.region,
       CASE WHEN o.region = 'APAC' THEN round(o.amount * 0.92, 2) ELSE o.amount END AS amount,
       o.status
FROM {ORDERS} o
LEFT JOIN (SELECT * FROM VALUES (1), (1) AS dup(k)) dup ON o.order_id % 500 = 0
""")
buggy = agent_tools.assess_change([ORDERS], candidates={ORDERS: CANDIDATE})
show(buggy)
print("\nnext_step:", agent_tools.safe_to_ship(buggy["id"])["next_step"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. The fixed pull request
# MAGIC Same discount, no duplicated rows. The verdict now depends only on how much changed and who depends on it.
# MAGIC If lineage from step 1 is visible, the certified downstream table makes it `needs_human`; otherwise the large change is a `warn`.

# COMMAND ----------

run(f"""
CREATE OR REPLACE TABLE {CANDIDATE} AS
SELECT order_id, region,
       CASE WHEN region = 'APAC' THEN round(amount * 0.92, 2) ELSE amount END AS amount,
       status
FROM {ORDERS}
""")
fixed = agent_tools.assess_change([ORDERS], candidates={ORDERS: CANDIDATE})
show(fixed)
gate = agent_tools.safe_to_ship(fixed["id"])
print("\nsafe_to_ship:", gate["safe_to_ship"], "\nnext_step:", gate["next_step"])

# COMMAND ----------

# MAGIC %md
# MAGIC ### The pull-request comment a reviewer would see

# COMMAND ----------

from bricksentry.report import to_markdown

report = to_markdown(sentry.get_assessment(fixed["id"]))
try:
    from IPython.display import Markdown, display
    display(Markdown(report))
except Exception:
    print(report)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Try it on one of your own tables (read-only)
# MAGIC Enter a full table name in the `my_table` widget and run the cell. It only reads lineage, Unity Catalog metadata and Delta history.

# COMMAND ----------

dbutils.widgets.text("my_table", "")
MY_TABLE = dbutils.widgets.get("my_table").strip()

if not MY_TABLE:
    print("Set the 'my_table' widget to catalog.schema.table, then re-run this cell.")
else:
    ds = agent_tools.whats_downstream(MY_TABLE)
    if "error" in ds:
        print("ERROR:", ds["error"])
    else:
        print(f"{MY_TABLE}: owner {ds['owner'] or '-'}, tags {ds['tags'] or '-'}, {ds['count']} downstream asset(s)")
        for n in ds["downstream"]:
            print(f"  depth {n['depth']}  {n['kind']:<10} {n['name']}  owner: {n['owner'] or '-'}")
    print("\nRollback plan:", agent_tools.rollback_plan(MY_TABLE).get("rollback_sql"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Cleanup
# MAGIC Drops the three demo tables. The schema is left in place in case it holds anything else.

# COMMAND ----------

for t in (CANDIDATE, REVENUE, ORDERS):
    run(f"DROP TABLE IF EXISTS {t}")
    print("dropped", t)
agent_tools.set_sentry(None)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next steps
# MAGIC - **In CI:** `bricksentry init` writes `bricksentry.yml` and `policy.yml`; add `bricksentry assess --from-git origin/main --comment` to your pull-request workflow. See [bricksentry.org/docs/ci](https://bricksentry.org/docs/ci).
# MAGIC - **For AI agents:** `bricksentry mcp` exposes these same tools over MCP. See [bricksentry.org/docs/agents](https://bricksentry.org/docs/agents).
# MAGIC - **Feedback and bugs:** [github.com/bricksentry/bricksentry/issues](https://github.com/bricksentry/bricksentry/issues)
