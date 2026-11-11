"""0.1.12: column-level signals (changed columns, column tags, column lineage) and `python -m bricksentry`."""

from __future__ import annotations

import subprocess
import sys

import pytest

from bricksentry import Config, Sentry, agent_tools
from bricksentry.assess import changed_columns
from bricksentry.lineage import StaticLineage, UnityCatalogLineage
from bricksentry.models import Assessment, ColumnChange, SchemaChange, TableDiff
from bricksentry.policy import ColumnContext, Policy
from bricksentry.policy_templates import TEMPLATES
from bricksentry.report import to_markdown


# --- which columns changed ---------------------------------------------------------
def test_changed_columns_covers_values_nulls_drops_and_retypes():
    d = TableDiff(
        table="t", base="b", candidate="c", keys=["id"], base_rows=10, candidate_rows=10, added_rows=0,
        removed_rows=0, changed_rows=2, unchanged_rows=8,
        schema=SchemaChange(removed={"fax": "string"}, type_changed={"zip": ("int", "string")}),
        columns=[
            ColumnChange("email", changed_rows=2),
            ColumnChange("phone", changed_rows=0, base_nulls=0, candidate_nulls=3),
            ColumnChange("name", changed_rows=0, base_nulls=1, candidate_nulls=1),
        ],
    )
    assert changed_columns(d) == ["email", "phone", "fax", "zip"]
    assert changed_columns(None) == []


# --- static lineage ----------------------------------------------------------------
def test_static_column_lineage_follows_two_hops_with_tags():
    lin = StaticLineage(
        column_edges={
            "main.crm.contacts.email": ["main.mart.customers.email_hash"],
            "main.mart.customers.email_hash": ["main.gold.kpis.contactable"],
        },
        column_tags={"main.mart.customers": {"email_hash": {"pii": ""}}, "main.gold.kpis": {"contactable": {"certified": "true"}}},
    )
    out = lin.column_downstream("main.crm.contacts", ["email"], depth=2)
    assert [(c.table, c.column, c.depth, c.source_column) for c in out] == [
        ("main.mart.customers", "email_hash", 1, "email"),
        ("main.gold.kpis", "contactable", 2, "email"),
    ]
    assert out[0].tags == {"pii": ""} and out[1].tags == {"certified": "true"}
    assert lin.column_downstream("main.crm.contacts", ["email"], depth=1)[-1].depth == 1


# --- policy conditions -------------------------------------------------------------
def _ctx():
    from bricksentry.models import ColumnImpact

    return ColumnContext(
        changed=["email", "amount"],
        tags={"email": {"pii": ""}, "amount": {"class": "finance"}},
        downstream=[ColumnImpact("amount", "main.gold.rev", "total", 1, {"certified": "true"})],
    )


@pytest.mark.parametrize("when,hit", [
    ({"changed_column_tag": "pii"}, True),
    ({"changed_column_tag": {"class": "finance"}}, True),
    ({"changed_column_tag": "certified"}, False),
    ({"downstream_column_tag": "certified"}, True),
    ({"downstream_column_tag": "pii"}, False),
    ({"changed_column": "ema*"}, True),
    ({"changed_column": ["fax", "zip"]}, False),
])
def test_column_conditions(when, hit):
    v, hits = Policy({"rules": [{"name": "r", "when": when, "then": "needs_human"}]}).evaluate(
        "main.s.t", None, [], {}, columns=_ctx())
    assert (v.value == "needs_human") is hit
    if hit:
        assert hits[0].reason


def test_column_reasons_name_the_columns():
    _, hits = Policy({"rules": [
        {"name": "a", "when": {"changed_column_tag": "pii"}, "then": "warn"},
        {"name": "b", "when": {"downstream_column_tag": "certified"}, "then": "warn"},
    ]}).evaluate("main.s.t", None, [], {}, columns=_ctx())
    reasons = {h.rule: h.reason for h in hits}
    assert reasons["a"] == "changed column(s) tagged pii: email"
    assert reasons["b"] == "1 downstream column(s) tagged certified: main.gold.rev.total (from amount)"


def test_templates_use_column_signals():
    conds = {n: [r["when"] for r in t["policy"]["rules"]] for n, t in TEMPLATES.items()}
    for name in ("default", "strict", "agent-safe", "pii", "finance"):
        assert {"changed_column_tag": "pii"} in conds[name], name
    for name in ("strict", "agent-safe", "certified"):
        assert {"downstream_column_tag": "certified"} in conds[name], name


# --- end to end with DuckDB and static lineage --------------------------------------
@pytest.fixture
def crm(backend):
    backend.execute("CREATE SCHEMA crm; CREATE SCHEMA crm_ci")
    backend.execute("""CREATE TABLE crm.contacts AS SELECT i AS id, 'u' || i || '@x.com' AS email,
                       (i * 10.0)::DOUBLE AS amount, 'n' || i AS name FROM range(1, 201) t(i)""")
    backend.execute("""CREATE TABLE crm_ci.contacts AS SELECT id,
                       CASE WHEN id <= 5 THEN 'changed' || id || '@y.com' ELSE email END AS email,
                       amount, name FROM crm.contacts""")
    return backend


def _sentry(backend, policy="builtin:default", column_depth=2):
    return Sentry(Config.from_dict({
        "assessment_store": False,
        "candidate_pattern": "{schema}_ci.{table}",
        "tables": {"crm.contacts": {"keys": ["id"]}},
        "policy": policy,
        "lineage": {
            "type": "static", "column_depth": column_depth,
            "column_edges": {"crm.contacts.email": ["mart.customers.email_hash"],
                             "mart.customers.email_hash": ["gold.kpis.reachable"]},
            "column_tags": {"crm.contacts": {"email": {"pii": ""}},
                            "gold.kpis": {"reachable": {"certified": "true"}}},
        },
    }), backend=backend)


def test_pii_column_change_needs_a_human(crm):
    a = _sentry(crm).assess(["crm.contacts"])
    t = a.tables[0]
    assert t.changed_columns == ["email"]
    assert t.column_tags == {"email": {"pii": ""}}
    assert [(c.table, c.column) for c in t.column_downstream] == [("mart.customers", "email_hash"), ("gold.kpis", "reachable")]
    assert a.verdict.value == "needs_human"
    assert any(h.rule == "pii-column-changed" and "email" in h.reason for h in a.hits)
    assert "Tagged columns changed: email (pii)" in t.summary
    assert "feed 2 downstream column(s)" in t.summary


def test_strict_flags_the_certified_column_downstream(crm):
    a = _sentry(crm, "builtin:strict").assess(["crm.contacts"])
    hit = next(h for h in a.hits if h.rule == "certified-column-feed")
    assert "gold.kpis.reachable (from email)" in hit.reason


def test_report_and_saved_assessment_keep_column_impact(crm):
    a = _sentry(crm).assess(["crm.contacts"])
    md = to_markdown(a)
    assert "Column impact: 1 changed column(s), 2 downstream column(s)" in md
    assert "| `email` | `gold.kpis.reachable` | 2 | certified=true |" in md
    back = Assessment.from_dict(a.to_dict()).tables[0]
    assert back.column_downstream[1].tags == {"certified": "true"} and back.changed_columns == ["email"]


def test_untouched_columns_are_not_followed(crm):
    crm.execute("CREATE OR REPLACE TABLE crm_ci.contacts AS SELECT * FROM crm.contacts")
    crm.execute("UPDATE crm_ci.contacts SET name = 'x' WHERE id = 1")
    a = _sentry(crm).assess(["crm.contacts"])
    t = a.tables[0]
    assert t.changed_columns == ["name"] and t.column_downstream == [] and t.column_tags == {}
    assert all(h.rule != "pii-column-changed" for h in a.hits)


def test_column_depth_zero_turns_column_lineage_off(crm):
    a = _sentry(crm, column_depth=0).assess(["crm.contacts"])
    assert a.tables[0].column_downstream == [] and a.tables[0].column_tags == {"email": {"pii": ""}}


def test_column_lookup_failures_degrade_with_a_note(crm, monkeypatch):
    s = _sentry(crm)

    def boom(*a, **k):
        raise PermissionError("PERMISSION_DENIED: no SELECT on system.access.column_lineage\nstack")

    monkeypatch.setattr(s.lineage, "column_downstream", boom)
    monkeypatch.setattr(s.lineage, "column_tags", boom)
    a = s.assess(["crm.contacts"])
    t = a.tables[0]
    assert t.diff is not None and t.changed_columns == ["email"] and t.column_downstream == []
    assert "Column tags unavailable (PERMISSION_DENIED" in t.summary
    assert "column lineage unavailable (PERMISSION_DENIED" in t.summary
    assert "\n" not in t.summary


def test_whats_downstream_with_columns(crm):
    agent_tools.set_sentry(_sentry(crm))
    try:
        out = agent_tools.whats_downstream("crm.contacts", columns=["email", "name"])
        assert out["column_tags"] == {"email": {"pii": ""}}
        assert [c["column"] for c in out["column_downstream"]] == ["email_hash", "reachable"]
        assert "column_downstream" not in agent_tools.whats_downstream("crm.contacts")
    finally:
        agent_tools.set_sentry(None)


# --- Unity Catalog column lineage (system tables emulated in DuckDB) ----------------
@pytest.fixture
def uc(backend):
    backend.execute("CREATE SCHEMA sysc")
    backend.execute("""CREATE TABLE sysc.column_lineage AS SELECT * FROM (VALUES
      ('main.crm.contacts', 'email', 'main.mart.customers', 'email_hash', current_date),
      ('main.crm.contacts', 'email', 'main.mart.customers', 'email_hash', current_date),
      ('main.crm.contacts', 'email', 'main.sandbox.contacts__preview_ab12', 'email', current_date),
      ('main.crm.contacts', 'name', 'main.mart.customers', 'name', current_date),
      ('main.mart.customers', 'email_hash', 'main.gold.kpis', 'reachable', current_date),
      ('main.crm.contacts', 'email', 'main.old.thing', 'e', current_date - INTERVAL 400 DAY),
      ('main.crm.contacts', 'email', NULL, NULL, current_date)
    ) t(source_table_full_name, source_column_name, target_table_full_name, target_column_name, event_date)""")
    backend.execute("""CREATE TABLE sysc.column_tags AS SELECT * FROM (VALUES
      ('main', 'crm', 'contacts', 'email', 'pii', NULL),
      ('main', 'gold', 'kpis', 'reachable', 'certified', 'true')
    ) t(catalog_name, schema_name, table_name, column_name, tag_name, tag_value)""")
    return UnityCatalogLineage(backend, lookback_days=90, column_lineage_table="sysc.column_lineage",
                               column_tags_table="sysc.column_tags")


def test_uc_column_lineage_walk(uc):
    out = uc.column_downstream("main.crm.contacts", ["email"], depth=2)
    assert [(c.source_column, c.table, c.column, c.depth) for c in out] == [
        ("email", "main.mart.customers", "email_hash", 1),
        ("email", "main.gold.kpis", "reachable", 2),
    ]  # duplicates, preview clones, old events and NULL targets left out; 'name' not followed
    assert out[1].tags == {"certified": "true"}
    assert uc.column_tags("main.crm.contacts") == {"email": {"pii": ""}}


def test_uc_column_lineage_missing_table_raises_for_the_caller(backend):
    lin = UnityCatalogLineage(backend, column_lineage_table="nope.column_lineage", column_tags_table="nope.tags")
    import duckdb

    with pytest.raises(duckdb.CatalogException):
        lin.column_downstream("main.crm.contacts", ["email"])
    with pytest.raises(duckdb.CatalogException):
        lin.column_tags("main.crm.contacts")


def test_uc_refuses_suspicious_column_names(uc):
    with pytest.raises(ValueError, match="suspicious"):
        uc.column_downstream("main.crm.contacts", ["x') OR 1=1 --"])


# --- python -m bricksentry ------------------------------------------------------------
def test_python_dash_m_runs_the_cli():
    r = subprocess.run([sys.executable, "-m", "bricksentry", "--version"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "bricksentry, version" in r.stdout
