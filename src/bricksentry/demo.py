"""``bricksentry demo``: the whole agent safety loop in about 30 seconds, locally.

Runs on an in-memory DuckDB database with a 20,000-row orders table and a small
lineage graph, so it needs no Databricks workspace and no data leaves your machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from bricksentry import agent_tools
from bricksentry.assess import Sentry
from bricksentry.config import Config

DOCS_URL = "https://bricksentry.org/docs"

CONFIG: dict[str, Any] = {
    "sandbox_schema": "sandbox",
    "candidate_pattern": "{schema}_pr_{pr}.{table}",
    "tables": {"sales.orders": {"keys": ["order_id"], "segment_by": ["region"]}},
    "lineage": {
        "type": "static",
        "edges": {
            "sales.orders": ["sales.daily_revenue", "job:orders_hourly", "dashboard:Exec Revenue"],
            "sales.daily_revenue": ["finance.month_close", "genie_space:Revenue Q&A"],
        },
        "owners": {
            "sales.orders": "sales-data-eng",
            "sales.daily_revenue": "finance-analytics",
            "finance.month_close": "finance-analytics",
        },
        "tags": {"sales.daily_revenue": {"certified": "true"}},
    },
}

SEED_SQL = """
CREATE TABLE sales.orders AS
SELECT i AS order_id,
       ['APAC','EMEA','AMER'][1 + (i % 3)] AS region,
       round(20 + (i % 97) * 3.1, 2)::DOUBLE AS amount,
       CASE WHEN i % 10 = 0 THEN 'refunded' ELSE 'paid' END AS status
FROM range(1, 20001) t(i)
"""

# The agent's first attempt: a join that fans out 40 orders (duplicate keys).
BUGGY_SQL = """
CREATE OR REPLACE TABLE sales_pr_42.orders AS
SELECT o.order_id, o.region,
       CASE WHEN o.region = 'APAC' THEN round(o.amount * 0.92, 2) ELSE o.amount END AS amount,
       o.status
FROM sales.orders o
LEFT JOIN (SELECT * FROM (VALUES (1), (1)) v(k)) dup ON o.order_id % 500 = 0 AND dup.k = 1
"""

FIXED_SQL = """
CREATE OR REPLACE TABLE sales_pr_42.orders AS
SELECT order_id, region,
       CASE WHEN region = 'APAC' THEN round(amount * 0.92, 2) ELSE amount END AS amount,
       status
FROM sales.orders
"""

DELETE_SQL = "DELETE FROM sales.orders WHERE status <> 'paid'"

# Same verdict colors as the website: pass green, warn amber, needs_human orange, block red.
_COLORS: dict[str, Any] = {"pass": (29, 160, 100), "warn": (214, 160, 40), "needs_human": (235, 115, 40), "block": (210, 40, 50)}


def _verdict(v: str) -> str:
    return click.style(f" {v.upper()} ", fg="white", bg=_COLORS.get(v, "white"), bold=True)


def _step(n: int, total: int, title: str, tool: str) -> None:
    click.echo()
    click.echo(
        click.style(f"[{n}/{total}] ", fg="cyan", bold=True)
        + click.style(title, bold=True)
        + click.style(f"   {tool}", fg="bright_black")
    )


def _line(text: str = "", **style: Any) -> None:
    click.echo("      " + (click.style(text, **style) if style else text))


def _short(summary: str) -> str:
    """Drop the table-name prefix and the lineage sentence (already shown in step 1)."""
    s = summary.split(" sales.orders feeds")[0]
    return s.removeprefix("sales.orders: ")


def _hits(rule_hits: list[dict[str, Any]]) -> None:
    for h in rule_hits:
        _line(click.style(f"[{h['verdict']}]", fg=_COLORS.get(h["verdict"], "white")) + f" {h['rule']}: {h['reason']}")


def run_demo(report_path: str | None = None) -> dict[str, Any]:
    """Run the demo, print it, and return the key results (used by tests)."""
    try:
        from bricksentry.backends.duckdb_backend import DuckDBBackend

        backend = DuckDBBackend()
    except ImportError as e:
        raise click.ClickException(
            'The demo runs on DuckDB. Install it with:  pip install "bricksentry[duckdb]"'
        ) from e

    backend.execute("CREATE SCHEMA sales; CREATE SCHEMA sales_pr_42")
    backend.execute(SEED_SQL)
    sentry = Sentry(Config.from_dict({**CONFIG, "assessment_store": False}), backend=backend)
    previous = agent_tools._SENTRY
    agent_tools.set_sentry(sentry)
    results: dict[str, Any] = {}
    try:
        click.echo(
            click.style("bricksentry demo", bold=True)
            + click.style("  ·  20,000-row sales.orders on local DuckDB; nothing leaves your machine", fg="bright_black")
        )
        click.echo(click.style("An AI agent is changing a revenue table. Watch what bricksentry tells it.", fg="bright_black"))
        total = 5

        _step(1, total, "Before editing: what depends on sales.orders?", "whats_downstream")
        ds = agent_tools.whats_downstream("sales.orders")
        _line("sales.orders")
        children: dict[str, list[dict[str, Any]]] = {}
        for n in ds["downstream"]:
            children.setdefault(n["via"], []).append(n)

        def walk(parent: str, prefix: str) -> None:
            kids = children.get(parent, [])
            for i, n in enumerate(kids):
                last = i == len(kids) - 1
                tags = " ".join(f"[{k}]" for k in n["tags"])
                _line(
                    f"{prefix}{'└─' if last else '├─'} {n['kind']:<11} {n['name']:<22} "
                    + click.style(n["owner"] or "", fg="bright_black")
                    + (" " + click.style(tags, fg=_COLORS["needs_human"]) if tags else "")
                )
                walk(n["name"], prefix + ("   " if last else "│  "))

        walk("sales.orders", "")
        results["downstream_count"] = ds["count"]

        _step(2, total, "Capture a rollback plan first", "rollback_plan")
        _line("On Databricks this returns the exact command, e.g.", fg="bright_black")
        _line("RESTORE TABLE `main`.`sales`.`orders` TO VERSION AS OF 41")
        _line("(DuckDB has no table history, so the demo skips it.)", fg="bright_black")

        _step(3, total, "Agent wants to delete refunded orders. It previews first", "preview_write")
        _line(DELETE_SQL, fg="bright_black")
        p = agent_tools.preview_write(DELETE_SQL, "sales.orders")
        _line(_verdict(p["verdict"]) + "  " + _short(p["tables"][0]["summary"]))
        _hits(p["rule_hits"])
        prod_rows = backend.scalar("SELECT COUNT(*) FROM sales.orders")
        _line(f"Ran on a disposable clone. Production still has {prod_rows:,} rows.", fg="green")
        results["preview_verdict"] = p["verdict"]
        results["production_rows_after_preview"] = prod_rows

        _step(4, total, "Agent opens PR #42 instead. Its join has a fan-out bug", "assess_change")
        backend.execute(BUGGY_SQL)
        a = agent_tools.assess_change(["sales.orders"], pr="42")
        _line(_verdict(a["verdict"]) + f"  risk {a['risk_tier']}")
        _hits(a["rule_hits"])
        gate = agent_tools.safe_to_ship(a["id"])
        _line("next_step: " + gate["next_step"], fg="cyan")
        results["buggy_verdict"] = a["verdict"]

        _step(5, total, "Agent fixes the join, rebuilds and checks again", "assess_change → safe_to_ship")
        backend.execute(FIXED_SQL)
        a2 = agent_tools.assess_change(["sales.orders"], pr="42")
        explanation = agent_tools.explain_diff(assessment_id=a2["id"])["explanations"]["sales.orders"]
        _line(_verdict(a2["verdict"]) + "  " + _short(explanation))
        _hits(a2["rule_hits"])
        gate2 = agent_tools.safe_to_ship(a2["id"])
        _line("next_step: " + gate2["next_step"], fg="cyan")
        results["fixed_verdict"] = a2["verdict"]
        results["notify"] = gate2["notify"]

        if report_path:
            from bricksentry.report import to_markdown

            Path(report_path).write_text(to_markdown(sentry.get_assessment(a2["id"])))
            click.echo()
            click.echo(f"Wrote {report_path}: the pull-request comment a reviewer would see.")

        click.echo()
        click.echo(click.style("The agent was stopped once, corrected itself, and handed the final call to the owners.", bold=True))
        click.echo("Next: run `bricksentry init` in your project, then see " + click.style(DOCS_URL, underline=True))
    finally:
        agent_tools.set_sentry(previous)
    return results
