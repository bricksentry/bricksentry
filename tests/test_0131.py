"""0.1.31: findings from the Free Edition edge-case rounds on 0.1.30."""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from test_0114 import FakeDelta

from bricksentry import job
from bricksentry.cleanup import drop_leftovers, find_leftovers, is_clone_name
from bricksentry.cli import main
from bricksentry.netguard import UnsafeURLError, _is_public, check_url
from bricksentry.preview import PreviewGuardError, rewrite_for_preview

CLONE = "sb.x.t__preview_0123abcd"


def q(p):
    return f"`{p}`"


def rw(sql, target="main.s.t"):
    return rewrite_for_preview(sql, target, CLONE, q)


# --- the guard reads quoted values and comments the way the engine does ---------------
@pytest.mark.parametrize("sql", [
    "UPDATE main.s.t SET a = '--'; DROP TABLE main.s.prod",
    'UPDATE main.s.t SET a = "--"; DROP TABLE main.s.prod',
    "UPDATE main.s.t SET a = '/*'; DELETE FROM main.s.prod; -- */",
    "UPDATE main.s.t SET a = 'x\\'; DELETE FROM main.s.prod; --'",  # backslash: DuckDB reads it differently
    "UPDATE main.s.t SET `a--b` = 1; DROP TABLE main.s.prod",
    "UPDATE main.s.t SET a = 'never closed; DROP TABLE main.s.prod",
    "UPDATE main.s.t SET a = 1 /* never closed ; DROP TABLE main.s.prod",
])
def test_hidden_second_statement_is_refused(sql):
    with pytest.raises(PreviewGuardError):
        rw(sql)


def test_quoted_values_and_comments_still_allowed():
    assert rw("UPDATE main.s.t SET note = 'it''s -- fine; ok' WHERE id = 1").startswith("UPDATE `sb`")
    assert rw("UPDATE main.s.t SET note = 'drop; truncate' WHERE id = 1")
    assert rw("UPDATE main.s.t SET p = 'C:\\\\tmp' WHERE id = 1")
    assert rw("UPDATE `main`.`s`.`t` SET `a--b` = 1").startswith("UPDATE `sb`")


def test_leading_comments_are_allowed():
    out = rw("-- fix duplicate orders\n/* reviewed */\nMERGE INTO main.s.t USING src ON t.id = src.id "
             "WHEN MATCHED THEN DELETE")
    assert "MERGE INTO `sb`.`x`.`t__preview_0123abcd`" in out
    with pytest.raises(PreviewGuardError, match="must start with"):
        rw("-- note\nSELECT * FROM main.s.t")


def test_placeholder_and_name_inside_strings_are_left_alone():
    out = rw("UPDATE {{target}} SET note = '{{target}}' WHERE id = 1")
    assert out == "UPDATE `sb`.`x`.`t__preview_0123abcd` SET note = '{{target}}' WHERE id = 1"
    out = rw("UPDATE main.s.t SET note = 'moved from main.s.t' WHERE id = 1")
    assert out.endswith("note = 'moved from main.s.t' WHERE id = 1")
    with pytest.raises(PreviewGuardError, match="only appears inside a quoted string"):
        rw("UPDATE other SET note = '{{target}}'")
    with pytest.raises(PreviewGuardError, match="Could not find"):
        rw("UPDATE other SET note = 'main.s.t'")


def test_preview_write_with_leading_comment_end_to_end(sentry):
    out = sentry.preview_write("-- agent: price fix\nUPDATE {{target}} SET amount = amount * 2 WHERE order_id <= 3",
                               "sales.orders")
    assert out["tables"][0]["diff"]["changed_rows"] == 3
    out = sentry.preview_write("/* rebuild */ CREATE OR REPLACE TABLE {{target}} AS "
                               "SELECT * FROM {{target}} WHERE order_id > 10", "sales.orders")
    assert out["tables"][0]["diff"]["removed_rows"] == 10
    assert sentry.backend.scalar("SELECT COUNT(*) FROM sales.orders") == 1000


# --- created scratch schemas are reported ----------------------------------------------
def test_created_scratch_schema_is_reported(sentry, caplog):
    with caplog.at_level(logging.WARNING, logger="bricksentry"):
        out = sentry.preview_write("UPDATE {{target}} SET status = 'x' WHERE order_id = 1", "sales.orders")
    assert out["preview"]["created_schema"] == "sandbox"
    assert "created scratch schema sandbox" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="bricksentry"):
        out = sentry.preview_write("UPDATE {{target}} SET status = 'y' WHERE order_id = 1", "sales.orders")
    assert "created_schema" not in out["preview"] and not caplog.text


# --- outbound URLs -----------------------------------------------------------------------
def test_cgnat_is_not_public(monkeypatch):
    for ip in ("100.64.0.1", "100.127.255.254", "::ffff:100.100.1.1"):
        assert not _is_public(ip)
    assert _is_public("100.128.0.1") and _is_public("8.8.8.8")
    with pytest.raises(UnsafeURLError):
        check_url("https://100.64.12.3/hook")


# --- the job with a webhook secret that can't be read -------------------------------------
def test_job_missing_secret_still_checks_then_fails_cleanly(monkeypatch):
    def boom(ref):
        raise RuntimeError("ResourceDoesNotExist: Scope bricksentry does not exist!\n  at ...")

    monkeypatch.setattr(job, "read_secret", boom)
    lines: list[str] = []
    code = job.run(["--tables", "sales.orders", "--scratch-schema", "scratch", "--fail-on", "never",
                    "--slack-secret", "bricksentry/slack"], backend=FakeDelta(), out=lines.append)
    text = "\n".join(lines)
    assert code == 2, text
    assert "--slack-secret bricksentry/slack: could not read the secret" in text
    assert "Scope bricksentry does not exist!" in text and "at ..." not in text
    assert "sales.orders: BLOCK" in text and "Undo: RESTORE" in text  # the check still ran
    assert "notification setup above is broken" in text


def test_job_malformed_secret_ref(monkeypatch):
    lines: list[str] = []
    code = job.run(["--tables", "sales.orders", "--scratch-schema", "scratch", "--slack-secret", "noslash"],
                   backend=FakeDelta(), out=lines.append)
    assert code == 2 and "must be scope/key" in "\n".join(lines)


# --- cleanup -------------------------------------------------------------------------------
def test_clone_name_patterns():
    for ok in ("orders__preview_0123abcd", "orders__preview_0123abcd_out", "bs_check_orders_v12_deadbeef"):
        assert is_clone_name(ok), ok
    for no in ("orders", "orders__preview", "orders__preview_0123ABCD", "orders__preview_0123abcd_x",
               "bs_check_orders", "bs_check_orders_v1_xyz", "my_bs_check_orders_v1_deadbeef", "bsv_employees"):
        assert not is_clone_name(no), no


class Dated(FakeDelta):
    def __init__(self, ages):
        super().__init__()
        self.ages = ages

    def list_tables(self, schema):
        now = datetime.now(timezone.utc)
        return [{"name": t["name"], "created": None if self.ages.get(t["name"]) is None
                 else (now - timedelta(hours=self.ages[t["name"]])).isoformat()}
                for t in super().list_tables(schema)]


def _seed(db):
    db.execute("CREATE SCHEMA IF NOT EXISTS scratch")
    for n in ("orders__preview_0123abcd", "orders__preview_0123abcd_out", "bs_check_orders_v3_deadbeef",
              "bs_check_orders_v4_cafebabe", "my_table", "orders_backup"):
        db.execute(f"CREATE TABLE scratch.{n} AS SELECT 1 AS x")


def test_find_and_drop_leftovers():
    db = Dated({"orders__preview_0123abcd": 30, "orders__preview_0123abcd_out": 30,
                "bs_check_orders_v3_deadbeef": 1, "my_table": 100, "orders_backup": 100})
    _seed(db)
    drop, skip = find_leftovers(db, "scratch", older_than_hours=6)
    assert drop == ["scratch.orders__preview_0123abcd", "scratch.orders__preview_0123abcd_out"]
    assert skip == ["scratch.bs_check_orders_v3_deadbeef", "scratch.bs_check_orders_v4_cafebabe"]  # recent / unknown
    assert drop_leftovers(db, drop + ["scratch.my_table"])[-1] == ("scratch.my_table",
                                                                   "not a bricksentry clone name; skipped")
    left = {t["name"] for t in FakeDelta.list_tables(db, "scratch")}
    assert left == {"bs_check_orders_v3_deadbeef", "bs_check_orders_v4_cafebabe", "my_table", "orders_backup"}
    drop, _ = find_leftovers(db, "scratch", older_than_hours=0)
    assert drop == ["scratch.bs_check_orders_v3_deadbeef", "scratch.bs_check_orders_v4_cafebabe"]


def test_cli_cleanup(monkeypatch, tmp_path):
    db = FakeDelta()
    _seed(db)
    monkeypatch.setattr("bricksentry.assess.make_backend", lambda spec: db)
    cfg = tmp_path / "bricksentry.yml"
    cfg.write_text("sandbox_schema: scratch\nassessment_store: false\nlineage: {type: static}\n")
    r = CliRunner().invoke(main, ["cleanup", "-c", str(cfg)])  # DuckDB has no created time: all kept
    assert r.exit_code == 0 and "Keeping 4 recent clone(s)" in r.output and "No leftover" in r.output
    r = CliRunner().invoke(main, ["cleanup", "-c", str(cfg), "--older-than-hours", "0"])  # not a tty, no --yes
    assert r.exit_code == 0 and "Nothing dropped" in r.output
    assert len(FakeDelta.list_tables(db, "scratch")) == 6
    r = CliRunner().invoke(main, ["cleanup", "-c", str(cfg), "--older-than-hours", "0", "--yes"])
    assert r.exit_code == 0, r.output
    assert r.output.count("dropped scratch.") == 4
    assert {t["name"] for t in FakeDelta.list_tables(db, "scratch")} == {"my_table", "orders_backup"}


def test_databricks_listing_sql():
    from test_0114 import RecordingDatabricks

    db = RecordingDatabricks()
    db.b.query = lambda q: db.sql.append(q) or []
    db.b.list_tables("cat.scratch")
    db.b.schema_exists("cat.scratch")
    sql = " ".join(db.sql)
    assert "`cat`.information_schema.tables WHERE table_schema = 'scratch'" in sql
    assert "`cat`.information_schema.schemata WHERE schema_name = 'scratch'" in sql
