import pytest

from bricksentry import Config, Sentry
from bricksentry.backends.duckdb_backend import DuckDBBackend


@pytest.fixture
def backend():
    b = DuckDBBackend()
    b.execute("CREATE SCHEMA sales; CREATE SCHEMA sales_ci")
    b.execute(
        """
        CREATE TABLE sales.orders AS
        SELECT i AS order_id,
               CASE WHEN i % 3 = 0 THEN 'APAC' ELSE 'EMEA' END AS region,
               (i * 1.5)::DOUBLE AS amount,
               'ok' AS status
        FROM range(1, 1001) t(i)
        """
    )
    return b


@pytest.fixture
def config():
    return Config.from_dict(
        {
            "sandbox_schema": "sandbox",
            "tables": {"sales.orders": {"keys": ["order_id"], "segment_by": ["region"]}},
            "lineage": {
                "type": "static",
                "edges": {
                    "sales.orders": ["sales.daily_revenue", "dashboard:Revenue Overview"],
                    "sales.daily_revenue": ["job:finance_close"],
                },
                "owners": {"sales.daily_revenue": "finance-team", "sales.orders": "sales-eng"},
                "tags": {"sales.daily_revenue": {"certified": "true"}},
            },
        }
    )


@pytest.fixture
def sentry(backend, config):
    return Sentry(config, backend=backend)
