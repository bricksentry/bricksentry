"""Run the "check my table" Databricks notebook end to end on DuckDB.

Databricks-only statements (DESCRIBE HISTORY, SHALLOW CLONE ... VERSION AS OF, the
primary-key lookup) are answered by a small fake backend, so every Python line of the
notebook runs exactly as written.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import bricksentry.assess as assess_mod
from bricksentry.backends.duckdb_backend import DuckDBBackend

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "examples" / "databricks" / "bricksentry_check_my_table.py"
sys.path.insert(0, str(ROOT / "scripts"))
import build_notebook  # noqa: E402

HISTORY = [
    {"version": "3", "timestamp": "2026-11-11 10:00:00", "operation": "OPTIMIZE", "operationMetrics": "{}"},
    {"version": "2", "timestamp": "2026-11-11 09:00:00", "operation": "MERGE", "operationMetrics": "{}"},
    {"version": "1", "timestamp": "2026-11-10 09:00:00", "operation": "WRITE", "operationMetrics": "{}"},
]


class FakeDelta(DuckDBBackend):
    def __init__(self):
        super().__init__()
        self.statements: list[str] = []
        self.execute(
            """CREATE SCHEMA main_sales; CREATE SCHEMA main_scratch;
            CREATE TABLE main_sales.v1 AS SELECT i AS order_id, i * 1.5 AS amount, 2000 + i % 20 AS order_year
                FROM range(1, 5001) t(i);
            CREATE TABLE main_sales.v2 AS SELECT * FROM main_sales.v1 WHERE order_id % 50 <> 0
                UNION ALL SELECT * FROM main_sales.v1 WHERE order_id < 30;
            CREATE TABLE main_sales.orders AS SELECT * FROM main_sales.v2;"""
        )

    def qualified(self, table):  # catalog.schema.table -> catalog_schema.table in DuckDB
        p = table.split(".")
        return table if len(p) < 3 else f"{p[0]}_{p[1]}.{p[2]}"

    def ensure_schema(self, schema):  # catalog.schema -> catalog_schema, created in __init__
        assert schema == "main.scratch"

    def query(self, sql):
        if sql.startswith("DESCRIBE HISTORY"):
            return HISTORY
        if "information_schema.table_constraints" in sql:
            return [{"column_name": "order_id"}]
        return super().query(sql)

    def execute(self, sql):
        self.statements.append(sql)
        m = re.match(r"CREATE OR REPLACE TABLE (\S+) SHALLOW CLONE (\S+) VERSION AS OF (\d+)", sql)
        if m:
            return super().execute(f"CREATE OR REPLACE TABLE {m.group(1)} AS SELECT * FROM main_sales.v{m.group(3)}")
        return super().execute(sql)

    def current_version(self, table):
        return 3


def _code() -> str:
    cells = build_notebook.build(NB)["cells"]
    out = []
    for c in cells:
        src = "".join(c["source"])
        if c["cell_type"] == "code" and not src.lstrip().startswith("%"):
            out.append(src.replace("dbutils.library.restartPython()", ""))
    return "\n\n".join(out)


@pytest.fixture
def run_notebook(monkeypatch, capsys):
    def run(**widgets):
        values = {"table": "main.sales.orders", "keys": "", "warehouse_id": "abc123",
                  "scratch_schema": "main.scratch", "version": "", "policy": "default", "sample_pct": ""}
        values.update(widgets)

        class Widgets:
            def text(self, k, d):
                values.setdefault(k, d)

            def dropdown(self, k, d, opts):
                assert values.get(k, d) in opts
                values.setdefault(k, d)

            def get(self, k):
                return values[k]

        fake = FakeDelta()
        real_init = assess_mod.Sentry.__init__
        monkeypatch.setattr(assess_mod.Sentry, "__init__",
                            lambda self, config=None, backend=None: real_init(self, config, backend=fake))
        ns = {"dbutils": types.SimpleNamespace(widgets=Widgets()), "__name__": "__main__"}
        exec(compile(_code(), str(NB), "exec"), ns)
        return ns, fake, capsys.readouterr().out
    return run


def test_checks_the_latest_data_change_and_cleans_up(run_notebook):
    ns, fake, out = run_notebook()
    assert "Checking version 2 (MERGE) against version 1." in out  # skips the OPTIMIZE
    assert "Keys: order_id" in out                                  # from the primary key
    a = ns["assessment"]
    assert a.verdict.value == "block"
    rules = {h.rule for h in a.hits}
    assert {"duplicate-keys", "row-loss"} <= rules
    assert "diff-failed" not in rules                                # lineage failure is not a diff failure
    assert "lineage-unavailable" in rules
    assert "order_year" not in a.tables[0].summary                   # not a measure
    assert "RESTORE TABLE main_sales.orders TO VERSION AS OF 1" in out
    assert "VERSION AS OF 3" not in ns["report"]                     # the report undoes the change, not "now"
    assert not fake.table_exists("main.scratch.bs_check_orders_v1")  # clones dropped
    assert not fake.table_exists("main.scratch.bs_check_orders_v2")


def test_share_link_prefills_the_form_without_names(run_notebook):
    ns, _, out = run_notebook()
    url = next(line for line in out.splitlines() if line.startswith("https://github.com/"))
    q = parse_qs(urlparse(url).query)
    assert q["template"] == ["it_caught_this.yml"]
    assert q["verdict"] == ["block"]
    form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "it_caught_this.yml").read_text()
    for field in ("verdict", "rules", "facts", "version"):
        assert f"id: {field}" in form
    assert "orders" not in url and "order_id" not in url and "amount" not in url


def test_explicit_version_and_keys(run_notebook):
    ns, _, out = run_notebook(version="2", keys="order_id")
    assert "Checking version 2" in out


def test_bad_widget_values_fail_clearly(run_notebook):
    with pytest.raises(ValueError, match="catalog.schema.table"):
        run_notebook(table="orders")
    with pytest.raises(ValueError, match="version"):
        run_notebook(version="latest")


def test_ipynb_copy_is_up_to_date():
    on_disk = json.loads(NB.with_suffix(".ipynb").read_text())
    assert on_disk == json.loads(json.dumps(build_notebook.build(NB)))
