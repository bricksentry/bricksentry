"""0.1.14: check a change that already happened (Delta history) and the job task."""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

from bricksentry import job
from bricksentry.assess import Sentry
from bricksentry.backends.duckdb_backend import DuckDBBackend
from bricksentry.cli import main
from bricksentry.config import Config
from bricksentry.history import NoChangeError, check_change, is_data_op, pick_change

HISTORY = [
    {"version": 4, "timestamp": "2026-11-11 10:05:00", "operation": "OPTIMIZE"},
    {"version": 3, "timestamp": "2026-11-11 10:00:00", "operation": "MERGE"},
    {"version": 2, "timestamp": "2026-11-11 09:59:00", "operation": "DELETE"},
    {"version": 1, "timestamp": "2026-11-10 09:00:00", "operation": "WRITE"},
    {"version": 0, "timestamp": "2026-11-01 09:00:00", "operation": "CREATE TABLE"},
]


class FakeDelta(DuckDBBackend):
    """DuckDB with Delta-style history: version N of `orders` is table `orders_vN`."""

    supports_time_travel = True

    def __init__(self, history=HISTORY, pk=("order_id",)):
        super().__init__()
        self._history, self._pk = history, list(pk)
        self.dropped: list[str] = []
        self.execute(
            """CREATE SCHEMA sales;
            CREATE TABLE sales.orders_v1 AS SELECT i AS order_id, i * 1.5 AS amount FROM range(1, 2001) t(i);
            CREATE TABLE sales.orders_v2 AS SELECT * FROM sales.orders_v1 WHERE order_id % 10 <> 0;
            CREATE TABLE sales.orders_v3 AS SELECT * FROM sales.orders_v2
                UNION ALL SELECT * FROM sales.orders_v1 WHERE order_id < 5;
            CREATE TABLE sales.orders_v4 AS SELECT * FROM sales.orders_v3;
            CREATE TABLE sales.orders AS SELECT * FROM sales.orders_v4;"""
        )

    def history(self, table, limit=50):
        return self._history[:limit]

    def clone_version(self, source, target, version):
        self.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {source}_v{version}")

    def primary_key(self, table):
        return self._pk

    def drop(self, table):
        self.dropped.append(table)
        super().drop(table)


def sentry(db=None, **extra):
    cfg = Config.from_dict({"sandbox_schema": "scratch", "assessment_store": False,
                            "lineage": {"type": "static", "edges": {"sales.orders": ["sales.daily_revenue"]},
                                        "tags": {"sales.daily_revenue": {"certified": "true"}}}, **extra})
    return Sentry(cfg, backend=db or FakeDelta())


# --- picking the change ---------------------------------------------------------
def test_data_ops():
    assert is_data_op("merge") and is_data_op("STREAMING UPDATE")
    assert not is_data_op("OPTIMIZE") and not is_data_op("VACUUM END") and not is_data_op(None)


def test_pick_latest_skips_optimize():
    c = pick_change("t", HISTORY)
    assert (c.base_version, c.version, c.operation) == (2, 3, "MERGE")


def test_pick_specific_version():
    c = pick_change("t", HISTORY, version=2)
    assert (c.base_version, c.version, c.operation) == (1, 2, "DELETE")
    with pytest.raises(NoChangeError):
        pick_change("t", HISTORY, version=0)


def test_pick_since_folds_the_whole_run():
    c = pick_change("t", HISTORY, since="2026-11-11T09:30:00Z")
    assert (c.base_version, c.version, c.versions) == (1, 3, [2, 3])
    assert c.operation == "DELETE + MERGE"
    # epoch millis, as {{job.start_time.timestamp_ms}} gives
    assert pick_change("t", HISTORY, since="1762851600000").version == 3
    with pytest.raises(NoChangeError):
        pick_change("t", HISTORY, since="2026-11-12T00:00:00Z")
    with pytest.raises(ValueError):
        pick_change("t", HISTORY, since="yesterday")


def test_pick_no_data_change():
    with pytest.raises(NoChangeError, match="OPTIMIZE"):
        pick_change("t", [{"version": 5, "operation": "OPTIMIZE"}, {"version": 4, "operation": "VACUUM END"}])
    with pytest.raises(NoChangeError):
        pick_change("t", [])


# --- check_change ---------------------------------------------------------------
def test_check_change_end_to_end():
    db = FakeDelta()
    a, change = sentry(db).check_change("sales.orders")
    t = a.tables[0]
    assert change.version == 3 and change.base_version == 2
    assert t.diff.added_rows == 0 and t.diff.base_rows == 1800 and t.diff.candidate_rows == 1804
    assert t.diff.duplicate_keys_candidate > 0
    assert a.verdict.value == "block"  # duplicate keys
    assert t.rollback == 'RESTORE TABLE "sales"."orders" TO VERSION AS OF 2'
    assert t.summary.startswith("v2 -> v3 (MERGE): ")
    assert [n.name for n in t.downstream] == ["sales.daily_revenue"]
    assert [re.sub(r"_[0-9a-f]{8}$", "", d) for d in db.dropped] == [
        "scratch.bs_check_orders_v2", "scratch.bs_check_orders_v3"]
    assert db.dropped[0][-8:] == db.dropped[1][-8:]  # one run id per check
    assert not any(db.table_exists(d) for d in db.dropped)


def test_check_change_since_and_keys_from_config():
    db = FakeDelta(pk=())
    s = sentry(db, tables={"sales.orders": {"keys": ["order_id"]}})
    a, change = s.check_change("sales.orders", since="2026-11-11 09:30:00")
    assert change.base_version == 1 and a.tables[0].diff.removed_rows == 200


def test_check_change_errors():
    with pytest.raises(NotImplementedError, match="Delta table history"):
        Sentry(Config.from_dict({"assessment_store": False}), backend=DuckDBBackend()).check_change("sales.orders")
    with pytest.raises(ValueError, match="No key columns"):
        sentry(FakeDelta(pk=())).check_change("sales.orders")
    with pytest.raises(ValueError, match="schema.table"):
        sentry().check_change("orders; DROP TABLE x")

    class Vacuumed(FakeDelta):
        def clone_version(self, source, target, version):
            raise RuntimeError("DELTA_FILE_NOT_FOUND")

    db = Vacuumed()
    with pytest.raises(RuntimeError, match="VACUUM"):
        sentry(db).check_change("sales.orders")
    assert db.dropped  # cleanup still ran


def test_base_backend_has_no_history():
    db = DuckDBBackend()
    with pytest.raises(NotImplementedError):
        db.history("x")
    with pytest.raises(NotImplementedError):
        db.clone_version("a", "b", 1)
    assert db.primary_key("a.b.c") == []


def test_check_change_function_matches_method():
    a, c = check_change(sentry(), "sales.orders", version=2)
    assert c.operation == "DELETE" and a.tables[0].diff.removed_rows == 200 and a.verdict.value != "block"


# --- Databricks SQL ---------------------------------------------------------------
class RecordingDatabricks:
    def __init__(self):
        from bricksentry.backends.databricks_backend import DatabricksBackend

        self.sql: list[str] = []
        b = DatabricksBackend.__new__(DatabricksBackend)
        b.query = lambda q: self.sql.append(q) or [{"column_name": "id"}, {"column_name": "day"}]
        b.execute = lambda q: self.sql.append(q)
        self.b = b


def test_databricks_sql():
    r = RecordingDatabricks()
    r.b.history("main.sales.orders", 20)
    r.b.clone_version("main.sales.orders", "main.scratch.x", 7)
    assert r.b.primary_key("main.sales.or'ders") == ["id", "day"]
    assert r.b.primary_key("sales.orders") == []
    assert r.sql[0] == "DESCRIBE HISTORY `main`.`sales`.`orders` LIMIT 20"
    assert r.sql[1] == ("CREATE OR REPLACE TABLE `main`.`scratch`.`x` SHALLOW CLONE "
                        "`main`.`sales`.`orders` VERSION AS OF 7")
    assert "c.table_name = 'or''ders'" in r.sql[2] and "PRIMARY KEY" in r.sql[2]


# --- CLI -----------------------------------------------------------------------
@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    db = FakeDelta()
    monkeypatch.setattr("bricksentry.assess.make_backend", lambda spec: db)
    cfg = tmp_path / "bricksentry.yml"
    cfg.write_text("sandbox_schema: scratch\nassessment_store: false\n"
                   "lineage: {type: static}\n")
    return str(cfg)


def test_cli_check_last_change(cli_env):
    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", cli_env, "--no-notify"])
    assert r.exit_code == 1, r.output  # block >= default --fail-on block
    assert "BLOCK" in r.output and 'Undo: RESTORE TABLE "sales"."orders" TO VERSION AS OF 2' in r.output

    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", cli_env, "--no-notify",
                                  "--version", "2", "--format", "json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["change"]["operation"] == "DELETE" and d["tables"][0]["diff"]["removed_rows"] == 200

    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", cli_env, "--version", "2",
                                  "--since", "2026-01-01"])
    assert r.exit_code == 2 and "not both" in r.output


def test_cli_no_change(cli_env):
    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", cli_env, "--no-notify",
                                  "--since", "2027-01-01T00:00:00Z"])
    assert r.exit_code == 2
    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", cli_env, "--no-notify",
                                  "--since", "2027-01-01T00:00:00Z", "--allow-no-change"])
    assert r.exit_code == 0 and "No data change" in r.output


def test_cli_markdown_output(cli_env, tmp_path):
    out = tmp_path / "r.md"
    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", cli_env, "--no-notify",
                                  "-o", str(out), "--format", "markdown"])
    assert out.exists() and 'RESTORE TABLE "sales"."orders" TO VERSION AS OF 2' in out.read_text()
    assert r.exit_code == 1


# --- job task ------------------------------------------------------------------------
def test_parse_tables_and_keys():
    assert job.parse_tables('["main.a.b", "main.a.c", "main.a.b"]') == ["main.a.b", "main.a.c"]
    assert job.parse_tables(" a.b , `c.d` ,") == ["a.b", "c.d"]
    assert job.parse_tables("") == []
    assert job.parse_keys(["id", "m.s.t=a, b"]) == {"*": ["id"], "m.s.t": ["a", "b"]}


def test_job_run_fails_on_block_and_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr("bricksentry.notify.send", lambda a, st, pr=None, force=False:
                        sent.append((a.verdict.value, st.slack_webhook, st.run_url)) or ["slack: sent"])
    monkeypatch.setattr(job, "read_secret", lambda ref: "https://hooks.slack.example/T/B/x" if ref else None)
    lines: list[str] = []
    code = job.run(["--tables", '["sales.orders"]', "--scratch-schema", "scratch", "--warehouse-id", "w1",
                    "--slack-secret", "bricksentry/slack", "--run-url", "https://example.cloud.databricks.com/jobs/1/runs/2",
                    "--since", "2026-11-11T09:30:00Z"],
                   backend=FakeDelta(), out=lines.append)
    text = "\n".join(lines)
    assert code == 1, text
    assert "sales.orders: BLOCK" in text and "v1 -> v3 (DELETE + MERGE)" in text
    assert 'Undo: RESTORE TABLE "sales"."orders" TO VERSION AS OF 1' in text
    assert "failing this task" in text
    assert sent == [("block", "https://hooks.slack.example/T/B/x", "https://example.cloud.databricks.com/jobs/1/runs/2")]


def test_job_run_fail_on_never_and_no_change():
    lines: list[str] = []
    assert job.run(["--tables", "sales.orders", "--scratch-schema", "scratch", "--fail-on", "never"],
                   backend=FakeDelta(), out=lines.append) == 0
    lines.clear()
    assert job.run(["--tables", "sales.orders", "--since", "2027-01-01"], backend=FakeDelta(),
                   out=lines.append) == 0
    assert "nothing to check" in lines[0]
    assert job.run(["--tables", ""], backend=FakeDelta(), out=lines.append) == 0


def test_job_run_error_exit_2_and_policy():
    lines: list[str] = []
    db = FakeDelta(pk=())
    assert job.run(["--tables", "sales.orders", "--policy", "strict"], backend=db, out=lines.append) == 2
    assert "ERROR No key columns" in lines[0]
    lines.clear()
    assert job.run(["--tables", "sales.orders", "--key", "sales.orders=order_id", "--scratch-schema", "scratch",
                    "--policy", "strict", "--fail-on", "needs_human"], backend=db, out=lines.append) == 1


def test_job_make_sentry_settings(tmp_path):
    cfg = tmp_path / "b.yml"
    cfg.write_text("backend: {type: duckdb}\nnotify: {on: block}\n")
    args = job.build_parser().parse_args(["--tables", "x.y", "--config", str(cfg), "--policy", "finance",
                                          "--notify-on", "warn", "--warehouse-id", "w9", "--run-url", "javascript:x"])
    s = job.make_sentry(args, backend=FakeDelta())
    assert s.config.backend == {"type": "databricks", "warehouse_id": "w9"}
    assert s.config.notify == {"on": "warn"}
    assert any(r.get("name") == "lineage-unavailable" for r in s.config.policy["rules"])


def test_bundle_template_files():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "templates" / "job-check"
    schema = json.loads((root / "databricks_template_schema.json").read_text())
    for name in ("tables", "warehouse_id", "scratch_schema"):
        assert name in schema["properties"]
    tmpl = (root / "template" / "{{.project_name}}" / "resources" / "bricksentry_check.job.yml.tmpl").read_text()
    assert "table_update:" in tmpl and "environment_key: default" in tmpl
    assert "bricksentry.job" in (root / "template" / "{{.project_name}}" / "src" / "bricksentry_check.py").read_text()
    for p in root.rglob("*"):
        if p.is_file():
            t = p.read_text()
            assert "94af1bc5" not in t and "@gmail" not in t
            assert not re.search(r"hooks\.slack\.com/services/T", t)


# --- render the bundle template (a small subset of Go templates is used on purpose) ---
def _render(text: str, values: dict) -> str:
    def cond(m):
        return m.group(2) if values.get(m.group(1)) else ""

    text = re.sub(r"\{\{-? ?if \.(\w+)\}\}(.*?)\{\{-? ?end\}\}", cond, text, flags=re.S)
    text = re.sub(r"\{\{`(.*?)`\}\}", lambda m: m.group(1), text)
    text = re.sub(r"\{\{\.(\w+)\}\}", lambda m: str(values[m.group(1)]), text)
    assert "{{." not in text and "{{-" not in text
    return text


@pytest.mark.parametrize("slack,keys", [("bricksentry/slack_webhook", "main.sales.orders=order_id"), ("", "")])
def test_bundle_template_renders(slack, keys):
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1] / "templates" / "job-check"
    schema = json.loads((root / "databricks_template_schema.json").read_text())
    values = {k: p.get("default", "") for k, p in schema["properties"].items()}
    assert values["bricksentry_version"] == __import__("bricksentry").__version__
    values.update(tables="main.sales.orders, main.sales.customers", warehouse_id="abc123", slack_secret=slack,
                  keys=keys)
    for k, p in schema["properties"].items():
        if "pattern" in p:
            assert re.fullmatch(p["pattern"], values[k]), k
    proj = root / "template" / "{{.project_name}}"
    bundle = yaml.safe_load(_render((proj / "databricks.yml.tmpl").read_text(), values))
    assert bundle["bundle"]["name"] == "bricksentry_check"
    job_yml = yaml.safe_load(_render((proj / "resources" / "bricksentry_check.job.yml.tmpl").read_text(), values))
    j = job_yml["resources"]["jobs"]["bricksentry_check"]
    assert j["trigger"]["table_update"]["table_names"] == ["main.sales.orders", "main.sales.customers"]
    task = j["tasks"][0]
    params = task["spark_python_task"]["parameters"]
    assert params[:2] == ["--tables", "{{job.trigger.table_update.updated_tables}}"]
    assert "{{workspace.url}}/jobs/{{job.id}}/runs/{{job.run_id}}" in params
    assert ("--slack-secret" in params) == bool(slack)
    assert ("--keys" in params) == bool(keys)
    assert task["max_retries"] == 0 and task["disable_auto_optimization"] is True
    assert j["environments"][0]["spec"]["dependencies"] == [f"bricksentry[databricks]=={values['bricksentry_version']}"]
    assert task["environment_key"] == j["environments"][0]["environment_key"]
    assert j["environments"][0]["spec"]["dependencies"][0].startswith("bricksentry[databricks]")
    # the job task's argv parses, and an unresolved trigger value falls back to --default-tables
    args = job.build_parser().parse_args(params)
    assert args.fail_on == "block" and args.since_minutes == 60
    readme = _render((proj / "README.md.tmpl").read_text(), values)
    assert "{{job.start_time.iso_datetime}}" in readme
    assert (proj / "src" / "bricksentry_check.py").exists()


def test_job_unresolved_trigger_value_uses_default_tables():
    lines: list[str] = []
    code = job.run(["--tables", "{{job.trigger.table_update.updated_tables}}", "--default-tables", "sales.orders",
                    "--since-minutes", "1", "--scratch-schema", "scratch"], backend=FakeDelta(), out=lines.append)
    assert code == 0 and "nothing to check" in lines[0]  # history is older than 1 minute


def test_yaml_on_key_is_read():
    cfg = Config.from_dict(__import__("yaml").safe_load("notify:\n  on: block\n"))
    assert cfg.notify == {"on": "block"} and not cfg.warnings


def test_job_main_returns_on_success_and_exits_on_failure(monkeypatch):
    # Databricks marks a spark_python_task failed on any SystemExit, even 0 (found live in 0.1.14).
    monkeypatch.setattr(job, "run", lambda argv: 0)
    assert job.main([]) is None
    monkeypatch.setattr(job, "run", lambda argv: 1)
    with pytest.raises(SystemExit) as e:
        job.main([])
    assert e.value.code == 1


def test_job_keys_option():
    lines: list[str] = []
    code = job.run(["--tables", "sales.orders", "--keys", "sales.orders=order_id; sales.other=x",
                    "--scratch-schema", "scratch", "--fail-on", "never"], backend=FakeDelta(pk=()), out=lines.append)
    assert code == 0 and "sales.orders: BLOCK" in lines[0]


def test_concurrent_checks_use_different_clones():
    """Two checks of the same table must not share clone names (found live: one job's cleanup
    dropped the other's clone mid-diff)."""
    db = FakeDelta()
    sentry(db).check_change("sales.orders")
    sentry(db).check_change("sales.orders")
    assert len(set(db.dropped)) == 4
