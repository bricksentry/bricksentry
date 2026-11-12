# Databricks notebook source
# MAGIC %md
# MAGIC # bricksentry, every feature on an HR pipeline
# MAGIC
# MAGIC A guided tour of **everything bricksentry does**, on a small but realistic HR pipeline:
# MAGIC
# MAGIC `bricksentry_hr_employees` (200,000 employees, tagged **pii**) → `bricksentry_hr_compensation` (pay by department, tagged **certified**)
# MAGIC
# MAGIC | Step | What you'll see |
# MAGIC |---|---|
# MAGIC | 1. Blast radius | What depends on a table, from Unity Catalog lineage, down to the column |
# MAGIC | 2. Check a change | Two pull-request versions of the pipeline: one risky, one broken |
# MAGIC | 3. Every rule | NULL keys, dropped columns, row loss, type changes |
# MAGIC | 4. Preview a write | An agent's UPDATE runs on a throwaway clone; dangerous SQL is refused |
# MAGIC | 5. Explain, gate, undo | Plain-English summary, the ship/no-ship gate, the exact `RESTORE` |
# MAGIC | 6. The PR report | The comment a reviewer would see |
# MAGIC | 7. Column rules and templates | Block any change to a tagged `salary` column; the built-in policies |
# MAGIC | 8. Sampling you can trust | A 5% sample can't hide a duplicate key |
# MAGIC | 9. Lineage from config | Blast radius for dashboards, jobs and Genie spaces without waiting for lineage |
# MAGIC | 10. Alerts, CLI, MCP, local demo | Slack/Teams alerts, the command line, the agent server |
# MAGIC
# MAGIC **What it creates:** two demo tables in the schema you choose (prefixed `bricksentry_hr_`), a `<schema>_ci` schema for
# MAGIC the pull-request versions, and a `bricksentry_sandbox` schema for preview clones. The last cell removes all of it.
# MAGIC It never touches any other table.
# MAGIC
# MAGIC **You need:** a SQL warehouse, `CREATE SCHEMA` on the catalog (or the three schemas created for you), and for the
# MAGIC blast radius `SELECT` on `system.access.table_lineage` and `system.access.column_lineage`.
# MAGIC Docs: [bricksentry.org](https://bricksentry.org). For a quick check of one of *your* tables, use the
# MAGIC [check-my-table notebook](https://github.com/bricksentry/bricksentry/blob/main/examples/databricks/bricksentry_check_my_table.py).

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade "bricksentry[databricks,mcp,duckdb]"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC Fill in the widgets, then run all. **catalog** and **schema** say where the demo tables go; **warehouse_id** is under
# MAGIC SQL Warehouses → your warehouse → Connection details.

# COMMAND ----------

import re

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "default")
dbutils.widgets.text("warehouse_id", "")
dbutils.widgets.dropdown("cleanup", "yes", ["yes", "no"])

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
WAREHOUSE_ID = dbutils.widgets.get("warehouse_id").strip()
for name, value in (("catalog", CATALOG), ("schema", SCHEMA)):
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Set the '{name}' widget (letters, digits, underscores); got {value!r}")
if not re.fullmatch(r"[A-Za-z0-9]+", WAREHOUSE_ID):
    raise ValueError("Set the 'warehouse_id' widget to your SQL warehouse ID (SQL Warehouses → Connection details).")

EMPLOYEES = f"{CATALOG}.{SCHEMA}.bricksentry_hr_employees"
COMP = f"{CATALOG}.{SCHEMA}.bricksentry_hr_compensation"
CI = f"{CATALOG}.{SCHEMA}_ci"                    # where "pull request" versions of the tables are built
SANDBOX = f"{CATALOG}.bricksentry_sandbox"       # where preview_write puts throwaway clones


def sql(statement: str):
    """Run Spark SQL (a separate helper so the steps read clearly)."""
    return spark.sql(statement)


def show_hits(result: dict) -> None:
    for h in result.get("rule_hits", []):
        print(f"   [{h['verdict']}] {h['rule']}: {h['reason']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build the HR pipeline
# MAGIC 200,000 employees (50 with no department, so the summary has one NULL-key row), and pay by department.
# MAGIC Unity Catalog tags drive the policy: the employee table is **pii**, the summary is **certified**.

# COMMAND ----------

sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
sql(f"CREATE SCHEMA IF NOT EXISTS {CI}")
sql(f"""
CREATE OR REPLACE TABLE {EMPLOYEES} AS
SELECT
  CONCAT('HR-', LPAD(CAST(id AS STRING), 6, '0')) AS employee_id,
  CONCAT('Employee ', CAST(id AS STRING))         AS full_name,
  CASE WHEN id % 4000 = 0 THEN NULL
       WHEN id % 8 = 0 THEN 'Engineering' WHEN id % 8 = 1 THEN 'Sales'   WHEN id % 8 = 2 THEN 'Marketing'
       WHEN id % 8 = 3 THEN 'HR'          WHEN id % 8 = 4 THEN 'Finance' WHEN id % 8 = 5 THEN 'Operations'
       WHEN id % 8 = 6 THEN 'Support'     ELSE 'Legal' END AS department,
  CAST(id % 6 + 1 AS INT)                                  AS job_level,
  CAST(45000 + (id % 8) * 5000 + (id % 97) * 250 AS DOUBLE) AS salary,
  CAST(3 + (id % 13) AS DOUBLE)                            AS bonus_pct,
  CAST(2015 + (id % 10) AS INT)                            AS hire_year,
  (id % 10 <> 0)                                           AS is_active
FROM range(200000) AS t(id)
""")
sql(f"""
CREATE OR REPLACE TABLE {COMP} AS
SELECT department, COUNT(*) AS headcount, ROUND(SUM(salary), 2) AS total_salary, ROUND(AVG(salary), 2) AS avg_salary
FROM {EMPLOYEES} GROUP BY department
""")
sql(f"ALTER TABLE {EMPLOYEES} SET TAGS ('pii')")
sql(f"ALTER TABLE {COMP} SET TAGS ('certified')")
sql(f"ALTER TABLE {EMPLOYEES} ALTER COLUMN salary SET TAGS ('certified')")   # a column-level tag, used in step 7
display(sql(f"SELECT * FROM {COMP} ORDER BY department NULLS LAST"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configure bricksentry
# MAGIC The same settings you would put in `bricksentry.yml`. Every check runs on your SQL warehouse.

# COMMAND ----------

from bricksentry import Config, Sentry, __version__, agent_tools

CONFIG = {
    "backend": {"type": "databricks", "warehouse_id": WAREHOUSE_ID},
    "sandbox_schema": SANDBOX,
    "candidate_pattern": "{catalog}.{schema}_ci.{table}",   # pull-request versions live in <schema>_ci
    "assessment_store": False,                              # keep assessments in memory for this notebook
    "max_samples": 3,                                       # sample rows shown per change in reports
    "lineage": {"type": "databricks", "depth": 3, "lookback_days": 90, "column_depth": 2},
    "tables": {
        EMPLOYEES: {"keys": ["employee_id"], "segment_by": ["department"]},
        COMP: {"keys": ["department"]},
    },
    "policy": "builtin:default",
}
sentry = Sentry(Config.from_dict(CONFIG))
agent_tools.set_sentry(sentry)
print(f"bricksentry {__version__} · warehouse {WAREHOUSE_ID} · tables {EMPLOYEES}, {COMP}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Blast radius: what depends on the employee table?
# MAGIC From Unity Catalog lineage. Pass `columns` to see which downstream **columns** a column feeds, and their tags.
# MAGIC Lineage is recorded a few minutes after the tables are built, so on a first run this can be empty: run the cell
# MAGIC again later, or see step 9 for lineage from config.

# COMMAND ----------

ds = agent_tools.whats_downstream(EMPLOYEES, columns=["salary", "bonus_pct"])
if "error" in ds:
    print("lineage unavailable:", ds["error"])
else:
    print(f"{ds['count']} downstream asset(s), table tags {ds['tags']}")
    for d in ds["downstream"]:
        print(f"   depth {d['depth']}  {d['kind']:10s} {d['name']}  tags={d['tags']}")
    print("column tags:", ds.get("column_tags"))
    for c in ds.get("column_downstream", []):
        print(f"   {c['source_column']} → {c['table']}.{c['column']} (depth {c['depth']}, tags={c['tags']})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Check a change: two "pull requests"
# MAGIC In CI your build writes the changed tables to `<schema>_ci`. Here we build them by hand:
# MAGIC - **PR #42** changes `bonus_pct` for 100 employees: small, but the table is pii.
# MAGIC - **PR #43** has a join bug that duplicates the Sales row in the certified summary.

# COMMAND ----------

sql(f"CREATE OR REPLACE TABLE {CI}.bricksentry_hr_employees AS SELECT * FROM {EMPLOYEES}")
sql(f"UPDATE {CI}.bricksentry_hr_employees SET bonus_pct = bonus_pct + 1 WHERE employee_id LIKE 'HR-0000%'")
sql(f"CREATE OR REPLACE TABLE {CI}.bricksentry_hr_compensation AS SELECT * FROM {COMP}")
sql(f"INSERT INTO {CI}.bricksentry_hr_compensation SELECT * FROM {COMP} WHERE department = 'Sales'")

res = agent_tools.assess_change(tables=[EMPLOYEES, COMP], pr="42")
print(f"verdict: {res['verdict']} · risk: {res['risk_tier']} · safe_to_ship: {res['safe_to_ship']}")
print("review requested from:", res["notify"])
for t in res["tables"]:
    print(f"\n{t['table']} → {t['verdict']}")
    show_hits(t)
    print("  ", t["summary"][:240])
# Expected: employees → needs_human (pii table, certified downstream); compensation → block (duplicate key).

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Every rule, one small change at a time
# MAGIC Each line rebuilds the summary with one kind of mistake and shows which rules fire.

# COMMAND ----------

def try_change(label: str, build_sql: str, after: str | None = None) -> None:
    sql(f"CREATE OR REPLACE TABLE {CI}.bricksentry_hr_compensation AS {build_sql}")
    if after:
        sql(after.format(t=f"{CI}.bricksentry_hr_compensation"))
    t = agent_tools.assess_change(tables=[COMP])["tables"][0]
    print(f"{label:16s} → {t['verdict']:12s} {[(h['rule'], h['verdict']) for h in t['rule_hits']]}")


ALL = f"SELECT * FROM {COMP}"
try_change("NULL key", ALL, "UPDATE {t} SET department = NULL WHERE department = 'Sales'")
try_change("dropped column", f"SELECT department, headcount, total_salary FROM {COMP}")
try_change("row loss", ALL, "DELETE FROM {t} WHERE department IN ('Legal', 'Support')")
try_change("type change", f"SELECT department, CAST(headcount AS STRING) AS headcount, total_salary, avg_salary FROM {COMP}")
try_change("no change", ALL)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Preview a write before running it
# MAGIC An AI agent wants to raise Sales pay by 2%. `preview_write` runs the statement on a **throwaway clone** in the
# MAGIC sandbox and checks the result; production is never written to. Statements it can't preview safely are refused,
# MAGIC with a suggestion of what to do instead.

# COMMAND ----------

ok = agent_tools.preview_write(
    sql=f"UPDATE {COMP} SET avg_salary = avg_salary * 1.02 WHERE department = 'Sales'",
    target=COMP, keys=["department"],
)
print(f"preview → {ok['verdict']} · safe_to_ship: {ok['safe_to_ship']}")
show_hits(ok)

for label, statement in [
    ("DROP", f"DROP TABLE {COMP}"),
    ("TRUNCATE", f"TRUNCATE TABLE {COMP}"),
    ("ALTER", f"ALTER TABLE {COMP} DROP COLUMN avg_salary"),
    ("two statements", f"UPDATE {COMP} SET headcount = 0; DROP TABLE {COMP}"),
    ("GRANT", f"GRANT SELECT ON TABLE {COMP} TO `someone@example.com`"),
]:
    r = agent_tools.preview_write(sql=statement, target=COMP, keys=["department"])
    print(f"\n{label}: {r.get('error', 'not refused')}")

print("\nproduction rows still:", sql(f"SELECT COUNT(*) AS n FROM {COMP}").first()["n"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Explain it, gate it, and know how to undo it

# COMMAND ----------

sql(f"CREATE OR REPLACE TABLE {CI}.bricksentry_hr_employees AS SELECT * FROM {EMPLOYEES}")
sql(f"UPDATE {CI}.bricksentry_hr_employees SET bonus_pct = bonus_pct + 1 WHERE employee_id LIKE 'HR-0000%'")
res = agent_tools.assess_change(tables=[EMPLOYEES])

print("explain_diff:", agent_tools.explain_diff(table=EMPLOYEES)["explanation"])
gate = agent_tools.safe_to_ship(res["id"])
print(f"\nsafe_to_ship: {gate['safe_to_ship']} · verdict: {gate['verdict']}")
print("next_step:", gate["next_step"])
for t in (EMPLOYEES, COMP):
    print("\nrollback:", agent_tools.rollback_plan(t)["rollback_sql"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. The pull-request report
# MAGIC What `bricksentry assess --comment` posts on a pull request.

# COMMAND ----------

from bricksentry.report import to_markdown

report = to_markdown(sentry.assess([EMPLOYEES], pr="42"))
try:
    from IPython.display import Markdown
    from IPython.display import display as show_md
    show_md(Markdown(report))
except Exception:
    print(report)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Column rules and the built-in policies
# MAGIC `salary` is tagged **certified** at the column level. A custom policy can block any change that touches it, by tag
# MAGIC or by name. The six built-in policies cover common cases without writing rules.

# COMMAND ----------

from bricksentry import policy_templates

column_policy = {
    "default": "pass",
    "rules": [
        {"name": "certified-column-changed", "when": {"changed_column_tag": "certified"}, "then": "block"},
        {"name": "salary-freeze", "when": {"changed_column": "salary"}, "then": "block"},
    ],
}
agent_tools.set_sentry(Sentry(Config.from_dict({**CONFIG, "policy": column_policy})))
sql(f"CREATE OR REPLACE TABLE {CI}.bricksentry_hr_employees AS SELECT * FROM {EMPLOYEES}")
sql(f"UPDATE {CI}.bricksentry_hr_employees SET salary = salary * 1.03 WHERE department = 'Finance'")
res = agent_tools.assess_change(tables=[EMPLOYEES])
print("custom column policy →", res["verdict"])
show_hits(res)
agent_tools.set_sentry(sentry)

print("\nBuilt-in policies (use with policy: builtin:<name>):")
for name in policy_templates.TEMPLATES:
    print(f"   {name:11s} {len(policy_templates.get_template(name)['rules']):2d} rules")
print("\n" + policy_templates.to_yaml("agent-safe")[:600])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Sampling you can trust
# MAGIC Big tables can be checked on a sample (`sample_pct`). A sample can miss a small change, so when the sample shows
# MAGIC nothing but the tables differ, bricksentry re-runs the check on every row. Here a 5% sample of the 9-row summary would
# MAGIC miss the duplicated Sales row; the verdict is still **block**.

# COMMAND ----------

sql(f"CREATE OR REPLACE TABLE {CI}.bricksentry_hr_compensation AS SELECT * FROM {COMP}")
sql(f"INSERT INTO {CI}.bricksentry_hr_compensation SELECT * FROM {COMP} WHERE department = 'Sales'")
sampled = Sentry(Config.from_dict({**CONFIG, "sample_pct": 5}))
t = sampled.assess([COMP]).tables[0]
print("sample_pct=5 →", t.verdict.value, [h.rule for h in t.hits])
print("note:", t.diff.sample_note if t.diff else None)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Lineage from config
# MAGIC Without waiting for Unity Catalog lineage (or for assets lineage doesn't record), describe the blast radius in config:
# MAGIC tables, jobs, dashboards and Genie spaces, with owners and tags.

# COMMAND ----------

static = Sentry(Config.from_dict({**CONFIG, "lineage": {
    "type": "static",
    "edges": {
        EMPLOYEES: [COMP, "job:hr_compensation_refresh", "dashboard:HR Compensation Overview", "genie_space:HR Q&A"],
        COMP: ["dashboard:HR Compensation Overview"],
    },
    "owners": {EMPLOYEES: "hr-data-eng", COMP: "people-analytics"},
    "tags": {EMPLOYEES: {"pii": ""}, COMP: {"certified": "true"}},
    "column_edges": {f"{EMPLOYEES}.salary": [f"{COMP}.total_salary", f"{COMP}.avg_salary"]},
    "column_tags": {EMPLOYEES: {"salary": {"certified": ""}}},
}}))
agent_tools.set_sentry(static)
ds = agent_tools.whats_downstream(EMPLOYEES)
print(f"{ds['count']} downstream asset(s):")
for d in ds["downstream"]:
    print(f"   depth {d['depth']}  {d['kind']:11s} {d['name']}  owner={d['owner']} tags={d['tags']}")
res = agent_tools.assess_change(tables=[EMPLOYEES])
print("\nsalary change with this lineage →", res["verdict"])
show_hits(res)
agent_tools.set_sentry(sentry)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 10. Alerts, the command line, the agent server and the local demo
# MAGIC - **Alerts** go to Slack or Teams when a verdict needs a human. Set `BRICKSENTRY_SLACK_WEBHOOK` (as a secret) to send
# MAGIC   them; without one, nothing is sent and this cell only shows the message.
# MAGIC - **MCP**: the same six checks as tools for AI agents (`bricksentry mcp`, or `--read-only` to leave out `preview_write`).

# COMMAND ----------

import subprocess
import sys

from bricksentry import mcp_server, notify

assessment = sentry.assess([EMPLOYEES])
settings = notify.NotifySettings.from_config({"on": "needs_human"})
print("alert status:", notify.send(assessment, settings) or "no webhook set, nothing sent")
print("Slack message title:", notify.slack_payload(assessment)["text"])

print("\nMCP tools (read-only server leaves out preview_write):")
for fn in mcp_server.tools(read_only=False):
    print(f"   {fn.__name__:16s} read-only={fn.__name__ in agent_tools.READ_ONLY}")

cli = subprocess.run([sys.executable, "-m", "bricksentry", "policies"], capture_output=True, text=True)
print("\n$ bricksentry policies\n" + cli.stdout)

# COMMAND ----------

# The full agent loop on a local 20,000-row table (DuckDB, nothing leaves the cluster)
from bricksentry import demo

result = demo.run_demo()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Clean up
# MAGIC With **cleanup = yes**, removes everything this notebook created: the two demo tables, `<schema>_ci` and the
# MAGIC sandbox schema. Nothing else is touched.

# COMMAND ----------

if dbutils.widgets.get("cleanup") == "yes":
    sql(f"DROP SCHEMA IF EXISTS {CI} CASCADE")
    sql(f"DROP SCHEMA IF EXISTS {SANDBOX} CASCADE")
    sql(f"DROP TABLE IF EXISTS {EMPLOYEES}")
    sql(f"DROP TABLE IF EXISTS {COMP}")
    print("Removed the demo tables and the _ci and sandbox schemas.")
else:
    print(f"Kept {EMPLOYEES}, {COMP}, {CI} and {SANDBOX}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next steps
# MAGIC - **Check one of your own tables:** the [check-my-table notebook](https://github.com/bricksentry/bricksentry/blob/main/examples/databricks/bricksentry_check_my_table.py) looks at the last change your pipeline made.
# MAGIC - **Check every pull request:** [bricksentry.org/docs/ci](https://bricksentry.org/docs/ci).
# MAGIC - **Give AI agents the checks:** `uvx bricksentry-mcp`, or run it as a Databricks App: [deploy/databricks-app](https://github.com/bricksentry/bricksentry/tree/main/deploy/databricks-app).
# MAGIC - Found something wrong, or something useful? [Tell us](https://github.com/bricksentry/bricksentry/issues/new?template=it_caught_this.yml).
