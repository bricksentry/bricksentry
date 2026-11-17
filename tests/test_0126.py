"""0.1.26: fixes for what Genie's worst-case rounds found."""

from __future__ import annotations

import warnings

import pytest

from bricksentry.config import Config
from bricksentry.diff import diff_tables
from bricksentry.history import Change, newer_table_changes, rollback_warning
from bricksentry.models import ColumnChange, SchemaChange, TableDiff
from bricksentry.policy import Policy, PolicyError


# --- (b) one changed copy no longer shifts how the other copies pair ---------------
def test_null_keys_with_two_changes_report_two(backend):
    backend.execute("CREATE SCHEMA p")
    backend.execute("CREATE TABLE p.base AS SELECT CASE WHEN i <= 2000 THEN NULL ELSE i END AS id, i AS v "
                    "FROM range(1, 3001) t(i)")
    # Change two rows that share the NULL key, plus nothing else.
    backend.execute("CREATE TABLE p.cand AS SELECT id, CASE WHEN i IN (10, 1500) THEN v + 1000000 ELSE v END AS v "
                    "FROM (SELECT *, v AS i FROM p.base)")
    d = diff_tables(backend, "p.base", "p.cand", ["id"])
    assert (d.added_rows, d.removed_rows, d.changed_rows) == (0, 0, 2)
    assert d.columns[0].changed_rows == 2


def test_duplicate_copies_identical_still_match(backend):
    backend.execute("CREATE SCHEMA r")
    backend.execute("CREATE TABLE r.base AS SELECT i % 7 AS id, i AS v FROM range(1, 701) t(i)")
    backend.execute("CREATE TABLE r.cand AS SELECT * FROM r.base")
    d = diff_tables(backend, "r.base", "r.cand", ["id"])
    assert (d.added_rows, d.removed_rows, d.changed_rows) == (0, 0, 0)


def test_extra_and_missing_copies_with_a_change(backend):
    backend.execute("CREATE SCHEMA s")
    backend.execute("CREATE TABLE s.base AS SELECT 1 AS id, i AS v FROM range(1, 6) t(i)")  # v 1..5
    # v=1 dropped, v=3 changed to 30, v=9 and v=10 new: 2 leftovers vs 3 leftovers.
    backend.execute("CREATE TABLE s.cand AS SELECT CAST(id AS BIGINT) AS id, CAST(v AS BIGINT) AS v FROM "
                    "(VALUES (1, 2), (1, 30), (1, 4), (1, 5), (1, 9), (1, 10)) t(id, v)")
    d = diff_tables(backend, "s.base", "s.cand", ["id"])
    assert d.changed_rows == 2 and d.added_rows == 1 and d.removed_rows == 0
    assert d.base_rows == 5 and d.candidate_rows == 6


# --- (c) an overflowing exact total doesn't kill the diff -------------------------
def test_decimal_overflow_falls_back_to_approximate_totals(backend):
    backend.execute("CREATE SCHEMA o")
    backend.execute("CREATE TABLE o.base AS SELECT i AS id, CAST(9.9e37 AS DECIMAL(38, 0)) AS big "
                    "FROM range(1, 11) t(i)")
    backend.execute("CREATE TABLE o.cand AS SELECT * FROM o.base WHERE id > 1")
    d = diff_tables(backend, "o.base", "o.cand", ["id"])
    assert d.removed_rows == 1
    assert any("approximate" in n or "could not be computed" in n for n in d.notes)


# --- (a) a total moving away from zero fires the total rule ----------------------
def _diff_with(col: ColumnChange) -> TableDiff:
    return TableDiff(table="t", base="b", candidate="c", keys=["id"], base_rows=10, candidate_rows=10,
                     added_rows=0, removed_rows=0, changed_rows=5, unchanged_rows=5, schema=SchemaChange(), columns=[col])


def test_total_moving_from_zero_fires():
    p = Policy({"rules": [{"name": "totals", "when": {"sum_change_pct_gt": 1}, "then": "block"}]})
    col = ColumnChange(column="amount", changed_rows=5, base_nulls=0, candidate_nulls=0, base_sum=0.0,
                       candidate_sum=5.0)
    assert col.sum_moved_from_zero
    verdict, hits = p.evaluate("t", _diff_with(col), [], {})
    assert verdict.value == "block" and "0 -> 5" in hits[0].reason
    still = ColumnChange(column="amount", changed_rows=0, base_nulls=0, candidate_nulls=0, base_sum=0.0,
                         candidate_sum=0.0)
    assert p.evaluate("t", _diff_with(still), [], {})[0].value == "pass"


# --- (d) metadata changes show in the rollback warning ---------------------------
def test_metadata_changes_after_the_version_are_listed():
    hist = [{"version": 6, "operation": "SET TBLPROPERTIES"}, {"version": 5, "operation": "OPTIMIZE"},
            {"version": 4, "operation": "RENAME COLUMN"}, {"version": 3, "operation": "DELETE"}]
    other = newer_table_changes(hist, 3)
    assert [h["version"] for h in other] == [4, 6]
    w = rollback_warning(Change("t", 3, 2, "DELETE", "", [3], newer_other=other))
    assert w.startswith("Newer table changes exist: v6 (SET TBLPROPERTIES), v4 (RENAME COLUMN).")
    both = rollback_warning(Change("t", 3, 2, "DELETE", "", [3],
                                   newer=[{"version": 7, "operation": "WRITE", "timestamp": ""}], newer_other=other))
    assert "It also undoes these table changes: v6 (SET TBLPROPERTIES), v4 (RENAME COLUMN)." in both


# --- (e)(f)(g) policy mistakes give clear messages -------------------------------
def test_typo_in_a_verdict_names_the_choices():
    with pytest.raises(PolicyError, match="'blok' is not a verdict. Use one of: pass, warn, needs_human, block"):
        Policy({"rules": [{"name": "x", "when": {"removed_pct_gt": 1}, "then": "blok"}]})
    p = Policy({"rules": [{"name": "x", "when": {"removed_pct_gt": 1}, "then": "BLOCK"}]})
    assert p.rules  # any case is accepted


def test_template_name_without_builtin_prefix_says_how(tmp_path):
    with pytest.raises(FileNotFoundError, match="builtin:strict"):
        Config.from_dict({"policy": "strict"}, root=tmp_path)


def test_empty_policy_warns():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        Policy({"rules": []})
    assert any("every change passes" in str(x.message) for x in w)


def test_non_mapping_policy_is_a_policy_error():
    with pytest.raises(PolicyError, match="mapping"):
        Policy(["rules"])  # type: ignore[arg-type]


# --- (h) preview_write shows one clear line when the statement fails -------------
def test_preview_write_failure_is_one_line(sentry, backend):
    backend.execute("CREATE SCHEMA IF NOT EXISTS sales")
    backend.execute("CREATE TABLE IF NOT EXISTS sales.pw AS SELECT 1 AS id, 2 AS v")
    with pytest.raises(RuntimeError) as e:
        sentry.preview_write("UPDATE sales.pw SET nope = 1", "sales.pw", keys=["id"])
    msg = str(e.value)
    assert msg.startswith("The statement failed on the preview clone, so production was not touched:")
    assert "\n" not in msg


# --- (i) pii / certified gates ignore no-op changes, but nothing else ------------
def test_noop_change_to_certified_feeder_passes_and_a_real_one_does_not(sentry, backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    assert sentry.assess(["sales.orders"]).verdict.value == "pass"
    backend.execute("DROP TABLE sales_ci.orders")
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    backend.execute("UPDATE sales_ci.orders SET amount = amount + 1 WHERE order_id = (SELECT MIN(order_id) FROM sales_ci.orders)")
    a = sentry.assess(["sales.orders"])
    hit = next(h for h in a.hits if h.rule == "certified-downstream")
    assert a.verdict.value == "needs_human" and "tagged certified" in hit.reason


def test_pii_gate_names_the_tag_and_fires_on_schema_only_and_missing_diff():
    p = Policy()
    no_rows = TableDiff(table="t", base="b", candidate="c", keys=["id"], base_rows=1, candidate_rows=1,
                        added_rows=0, removed_rows=0, changed_rows=0, unchanged_rows=1, schema=SchemaChange())
    assert p.evaluate("t", no_rows, [], {"pii": "true"})[0].value == "pass"
    no_rows.schema = SchemaChange(removed={"email": "string"})
    v, hits = p.evaluate("t", no_rows, [], {"pii": "true"})
    assert v.value == "needs_human" and any(h.rule == "pii-table" and "table is tagged pii" in h.reason for h in hits)
    # No diff at all can't prove nothing changed: the gate still fires.
    custom = Policy({"rules": [{"name": "pii", "when": {"tag": "pii", "any_change": True}, "then": "needs_human"}]})
    v, hits = custom.evaluate("t", None, [], {"pii": "true"}, diff_error="boom")
    assert v.value == "needs_human" and "can't be ruled out" in hits[0].reason


# --- found in the 0.1.26 pre-release check: mistyped names in agent tools --------
def test_agent_tools_report_a_mistyped_table(sentry):
    from bricksentry import agent_tools

    agent_tools.set_sentry(sentry)
    try:
        for out in (agent_tools.whats_downstream("sales.orderz"), agent_tools.rollback_plan("sales.orderz")):
            assert out["error"].startswith("LookupError: Table sales.orderz not found.")
        assert agent_tools.whats_downstream("sales.orders")["count"] == 3  # real tables unchanged
        assert "error" not in agent_tools.rollback_plan("sales.orders")
    finally:
        agent_tools.set_sentry(None)


def test_misspelled_backend_key_is_rejected():
    from bricksentry.backends.base import make_backend

    with pytest.raises(ValueError, match="Unknown key\\(s\\) under backend: path for type duckdb. Valid keys: type, database"):
        make_backend({"type": "duckdb", "path": "x.db"})
    with pytest.raises(ValueError, match="warehouse"):
        make_backend({"type": "databricks", "warehouse": "abc"})
    assert make_backend({"type": "duckdb", "database": ":memory:"}).name == "duckdb"
