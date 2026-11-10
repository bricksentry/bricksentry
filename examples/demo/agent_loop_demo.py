"""Runnable demo (no Databricks needed): an "agent" makes a buggy change, bricksentry
catches it, the agent fixes it, and the gate routes the final change to a human.

    pip install "bricksentry[duckdb]"
    python examples/demo/agent_loop_demo.py
"""

from __future__ import annotations

import json
import textwrap

from bricksentry import Config, Sentry, agent_tools
from bricksentry.backends.duckdb_backend import DuckDBBackend


def banner(s: str) -> None:
    print("\n" + "=" * 78 + f"\n{s}\n" + "=" * 78)


def show(obj, keys=None):
    if keys:
        obj = {k: obj[k] for k in keys if k in obj}
    print(textwrap.indent(json.dumps(obj, indent=2, default=str), "  "))


def seed(b: DuckDBBackend) -> None:
    b.execute("CREATE SCHEMA sales; CREATE SCHEMA sales_pr_42")
    b.execute(
        """
        CREATE TABLE sales.orders AS
        SELECT i AS order_id,
               ['APAC','EMEA','AMER'][1 + (i % 3)] AS region,
               round(20 + (i % 97) * 3.1, 2)::DOUBLE AS amount,
               CASE WHEN i % 10 = 0 THEN 'refunded' ELSE 'paid' END AS status
        FROM range(1, 20001) t(i)
        """
    )


CONFIG = {
    "sandbox_schema": "sandbox",
    "candidate_pattern": "{schema}_pr_{pr}.{table}",
    "tables": {"sales.orders": {"keys": ["order_id"], "segment_by": ["region"]}},
    "lineage": {
        "type": "static",
        "edges": {
            "sales.orders": ["sales.daily_revenue", "job:orders_hourly", "dashboard:Exec Revenue"],
            "sales.daily_revenue": ["finance.month_close", "genie_space:Revenue Q&A"],
        },
        "owners": {"sales.orders": "sales-data-eng", "sales.daily_revenue": "finance-analytics",
                   "finance.month_close": "finance-analytics"},
        "tags": {"sales.daily_revenue": {"certified": "true"}},
    },
}


def main() -> None:
    b = DuckDBBackend()
    seed(b)
    agent_tools.set_sentry(Sentry(Config.from_dict(CONFIG), backend=b))

    banner("1. Agent asks: what depends on sales.orders before I touch it?")
    ds = agent_tools.whats_downstream("sales.orders")
    for n in ds["downstream"]:
        print(f"  depth {n['depth']}  [{n['kind']}] {n['name']}  {n['owner'] or ''} {n['tags'] or ''}")

    banner("2. Agent captures a rollback plan first")
    show(agent_tools.rollback_plan("sales.orders"), ["rollback_sql"])

    banner("3. Agent wants to 'clean up refunds' directly. It previews the write first")
    p = agent_tools.preview_write("DELETE FROM sales.orders WHERE status <> 'paid'", "sales.orders")
    print("  verdict:", p["verdict"], "| risk:", p["risk_tier"])
    print("  ", p["tables"][0]["summary"])
    print("  production rows still:", b.scalar("SELECT COUNT(*) FROM sales.orders"))

    banner("4. Agent instead edits the pipeline. Its first attempt has a join bug (fan-out)")
    b.execute(
        """
        CREATE TABLE sales_pr_42.orders AS
        SELECT o.order_id, o.region,
               CASE WHEN o.region = 'APAC' THEN round(o.amount * 0.92, 2) ELSE o.amount END AS amount,
               o.status
        FROM sales.orders o
        LEFT JOIN (SELECT * FROM (VALUES (1), (1)) v(k)) dup ON o.order_id % 500 = 0 AND dup.k = 1
        """
    )
    a = agent_tools.assess_change(["sales.orders"], pr="42")
    print("  verdict:", a["verdict"])
    for h in a["rule_hits"]:
        print(f"   - [{h['verdict']}] {h['rule']}: {h['reason']}")
    gate = agent_tools.safe_to_ship(a["id"])
    print("  next_step:", gate["next_step"])

    banner("5. Agent fixes the join, rebuilds the candidate and assesses again")
    b.execute(
        """
        CREATE OR REPLACE TABLE sales_pr_42.orders AS
        SELECT order_id, region,
               CASE WHEN region = 'APAC' THEN round(amount * 0.92, 2) ELSE amount END AS amount,
               status
        FROM sales.orders
        """
    )
    a2 = agent_tools.assess_change(["sales.orders"], pr="42")
    print("  verdict:", a2["verdict"])
    print("  ", agent_tools.explain_diff(assessment_id=a2["id"])["explanations"]["sales.orders"])
    gate2 = agent_tools.safe_to_ship(a2["id"])
    print("  safe_to_ship:", gate2["safe_to_ship"])
    print("  next_step:", gate2["next_step"])

    banner("6. The PR comment a human reviewer sees")
    from bricksentry.report import to_markdown

    print(to_markdown(agent_tools.get_sentry().get_assessment(a2["id"])))


if __name__ == "__main__":
    main()
