"""The Databricks backend against a fake SDK client, and UC lineage SQL against
DuckDB copies of the system tables (same queries, configurable table names)."""

from types import SimpleNamespace as NS

from bricksentry.backends.databricks_backend import DatabricksBackend
from bricksentry.lineage import UnityCatalogLineage


class FakeStatements:
    def __init__(self):
        self.calls = []

    def execute_statement(self, statement, warehouse_id, **kw):
        self.calls.append(statement)
        return NS(
            statement_id="s1",
            status=NS(state=NS(value="PENDING")),
            manifest=None,
            result=None,
        )

    def get_statement(self, statement_id):
        cols = [NS(name="n", type_name=NS(value="LONG")), NS(name="name", type_name=NS(value="STRING"))]
        return NS(
            statement_id=statement_id,
            status=NS(state=NS(value="SUCCEEDED")),
            manifest=NS(schema=NS(columns=cols)),
            result=NS(data_array=[["1", "a"]], next_chunk_index=1),
        )

    def get_statement_result_chunk_n(self, statement_id, idx):
        return NS(data_array=[["2", None]], next_chunk_index=None)


def test_databricks_backend_polls_and_pages(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeStatements()
    b = DatabricksBackend(warehouse_id="wh", client=NS(statement_execution=fake))
    rows = b.query("SELECT 1")
    assert rows == [{"n": 1, "name": "a"}, {"n": 2, "name": None}]
    assert b.qualified("main.sales.orders") == "`main`.`sales`.`orders`"
    assert "xxhash64" in b.sample_predicate(["id"], 10)


def test_uc_lineage_walk(backend):
    backend.execute("CREATE SCHEMA sys")
    backend.execute(
        """
        CREATE TABLE sys.table_lineage AS SELECT * FROM (VALUES
          ('main.sales.orders', 'main.sales.daily_revenue', 'JOB', '111', current_date),
          ('main.sales.orders', NULL, 'DASHBOARD_V3', 'dash-9', current_date),
          ('main.sales.daily_revenue', 'main.finance.close', 'NOTEBOOK', '222', current_date),
          ('main.sales.orders', 'main.old.stale', 'JOB', '333', current_date - INTERVAL 400 DAY)
        ) t(source_table_full_name, target_table_full_name, entity_type, entity_id, event_date)
        """
    )
    backend.execute(
        """CREATE TABLE sys.tables AS SELECT * FROM (VALUES
          ('main','sales','daily_revenue','finance-team')) t(table_catalog, table_schema, table_name, table_owner)"""
    )
    backend.execute(
        """CREATE TABLE sys.table_tags AS SELECT * FROM (VALUES
          ('main','sales','daily_revenue','certified', NULL)) t(catalog_name, schema_name, table_name, tag_name, tag_value)"""
    )
    lin = UnityCatalogLineage(
        backend, lookback_days=90,
        lineage_table="sys.table_lineage", tables_table="sys.tables", tags_table="sys.table_tags",
    )
    nodes = lin.downstream("main.sales.orders", depth=3)
    names = {(n.kind, n.name, n.depth) for n in nodes}
    assert ("table", "main.sales.daily_revenue", 1) in names
    assert ("dashboard", "dashboard_v3:dash-9", 1) in names
    assert ("table", "main.finance.close", 2) in names
    assert not any("stale" in n.name for n in nodes)  # outside lookback window
    rev = next(n for n in nodes if n.name == "main.sales.daily_revenue")
    assert rev.owner == "finance-team" and "certified" in rev.tags
