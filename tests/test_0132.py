"""0.1.32: findings from the scale, adversarial and platform-feature round on 0.1.31."""

import pytest
from click.testing import CliRunner
from test_0114 import FakeDelta

from bricksentry import governed, job
from bricksentry.assess import Sentry
from bricksentry.cli import main
from bricksentry.config import Config
from bricksentry.preview import PreviewGuardError, rewrite_for_preview


def q(p):
    return f"`{p}`"


def rw(sql, target="main.s.t"):
    return rewrite_for_preview(sql, target, "sb.x.t__preview_0123abcd", q)


# --- a column named like a keyword -------------------------------------------------
def test_quoted_keyword_names_are_names():
    assert rw("UPDATE main.s.t SET `drop` = 1 WHERE id = 2").startswith("UPDATE `sb`")
    assert rw("INSERT INTO main.s.t (`drop`, `alter`, `grant`) VALUES (1, 2, 3)")
    assert rw('UPDATE main.s.t SET "truncate" = 1')
    assert rw("UPDATE main.s.t SET `a;b` = 1")
    assert rw("UPDATE `main`.`s`.`t` SET `drop` = 1")


@pytest.mark.parametrize("sql", [
    "UPDATE main.s.t SET drop = 1",  # unquoted: still a keyword
    "UPDATE main.s.t SET `drop` = 1; DROP TABLE main.s.prod",
    'UPDATE main.s.t SET a = "x\\"; DROP TABLE main.s.prod; --"',  # DuckDB: no escapes in "..."
    'UPDATE main.s.t SET a = 1 "; DROP TABLE main.s.prod',  # never closed
    "UPDATE main.s.t SET a = 1 `; DROP TABLE main.s.prod",
])
def test_quoted_names_hide_nothing(sql):
    with pytest.raises(PreviewGuardError):
        rw(sql)


def test_production_name_in_backticks_is_still_found():
    # The leftover check still reads inside backticks: a second reference to production refused.
    with pytest.raises(PreviewGuardError):
        rewrite_for_preview("INSERT INTO {{target}} SELECT * FROM `main`.`s`.`t`", "main.s.t",
                            "sb.x.t__preview_0123abcd", q)


# --- tables with column masks or row filters -----------------------------------------
class Governed(FakeDelta):
    def __init__(self, prot=None, clone_error=None):
        super().__init__()
        self.prot = prot or {}
        self.clone_error = clone_error

    def protections(self, tables):
        return {t: self.prot[t] for t in tables if t in self.prot}

    def clone_version(self, source, target, version):
        if self.clone_error:
            raise RuntimeError(self.clone_error)
        return super().clone_version(source, target, version)

    def clone(self, source, target):
        if self.clone_error:
            raise RuntimeError(self.clone_error)
        return super().clone(source, target)


def sentry(db):
    return Sentry(Config.from_dict({"assessment_store": False, "sandbox_schema": "scratch",
                                    "tables": {"sales.orders": {"keys": ["order_id"]}}}), backend=db)


def test_describe():
    db = Governed({"sales.orders": {"masked": ["ssn", "amount"], "row_filter": True}})
    assert governed.describe(db, "sales.orders") == "a column mask (on amount, ssn) and a row filter"
    assert governed.describe(db, "sales.other") is None
    assert governed.from_error(RuntimeError("[COLUMN_MASKS_FEATURE_NOT_SUPPORTED.TIME_TRAVEL] x")) == "a column mask"
    assert governed.from_error(RuntimeError("[ROW_LEVEL_SECURITY_FEATURE_NOT_SUPPORTED.CLONE]")) == "a row filter"
    assert governed.from_error(RuntimeError("other")) is None


def test_check_change_refuses_up_front():
    db = Governed({"sales.orders": {"masked": ["amount"], "row_filter": False}})
    with pytest.raises(ValueError, match="sales.orders has a column mask \\(on amount\\).*can't compare versions"):
        sentry(db).check_change("sales.orders")
    assert not [t for t in FakeDelta.list_tables(db, "scratch")]  # nothing was cloned


def test_check_change_recognises_the_engine_error():
    db = Governed(clone_error="[ROW_LEVEL_SECURITY_FEATURE_NOT_SUPPORTED.TIME_TRAVEL] Time travel is not supported")
    with pytest.raises(ValueError, match="has a row filter.*dev copy without the mask or filter"):
        sentry(db).check_change("sales.orders")


def test_preview_refuses_up_front_and_never_suggests_unmasking():
    db = Governed({"sales.orders": {"masked": [], "row_filter": True}})
    with pytest.raises(ValueError) as e:
        sentry(db).preview_write("UPDATE {{target}} SET amount = 0 WHERE order_id = 1", "sales.orders")
    msg = str(e.value)
    assert "won't copy governed data" in msg and "drop" not in msg.lower() and "remove" not in msg.lower()
    db = Governed(clone_error="[COLUMN_MASKS_FEATURE_NOT_SUPPORTED.CLONE] Shallow clone not supported")
    with pytest.raises(ValueError, match="has a column mask"):
        sentry(db).preview_write("UPDATE {{target}} SET amount = 0 WHERE order_id = 1", "sales.orders")


def test_cli_and_job_report_it_cleanly(monkeypatch, tmp_path):
    db = Governed({"sales.orders": {"masked": ["amount"], "row_filter": False}})
    monkeypatch.setattr("bricksentry.assess.make_backend", lambda spec: db)
    cfg = tmp_path / "bricksentry.yml"
    cfg.write_text("sandbox_schema: scratch\nassessment_store: false\nlineage: {type: static}\n")
    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", str(cfg), "--no-notify"])
    assert r.exit_code == 2 and "has a column mask" in r.output and "Traceback" not in r.output
    lines: list[str] = []
    code = job.run(["--tables", "sales.orders", "--scratch-schema", "scratch"], backend=db, out=lines.append)
    assert code == 2 and "has a column mask" in "\n".join(lines)


# --- governance metadata is read afresh for every check (0.1.33) ----------------------
def _uc(backend):
    from bricksentry.lineage import UnityCatalogLineage

    backend.execute("CREATE SCHEMA IF NOT EXISTS sys3")
    backend.execute("CREATE OR REPLACE TABLE sys3.table_lineage AS SELECT * FROM (VALUES "
                    "('sales.orders', 'sales.daily', 'JOB', '1', current_date)) "
                    "t(source_table_full_name, target_table_full_name, entity_type, entity_id, event_date)")
    backend.execute("CREATE OR REPLACE TABLE sys3.tables AS SELECT * FROM (VALUES "
                    "(NULL, 'sales', 'orders', 'data-team'), (NULL, 'sales', 'daily', 'bi')) "
                    "t(table_catalog, table_schema, table_name, table_owner)")
    backend.execute("CREATE OR REPLACE TABLE sys3.table_tags AS SELECT * FROM (VALUES "
                    "('x', 'x', 'x', 'x', NULL)) t(catalog_name, schema_name, table_name, tag_name, tag_value)")
    return UnityCatalogLineage(backend, lineage_table="sys3.table_lineage", tables_table="sys3.tables",
                               tags_table="sys3.table_tags")


def test_tag_added_after_first_check_is_seen(sentry):
    b = sentry.backend
    lin = _uc(b)
    s = Sentry(sentry.config, backend=b, lineage=lin)
    first = s.preview_write("UPDATE {{target}} SET status = 'x' WHERE order_id = 1", "sales.orders")
    assert "pii" not in str(first)
    # A long-lived process (the MCP server): the tag is added after the first check.
    b.execute("INSERT INTO sys3.table_tags VALUES (NULL, 'sales', 'orders', 'pii', NULL), "
              "(NULL, 'sales', 'daily', 'certified', NULL)")
    second = s.preview_write("UPDATE {{target}} SET status = 'y' WHERE order_id = 1", "sales.orders")
    t = second["tables"][0]
    assert "pii" in t["tags"], t
    nodes = s.downstream("sales.orders")
    assert any("certified" in n.tags for n in nodes)


def test_in_memory_assessments_are_capped(sentry, monkeypatch):
    monkeypatch.setattr(type(sentry), "_MAX_IN_MEMORY", 3)
    ids = [sentry.preview_write(f"UPDATE {{{{target}}}} SET status = 's{i}' WHERE order_id = 1",
                                "sales.orders")["id"] for i in range(5)]
    assert list(sentry._assessments) == ids[-3:]
