"""Run the HR walkthrough notebook end to end on DuckDB.

Spark SQL goes to a DuckDB-backed stand-in for `spark`, Unity Catalog tag DDL is
ignored, and bricksentry's backend is the same DuckDB database, so every Python
line of the notebook runs as written. Unity Catalog lineage isn't available here,
which also exercises the notebook's "lineage unavailable" path.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

import bricksentry.assess as assess_mod
from bricksentry.backends.duckdb_backend import DuckDBBackend
from bricksentry.lineage import UnityCatalogLineage

pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "examples" / "databricks" / "bricksentry_hr_walkthrough.py"
sys.path.insert(0, str(ROOT / "scripts"))
import build_notebook  # noqa: E402


def _local(name: str) -> str:
    """main.hr.t -> main_hr.t, main.hr_ci -> main_hr_ci (DuckDB has one catalog here)."""
    name = re.sub(r'"main"\."(\w+)"', r'"main_\1"', name)
    return re.sub(r"\bmain\.(\w+)(?=[\s.;,)`]|$)", r"main_\1", name)


class Backend(DuckDBBackend):
    def qualified(self, table):
        p = table.split(".")
        if len(p) == 3:
            return f"{p[0]}_{p[1]}.{p[2]}"
        if len(p) == 2 and p[0] == "main":
            return f"main_{p[1]}"
        return table

    def execute(self, sql):
        return super().execute(_local(sql))


class Result:
    def __init__(self, rows):
        self.rows = rows

    def first(self):
        return self.rows[0] if self.rows else None


class FakeSpark:
    def __init__(self, backend):
        self.backend = backend
        self.statements = []

    def sql(self, statement):
        self.statements.append(statement)
        if re.search(r"\bSET TAGS\b", statement):
            return Result([])  # Unity Catalog tags: nothing to do on DuckDB
        s = _local(statement)
        if s.lstrip().upper().startswith(("SELECT", "WITH")):
            return Result(self.backend.query(s))
        self.backend.execute(s)
        return Result([])


db_ref: list = []
E, C = "main.hr.bricksentry_hr_employees", "main.hr.bricksentry_hr_compensation"


def _unity_catalog_stand_in(backend):
    """The system tables bricksentry reads, as they'd look once lineage is recorded.
    Loaded without the name rewriting, so the stored names are the real three-part ones."""
    backend = types.SimpleNamespace(execute=lambda q: DuckDBBackend.execute(db_ref[0], q))
    backend.execute("CREATE SCHEMA uc")
    backend.execute(f"""CREATE TABLE uc.table_lineage AS SELECT * FROM (VALUES
      ('{E}', '{C}', 'NOTEBOOK', 'nb1', current_date),
      ('{C}', NULL, 'DASHBOARD_V3', 'hr-dash', current_date)
    ) t(source_table_full_name, target_table_full_name, entity_type, entity_id, event_date)""")
    backend.execute(f"""CREATE TABLE uc.column_lineage AS SELECT * FROM (VALUES
      ('{E}', 'salary', '{C}', 'total_salary', current_date),
      ('{E}', 'salary', '{C}', 'avg_salary', current_date)
    ) t(source_table_full_name, source_column_name, target_table_full_name, target_column_name, event_date)""")
    backend.execute("""CREATE TABLE uc.tables AS SELECT * FROM (VALUES
      ('main', 'hr', 'bricksentry_hr_employees', 'hr-data-eng'),
      ('main', 'hr', 'bricksentry_hr_compensation', 'people-analytics')
    ) t(table_catalog, table_schema, table_name, table_owner)""")
    backend.execute("""CREATE TABLE uc.table_tags AS SELECT * FROM (VALUES
      ('main', 'hr', 'bricksentry_hr_employees', 'pii', NULL),
      ('main', 'hr', 'bricksentry_hr_compensation', 'certified', NULL)
    ) t(catalog_name, schema_name, table_name, tag_name, tag_value)""")
    backend.execute("""CREATE TABLE uc.column_tags AS SELECT * FROM (VALUES
      ('main', 'hr', 'bricksentry_hr_employees', 'salary', 'certified', NULL)
    ) t(catalog_name, schema_name, table_name, column_name, tag_name, tag_value)""")


@pytest.fixture
def run(monkeypatch, capsys):
    backend = db = Backend()
    db_ref[:] = [db]
    _unity_catalog_stand_in(backend)
    real_make = assess_mod.make_lineage

    def make_lineage(cfg, b):
        if (cfg or {}).get("type", "databricks") != "databricks":
            return real_make(cfg, b)
        return UnityCatalogLineage(
            b, lookback_days=90, lineage_table="uc.table_lineage", tables_table="uc.tables",
            tags_table="uc.table_tags", column_lineage_table="uc.column_lineage", column_tags_table="uc.column_tags")

    monkeypatch.setattr(assess_mod, "make_lineage", make_lineage)
    real_init = assess_mod.Sentry.__init__
    monkeypatch.setattr(assess_mod.Sentry, "__init__",
                        lambda self, config=None, backend=None, **k: real_init(self, config, backend=backend or db, **k))
    widgets = {"catalog": "main", "schema": "hr", "warehouse_id": "abc123", "cleanup": "yes"}
    dbutils = types.SimpleNamespace(widgets=types.SimpleNamespace(
        text=lambda k, d: widgets.setdefault(k, d),
        dropdown=lambda k, d, opts: widgets.setdefault(k, d),
        get=lambda k: widgets[k],
    ))
    cells = build_notebook.build(NB)["cells"]
    code = "\n\n".join(
        "".join(c["source"]).replace("dbutils.library.restartPython()", "")
        for c in cells
        if c["cell_type"] == "code" and not "".join(c["source"]).lstrip().startswith("%")
    )
    spark = FakeSpark(backend)
    ns = {"spark": spark, "dbutils": dbutils, "display": lambda *a, **k: None, "__name__": "__main__"}
    exec(compile(code, str(NB), "exec"), ns)
    return ns, spark, backend, capsys.readouterr().out


def test_hr_walkthrough_runs_end_to_end(run):
    ns, spark, backend, out = run
    # step 1: table and column blast radius from (stand-in) Unity Catalog lineage
    assert "3 downstream asset(s), table tags {'pii': ''}" in out
    assert "depth 2  dashboard" in out
    assert "salary → main.hr.bricksentry_hr_compensation.total_salary" in out
    assert "column tags: {'salary': {'certified': ''}}" in out
    # step 2: pii table needs a human, duplicated Sales row blocks
    assert "verdict: block" in out
    assert "review requested from: ['hr-data-eng', 'people-analytics']" in out
    assert re.search(r"bricksentry_hr_employees → needs_human", out)
    assert re.search(r"bricksentry_hr_compensation → block", out)
    # step 3: each mistake trips its rule
    for label, rule in [("NULL key", "null-keys"), ("dropped column", "dropped-column"),
                        ("row loss", "row-loss"), ("type change", "type-change")]:
        line = next(line for line in out.splitlines() if line.startswith(label))
        assert rule in line, line
    assert re.search(r"^no change\s+→ pass", out, re.M)
    # step 4: preview ran on a clone; all five refusals
    assert out.count("PreviewGuardError") == 5
    assert "production rows still: 9" in out
    # step 7: the column policy blocks a salary change
    assert "custom column policy → block" in out
    assert "certified-column-changed" in out and "salary-freeze" in out
    # step 8: the sample can't hide the duplicate
    assert re.search(r"sample_pct=5 → block \[.*duplicate-keys", out)
    # step 9: static lineage, with column edges
    assert "4 downstream asset(s):" in out
    assert "genie_space" in out
    # step 10 + demo
    assert "no webhook set, nothing sent" in out
    assert "agent-safe" in out
    assert ns["result"]["buggy_verdict"] == "block"
    # cleanup removed everything the notebook created
    assert not backend.table_exists("main.hr.bricksentry_hr_employees")
    assert not backend.table_exists("main.hr.bricksentry_hr_compensation")
    assert not any("DROP TABLE" in s and "bricksentry_hr" not in s and "_ci" not in s for s in spark.statements)


def test_hr_walkthrough_has_no_workspace_specifics():
    text = NB.read_text()
    for private in ("94af1bc5", "k.b.reddy", "@gmail.com", "workspace.default"):
        assert private not in text


def test_hr_ipynb_copy_is_up_to_date():
    import json

    on_disk = json.loads(NB.with_suffix(".ipynb").read_text())
    assert on_disk == json.loads(json.dumps(build_notebook.build(NB)))
