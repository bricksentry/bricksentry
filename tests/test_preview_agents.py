import asyncio

import pytest

from bricksentry import agent_tools
from bricksentry.preview import PreviewGuardError, rewrite_for_preview


def q(x):
    return '"' + x + '"'


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE sales.orders",
        "DELETE FROM sales.orders; DROP TABLE sales.customers",
        "INSERT INTO sales.other SELECT * FROM sales.orders",
        "SELECT * FROM sales.orders",
        "TRUNCATE TABLE sales.orders",
        "DELETE FROM sales.customers WHERE 1=1",
    ],
)
def test_guard_rejects_unsafe(sql):
    with pytest.raises(PreviewGuardError):
        rewrite_for_preview(sql, "sales.orders", "sandbox.orders__preview_x", q)


def test_guard_rewrites_target_and_placeholder():
    out = rewrite_for_preview("MERGE INTO sales.orders t USING src s ON t.id = s.id WHEN MATCHED THEN DELETE",
                              "sales.orders", "sandbox.c1", q)
    assert out.startswith('MERGE INTO "sandbox"."c1"')
    out2 = rewrite_for_preview("UPDATE {{target}} SET status = 'x'", "sales.orders", "sandbox.c1", q)
    assert out2.startswith('UPDATE "sandbox"."c1"')
    # a string literal containing a semicolon is fine
    rewrite_for_preview("UPDATE sales.orders SET status = 'a;b'", "sales.orders", "sandbox.c1", q)


def test_preview_never_touches_production(sentry, backend):
    out = sentry.preview_write("DELETE FROM sales.orders WHERE region = 'APAC'", "sales.orders")
    assert backend.scalar("SELECT COUNT(*) FROM sales.orders") == 1000
    t = out["tables"][0]
    assert t["diff"]["removed_rows"] == 333
    assert out["verdict"] == "needs_human"
    assert any(h["rule"] == "row-loss" for h in out["rule_hits"])
    # clone is dropped
    assert backend.scalar(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'sandbox'"
    ) == 0


def test_agent_tool_loop(sentry, backend):
    agent_tools.set_sentry(sentry)
    try:
        ds = agent_tools.whats_downstream("sales.orders")
        assert ds["count"] == 3 and ds["owner"] == "sales-eng"

        backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders UNION ALL SELECT * FROM sales.orders WHERE order_id = 1")
        a = agent_tools.assess_change(["sales.orders"])
        assert a["verdict"] == "block"

        gate = agent_tools.safe_to_ship(a["id"])
        assert gate["safe_to_ship"] is False and gate["next_step"].startswith("Stop")

        ex = agent_tools.explain_diff(assessment_id=a["id"])
        assert "duplicate key" in ex["explanations"]["sales.orders"]

        rb = agent_tools.rollback_plan("sales.orders")
        assert "rollback_sql" in rb

        err = agent_tools.preview_write("DROP TABLE sales.orders", "sales.orders")
        assert "error" in err
    finally:
        agent_tools.set_sentry(None)


def test_mcp_server_lists_tools():
    pytest.importorskip("mcp")
    from bricksentry.mcp_server import build_server

    tools = asyncio.run(build_server().list_tools())
    assert {t.name for t in tools} == {
        "whats_downstream", "assess_change", "preview_write", "explain_diff", "safe_to_ship", "rollback_plan",
    }
