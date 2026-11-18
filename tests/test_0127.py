"""0.1.27: fixes for live-test round 7 (#6 value leak to agents, #9 unpinned reads,
#8 MAP columns, #7 float noise, key casing)."""

from __future__ import annotations

import json

import pytest

from bricksentry import Config, Sentry, agent_tools
from bricksentry.backends.databricks_backend import DatabricksBackend
from bricksentry.backends.duckdb_backend import DuckDBBackend
from bricksentry.diff import diff_tables
from bricksentry.report import to_markdown

SSN = "SENTINEL-123-45-6789"
SALARY = 987654.32


def _hr_sentry(backend: DuckDBBackend, **extra) -> Sentry:
    backend.execute("CREATE SCHEMA IF NOT EXISTS hr")
    backend.execute(
        "CREATE OR REPLACE TABLE hr.emp AS SELECT i AS id, CASE WHEN i = 7 THEN '" + SSN + "' ELSE 'x' || i END AS ssn, "
        f"CAST(CASE WHEN i = 7 THEN {SALARY} ELSE 1000.0 END AS DOUBLE) AS salary, 'eng' AS dept FROM range(1, 11) t(i)"
    )
    backend.execute("CREATE OR REPLACE TABLE hr.one AS SELECT 1 AS id, CAST(" + str(SALARY) + " AS DOUBLE) AS salary")
    backend.execute("CREATE OR REPLACE TABLE hr.small AS SELECT 1 AS id, CAST(" + str(SALARY) + " AS DECIMAL(8, 2)) AS salary")
    cfg = Config.from_dict({
        "sandbox_schema": "sandbox",
        "tables": {"hr.emp": {"keys": ["id"], "segment_by": ["ssn"]}, "hr.one": {"keys": ["id"]}},
        "lineage": {"type": "static", "tags": {"hr.emp": {"pii": "true"}}},
        **extra,
    })
    s = Sentry(cfg, backend=backend)
    agent_tools.set_sentry(s)
    return s


@pytest.fixture
def hr(backend):
    s = _hr_sentry(backend)
    yield s
    agent_tools.set_sentry(None)


def _leaks(out) -> list[str]:
    text = json.dumps(out, default=str)
    return [v for v in (SSN, "987654", "987,654", "1975308", "1,975,308") if v in text]


# --- #6: agent tools never return production values --------------------------------
def test_preview_write_returns_no_values_to_agents(hr):
    out = agent_tools.preview_write("UPDATE {{target}} SET ssn = 'redacted', salary = salary * 2 WHERE id IN (7, 3)",
                                    "hr.emp")
    assert "error" not in out, out
    assert out["tables"][0]["diff"]["changed_rows"] == 2  # counts still there
    assert _leaks(out) == []
    for tool_out in (agent_tools.explain_diff(assessment_id=out["id"]), agent_tools.explain_diff(table="hr.emp"),
                     agent_tools.safe_to_ship(out["id"])):
        assert _leaks(tool_out) == [], tool_out
    # Humans (CLI / Python API) still get the samples.
    human = hr.preview_write("UPDATE {{target}} SET salary = salary * 2 WHERE id = 7", "hr.emp")
    assert SSN in json.dumps(human) or "987654" in json.dumps(human)


def test_agent_copies_own_value_trick_and_totals_dont_leak(hr):
    # "SET note = ssn" style: the new value IS the production value, so both sides are hidden.
    out = agent_tools.preview_write("UPDATE {{target}} SET dept = ssn WHERE id = 7", "hr.emp")
    assert _leaks(out) == []
    one = agent_tools.preview_write("UPDATE {{target}} SET salary = salary * 2", "hr.one")
    assert _leaks(one) == []
    col = one["tables"][0]["diff"]["columns"][0]
    assert "base_sum" not in col and "candidate_sum" not in col and col["sum_change_pct"] == 100.0


def test_engine_errors_lose_quoted_values(hr):
    out = agent_tools.preview_write("UPDATE {{target}} SET id = CAST(ssn AS INTEGER) WHERE id = 7", "hr.emp")
    assert "error" in out and SSN not in out["error"]
    # DuckDB quotes values with double quotes: "Casting value "1975308.64" ... out of range"
    out = agent_tools.preview_write("UPDATE {{target}} SET salary = salary * 2", "hr.small", keys=["id"])
    assert "error" in out and _leaks(out) == [], out


def test_agent_tables_allowlist_refuses_and_audits(backend, tmp_path):
    s = _hr_sentry(backend, agent_tables=["hr.one"])
    try:
        out = agent_tools.preview_write("UPDATE {{target}} SET salary = 0", "hr.emp")
        assert out["error"].startswith("PermissionError: Not allowed for agents: hr.emp")
        assert "error" not in agent_tools.preview_write("UPDATE {{target}} SET salary = 0", "hr.one")
        assert "error" in agent_tools.assess_change(["hr.emp"], candidates={"hr.emp": "hr.one"})
        lines = [json.loads(x) for x in (s.config.store_dir() / "audit.jsonl").read_text().splitlines()]
        assert [(x["tool"], x["tables"], bool(x["refused"])) for x in lines] == [
            ("preview_write", ["hr.emp"], True), ("preview_write", ["hr.one"], False),
            ("assess_change", ["hr.emp", "hr.one"], True)]
        assert lines[1]["rows"]["hr.one"]["changed_rows"] == 1
    finally:
        agent_tools.set_sentry(None)


def test_bad_agent_tables_config():
    with pytest.raises(ValueError, match="agent_tables must be a list"):
        Config.from_dict({"agent_tables": {"a": 1}})


def test_reports_hide_values_of_pii_tables(hr):
    hr.preview_write("UPDATE {{target}} SET ssn = 'new' WHERE id = 7", "hr.emp")
    a = hr.get_assessment(hr._assessments and list(hr._assessments)[-1])
    md = to_markdown(a)
    assert SSN not in md and "value hidden (tagged pii)" in md


# --- #9: every query reads the version pinned at the start --------------------------
class Versioned(DuckDBBackend):
    """DuckDB with fake Delta versions: version n of table t is the table t__v<n>."""

    def __init__(self):
        super().__init__()
        self.versions: dict[str, int] = {}
        self.on_query = None
        self.queries = 0

    def current_version(self, table):
        return self.versions.get(table)

    def at_version(self, table, version):
        return self.qualified(f"{table}__v{version}") if version is not None else self.qualified(table)

    def columns(self, table):  # the current version, like DESCRIBE TABLE
        v = self.versions.get(table)
        return super().columns(f"{table}__v{v}" if v is not None else table)

    def columns_at(self, table, version, strict=False):
        return super().columns(f"{table}__v{version}") if version is not None else self.columns(table)

    def query(self, sql):
        self.queries += 1
        if self.on_query:
            self.on_query(self.queries)
        return super().query(sql)

    def write(self, table, select):
        v = self.versions.get(table, -1) + 1
        self.execute(f"CREATE OR REPLACE TABLE {table}__v{v} AS {select}")
        self.versions[table] = v


def test_candidate_replaced_mid_diff_gives_pinned_result_and_needs_human():
    b = Versioned()
    b.execute("CREATE SCHEMA p")
    b.write("p.base", "SELECT i AS id, i AS v FROM range(1, 3001) t(i)")
    b.write("p.cand", "SELECT id, CASE WHEN id <= 5 THEN v + 1 ELSE v END AS v FROM p.base__v0")  # 5 changed
    big = "SELECT id, CASE WHEN id <= 2000 THEN v + 1 ELSE v END AS v FROM p.base__v0"  # 2,000 changed

    def replace_once(n):
        if n == 3 and b.versions["p.cand"] == 0:
            b.write("p.cand", big)

    b.queries = 0
    b.on_query = replace_once
    d = diff_tables(b, "p.base", "p.cand", ["id"])
    assert d.changed_rows == 5 and d.unchanged_rows == 2995  # one consistent state: the pinned one
    assert d.moved_during_check == ["candidate"] and d.pinned_versions == {"base": 0, "candidate": 0}
    assert any("changed while it was being checked" in n for n in d.notes)

    from bricksentry.policy import Policy

    v, hits = Policy().evaluate("p.base", d, [], {})
    assert v.value == "needs_human" and "changed-during-check" in {h.rule for h in hits}


def test_base_moving_mid_diff_cant_change_a_pinned_result():
    b = Versioned()
    b.execute("CREATE SCHEMA p")
    b.write("p.base", "SELECT i AS id, i AS v FROM range(1, 101) t(i)")
    b.write("p.cand", "SELECT * FROM p.base__v0")
    b.queries = 0
    b.on_query = lambda n: b.write("p.base", "SELECT 1 AS id, 1 AS v") if n == 2 else None
    d = diff_tables(b, "p.base", "p.cand", ["id"])
    assert d.is_identical is True and d.moved_during_check == []  # pinned: the move can't affect the result


def test_databricks_reads_at_version():
    b = DatabricksBackend(warehouse_id="w", client=object())
    assert b.at_version("c.s.t", 12) == "`c`.`s`.`t` VERSION AS OF 12"
    seen = []
    b.query = lambda sql: seen.append(sql) or [{"col_name": "id", "data_type": "INT"}]
    assert b.columns_at("c.s.t", 12) == {"id": "int"} and seen == ["DESCRIBE QUERY SELECT * FROM `c`.`s`.`t` VERSION AS OF 12"]
    assert b.at_version("c.s.t", None) == "`c`.`s`.`t`"


# --- #8: MAP columns ----------------------------------------------------------------
def test_databricks_compares_maps_by_sorted_entries():
    b = DatabricksBackend(warehouse_id="w", client=object())
    assert b.canon("b.`m`", "map<string,int>") == "to_json(array_sort(map_entries(b.`m`)))"
    assert b.canon("b.`s`", "struct<a:map<string,int>>") == "to_json(b.`s`)"
    assert b.canon("b.`x`", "int") == "b.`x`"
    assert "to_json(array_sort(map_entries(`m`)))" in b.row_hash(["id", "m"], {"m": "map<string,int>"})


class NoMaps(DuckDBBackend):
    """A backend that, like some engines, can't compare one column type."""

    def canon(self, expr, type_name):
        return f"no_such_function({expr})" if type_name.startswith("map") else expr


def test_uncomparable_column_is_skipped_not_fatal():
    b = NoMaps()
    b.execute("CREATE SCHEMA m")
    b.execute("CREATE TABLE m.base AS SELECT i AS id, i AS v, MAP {'k': i} AS mp FROM range(1, 51) t(i)")
    b.execute("CREATE TABLE m.cand AS SELECT id, CASE WHEN id = 3 THEN 0 ELSE v END AS v, mp FROM m.base")
    d = diff_tables(b, "m.base", "m.cand", ["id"])
    assert d.error is None and d.changed_rows == 1 and list(d.not_compared) == ["mp"]
    assert any("Not compared" in n for n in d.notes)
    from bricksentry.policy import Policy

    v, hits = Policy().evaluate("m.base", d, [], {})
    assert v.value == "needs_human" and "columns-not-compared" in {h.rule for h in hits}


def test_maps_written_in_a_different_key_order_are_equal(backend):
    backend.execute("CREATE SCHEMA mo")
    backend.execute("CREATE TABLE mo.base AS SELECT i AS id, map(['a', 'b'], [1, i]) AS mp FROM range(1, 6) t(i)")
    backend.execute("CREATE TABLE mo.cand AS SELECT i AS id, CASE WHEN i = 2 THEN map(['b', 'a'], [9, 1]) "
                    "ELSE map(['b', 'a'], [i, 1]) END AS mp FROM range(1, 6) t(i)")
    d = diff_tables(backend, "mo.base", "mo.cand", ["id"])
    assert d.changed_rows == 1 and d.not_compared == {}


def test_duckdb_maps_with_equal_content_compare_equal(backend):
    backend.execute("CREATE SCHEMA mm")
    backend.execute("CREATE TABLE mm.base AS SELECT i AS id, MAP {'a': 1, 'b': 2} AS mp FROM range(1, 4) t(i)")
    backend.execute("CREATE TABLE mm.cand AS SELECT i AS id, MAP {'a': 1, 'b': 2} AS mp FROM range(1, 4) t(i)")
    assert diff_tables(backend, "mm.base", "mm.cand", ["id"]).is_identical


# --- #7: floats -------------------------------------------------------------------------
def _floats(backend):
    backend.execute("CREATE SCHEMA f")
    backend.execute("CREATE TABLE f.base AS SELECT i AS id, 0.3::DOUBLE AS x FROM range(1, 51) t(i)")
    backend.execute("CREATE TABLE f.cand AS SELECT i AS id, (0.1::DOUBLE + 0.2::DOUBLE) AS x FROM range(1, 51) t(i)")
    backend.execute("CREATE TABLE f.odd AS SELECT * FROM (VALUES (1, 'NaN'::DOUBLE), (2, 'inf'::DOUBLE), "
                    "(3, NULL), (4, 0.3::DOUBLE)) t(id, x)")
    backend.execute("CREATE TABLE f.odd2 AS SELECT * FROM (VALUES (1, 1.0::DOUBLE), (2, 1.0::DOUBLE), "
                    "(3, 0.3::DOUBLE), (4, 0.30000000000000004::DOUBLE)) t(id, x)")


def test_floats_exact_by_default_with_a_hint(backend):
    _floats(backend)
    d = diff_tables(backend, "f.base", "f.cand", ["id"])
    assert d.changed_rows == 50 and any("float_tolerance" in n for n in d.notes)


def test_float_tolerance_is_opt_in_and_keeps_real_changes(backend):
    _floats(backend)
    assert diff_tables(backend, "f.base", "f.cand", ["id"], float_tolerance=1e-9).changed_rows == 0
    d = diff_tables(backend, "f.odd", "f.odd2", ["id"], float_tolerance=1e-9)
    assert d.changed_rows == 3  # NaN -> 1, inf -> 1, NULL -> 0.3 still change; 0.3 ~ 0.30000000000000004
    with pytest.raises(ValueError, match="float_tolerance"):
        Config.from_dict({"float_tolerance": 2})


# --- key casing ----------------------------------------------------------------------------
def test_keys_match_case_insensitively(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["ORDER_ID"])
    assert d.keys == ["order_id"] and d.is_identical
