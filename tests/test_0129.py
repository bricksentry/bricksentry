"""0.1.29: the engine's own query plan must agree with agent_tables, and pinning costs fewer queries."""

from __future__ import annotations

import pytest

from bricksentry import Config, Sentry, agent_tools
from bricksentry.backends.databricks_backend import DatabricksBackend, PlanError, parse_plan_tables
from bricksentry.backends.duckdb_backend import DuckDBBackend
from bricksentry.diff import diff_tables

PLAN = """== Parsed Logical Plan ==
'UpdateTable [assignment('x, 'f.get_ssn())]
+- 'UnresolvedRelation [main, ok, t]

== Analyzed Logical Plan ==
UpdateTable [assignment(x#1, scalar-subquery#5 [])]
:  +- Aggregate [max(ssn#9) AS max(ssn)#10]
:     +- SubqueryAlias main.bad.secret
:        +- Relation main.bad.secret[id#8,ssn#9] parquet
+- SubqueryAlias main.ok.t
   +- Relation main.ok.t[id#0,x#1] parquet

== Optimized Logical Plan ==
Relation main.should.not_count[id#0] parquet

== Physical Plan ==
..."""

VIEW_PLAN = """== Analyzed Logical Plan ==
InsertIntoStatement main.ok.t
+- Project [id#1]
   +- SubqueryAlias main.ok.v
      +- View (`main`.`ok`.`v`, [id#1])
         +- Relation main.bad.secret[id#1] parquet
== Optimized Logical Plan ==
"""


def test_plan_parsing_finds_tables_inside_functions_and_views():
    assert parse_plan_tables(PLAN) == ["main.bad.secret", "main.ok.t"]
    assert set(parse_plan_tables(VIEW_PLAN)) == {"main.bad.secret", "main.ok.v"}


@pytest.mark.parametrize("text", [
    "== Physical Plan ==\nError occurred during query planning: \n[TABLE_OR_VIEW_NOT_FOUND] x",
    "== Physical Plan ==\n*(1) Scan",  # no analyzed plan to read
])
def test_unreadable_plans_raise(text):
    with pytest.raises(PlanError):
        parse_plan_tables(text)


class PlanBackend(DuckDBBackend):
    """DuckDB that also answers plan_tables(), like Databricks."""

    def __init__(self, plan):
        super().__init__()
        self.plan = plan
        self.asked = []

    def plan_tables(self, sql):
        self.asked.append(sql)
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan


def _sentry(backend):
    backend.execute("CREATE SCHEMA ok; CREATE SCHEMA bad")
    backend.execute("CREATE TABLE ok.t AS SELECT i AS id, 'x' || i AS ssn FROM range(1, 11) t(i)")
    s = Sentry(Config.from_dict({"sandbox_schema": "sandbox", "assessment_store": False,
                                 "tables": {"ok.t": {"keys": ["id"]}}, "agent_tables": ["ok.*"]}), backend=backend)
    agent_tools.set_sentry(s)
    return s


@pytest.fixture(autouse=True)
def _reset():
    yield
    agent_tools.set_sentry(None)


def test_table_hidden_from_the_parser_is_caught_by_the_plan():
    # e.g. UPDATE ok.t SET ssn = get_ssn(), where the SQL function reads bad.secret
    b = PlanBackend(["bad.secret", "ok.t"])
    _sentry(b)
    out = agent_tools.preview_write("UPDATE {{target}} SET ssn = upper(ssn)", "ok.t")
    assert out["error"].startswith("PermissionError: Not allowed for agents: bad.secret"), out
    assert b.asked == ["UPDATE ok.t SET ssn = upper(ssn)"]  # planned against the real target, never run


def test_plan_that_agrees_lets_the_preview_run():
    b = PlanBackend(["spark_catalog.ok.t"])
    _sentry(b)
    out = agent_tools.preview_write("UPDATE {{target}} SET ssn = 'z' WHERE id = 1", "ok.t")
    assert "error" not in out and out["tables"][0]["diff"]["changed_rows"] == 1


@pytest.mark.parametrize("plan", [PlanError("Spark couldn't plan the statement"), [], ["ok.other"]])
def test_unreadable_or_odd_plans_dont_block_a_valid_preview(plan):
    # The parser and the view/function definitions are the boundary; plan formats vary on
    # serverless, so an unreadable plan is no reason to refuse (round: agent MERGE refused).
    _sentry(PlanBackend(plan))
    out = agent_tools.preview_write("UPDATE {{target}} SET ssn = 'z' WHERE id = 1", "ok.t")
    assert "error" not in out, out


def test_serverless_edge_format_names_are_read():
    edge = ("== Analyzed Logical Plan ==\nMergeIntoCommandEdge SubqueryAlias u, SubqueryAlias t, "
            "`workspace`.`s`.`allowed_t`, Delta[version=3]\n== Optimized Logical Plan ==")
    assert parse_plan_tables(edge) == ["workspace.s.allowed_t"]


def test_no_plan_check_without_agent_tables():
    b = PlanBackend(["bad.secret", "ok.t"])
    b.execute("CREATE SCHEMA ok")
    b.execute("CREATE TABLE ok.t AS SELECT 1 AS id, 'a' AS ssn")
    agent_tools.set_sentry(Sentry(Config.from_dict({"sandbox_schema": "sandbox", "assessment_store": False,
                                                    "tables": {"ok.t": {"keys": ["id"]}}}), backend=b))
    assert "error" not in agent_tools.preview_write("UPDATE {{target}} SET ssn = 'z'", "ok.t")
    assert b.asked == []


def test_databricks_runs_explain_extended():
    b = DatabricksBackend(warehouse_id="w", client=object())
    seen = []
    b.query = lambda sql: seen.append(sql) or [{"plan": PLAN}]
    assert b.plan_tables("UPDATE main.ok.t SET x = 1") == ["main.bad.secret", "main.ok.t"]
    assert seen == ["EXPLAIN EXTENDED UPDATE main.ok.t SET x = 1"]


# --- pinning: fewer metadata queries ---------------------------------------------------
class Counting(DuckDBBackend):
    def __init__(self):
        super().__init__()
        self.version_reads = 0
        self.sql = []

    def current_version(self, table):
        self.version_reads += 1
        return 3

    def query(self, sql):
        self.sql.append(sql)
        return super().query(sql)


def test_pinning_needs_three_version_reads_and_no_probe_query():
    b = Counting()
    b.execute("CREATE SCHEMA c")
    b.execute("CREATE TABLE c.base AS SELECT i AS id, i AS v FROM range(1, 101) t(i)")
    b.execute("CREATE TABLE c.cand AS SELECT * FROM c.base")
    b.sql.clear()
    d = diff_tables(b, "c.base", "c.cand", ["id"])
    assert d.is_identical and d.pinned_versions == {"base": 3, "candidate": 3}
    assert b.version_reads == 3  # both at the start, then only the candidate after
    assert not any("SELECT 1 AS ok" in q for q in b.sql)


# --- views and SQL functions: their definitions are held to agent_tables too ------------
def _vf(backend):
    backend.execute("CREATE SCHEMA ok; CREATE SCHEMA bad")
    backend.execute("CREATE TABLE ok.t AS SELECT i AS id, 'x' || i AS ssn FROM range(1, 11) t(i)")
    backend.execute("CREATE TABLE ok.u AS SELECT i AS id, 'u' || i AS ssn FROM range(1, 11) t(i)")
    backend.execute("CREATE TABLE bad.secret AS SELECT i AS id, 'SECRET-' || i AS ssn FROM range(1, 11) t(i)")
    backend.execute("CREATE VIEW ok.v_bad AS SELECT id, ssn FROM bad.secret")
    backend.execute("CREATE VIEW ok.v_good AS SELECT id, ssn FROM ok.u")
    backend.execute("CREATE VIEW ok.v_nested AS SELECT * FROM ok.v_bad")
    backend.execute("CREATE MACRO ok.get_secret() AS (SELECT max(ssn) FROM bad.secret)")
    backend.execute("CREATE MACRO ok.get_good() AS (SELECT max(ssn) FROM ok.u)")
    s = Sentry(Config.from_dict({"sandbox_schema": "sandbox", "assessment_store": False,
                                 "tables": {"ok.t": {"keys": ["id"]}, "ok.v_bad": {"keys": ["id"]}},
                                 "agent_tables": ["ok.*"]}), backend=backend)
    agent_tools.set_sentry(s)
    return s


@pytest.mark.parametrize("sql", [
    "UPDATE {{target}} SET ssn = (SELECT max(ssn) FROM ok.v_bad)",
    "UPDATE {{target}} SET ssn = (SELECT max(ssn) FROM ok.v_nested)",  # a view over a view
    "UPDATE {{target}} SET ssn = ok.get_secret()",
    "UPDATE {{target}} SET ssn = get_secret()",  # unqualified: matched in any schema
])
def test_views_and_functions_over_forbidden_tables_are_refused(backend, sql):
    _vf(backend)
    out = agent_tools.preview_write(sql, "ok.t")
    assert out.get("error", "").startswith("PermissionError: Not allowed for agents: bad.secret"), out


@pytest.mark.parametrize("sql", [
    "UPDATE {{target}} SET ssn = (SELECT max(ssn) FROM ok.v_good) WHERE id = 1",
    "UPDATE {{target}} SET ssn = ok.get_good() WHERE id = 1",
    "UPDATE {{target}} SET ssn = upper(ssn) WHERE id = 1",  # built-ins aren't looked up
])
def test_views_and_functions_over_listed_tables_still_work(backend, sql):
    _vf(backend)
    out = agent_tools.preview_write(sql, "ok.t")
    assert "error" not in out and out["tables"][0]["diff"]["changed_rows"] == 1, out


def test_listed_view_as_candidate_over_forbidden_table_is_refused(backend):
    _vf(backend)
    out = agent_tools.assess_change(["ok.t"], candidates={"ok.t": "ok.v_bad"})
    assert out["error"].startswith("PermissionError: Not allowed for agents: bad.secret")


class Unreadable(DuckDBBackend):
    def view_definitions(self, tables):
        raise RuntimeError("no access to system.information_schema")


def test_catalog_that_cant_be_read_fails_closed():
    b = Unreadable()
    _vf(b)
    out = agent_tools.preview_write("UPDATE {{target}} SET ssn = 'z'", "ok.t")
    assert "Couldn't read view and function definitions" in out.get("error", ""), out


def test_databricks_reads_definitions_from_system_information_schema():
    b = DatabricksBackend(warehouse_id="w", client=object())
    seen = []

    def q(sql):
        seen.append(sql)
        if "information_schema.views" in sql:
            return [{"table_catalog": "main", "table_schema": "ok", "table_name": "v", "view_definition": "SELECT 1"}]
        return [{"routine_catalog": "main", "routine_schema": "ok", "routine_name": "f", "routine_body": "SQL",
                 "routine_definition": "(SELECT 1)"}]

    b.query = q
    assert b.view_definitions(["main.ok.v", "ok.t"]) == {"main.ok.v": ("SELECT 1", "main", "ok")}
    assert b.function_bodies(["f"]) == [("main.ok.f", "(SELECT 1)", "SQL", "main", "ok")]
    assert "system.information_schema.views" in seen[0] and "current_catalog()" in seen[0]
    assert "lower(routine_name) = 'f'" in seen[1]
