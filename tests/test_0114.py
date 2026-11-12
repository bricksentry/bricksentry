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
    """Render the subset of Go templates the bundle uses: {{.x}}, {{`raw`}}, nested
    {{if .x}} / {{if eq .x "y"}} ... {{end}}, with optional {{- trimming."""
    text = re.sub(r"\{\{`(.*?)`\}\}", lambda m: m.group(1).replace("{{", "\x00").replace("}}", "\x01"), text)
    tok = re.compile(r"(\s*)\{\{(-?) ?(if (?:eq )?\.\w+(?: \"[^\"]*\")?|else|end)\}\}")

    def cond(expr: str) -> bool:
        m = re.fullmatch(r"if (eq )?\.(\w+)(?: \"([^\"]*)\")?", expr)
        assert m, expr
        v = values[m.group(2)]
        return v == m.group(3) if m.group(1) else bool(v)

    out, stack, pos = [], [True], 0
    for m in tok.finditer(text):
        if all(stack):
            out.append(text[pos:m.start()] + ("" if m.group(2) else m.group(1)))
        expr = m.group(3)
        if expr == "end":
            stack.pop()
        elif expr == "else":
            stack[-1] = not stack[-1]
        else:
            stack.append(cond(expr))
        pos = m.end()
    assert stack == [True], "unbalanced if/end"
    out.append(text[pos:])
    text = "".join(out)
    text = re.sub(r"\{\{\.(\w+)\}\}", lambda m: str(values[m.group(1)]), text)
    assert "{{" not in text, text[text.index("{{") - 80:text.index("{{") + 80]
    return text.replace("\x00", "{{").replace("\x01", "}}")


TABLES = "main.sales.orders, main.sales.customers"


@pytest.mark.parametrize("slack,keys,demo,tables", [
    ("bricksentry/slack_webhook", "main.sales.orders=order_id", "yes", TABLES),
    ("", "", "no", TABLES), ("", "", "yes", TABLES), ("", "", "yes", ""),
])
def test_bundle_template_renders(slack, keys, demo, tables):
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1] / "templates" / "job-check"
    schema = json.loads((root / "databricks_template_schema.json").read_text())
    # Databricks evaluates templated defaults at init; {{default_catalog}} is the workspace default
    dc = 'if eq (default_catalog) ""}}main{{else}}{{default_catalog}}{{end}}'
    assert schema["properties"]["scratch_schema"]["default"] == "{{" + dc + ".bricksentry_scratch"
    values = {k: p.get("default", "") for k, p in schema["properties"].items()}
    values["scratch_schema"] = "main.bricksentry_scratch"
    assert values["bricksentry_version"] == __import__("bricksentry").__version__
    assert [k for k, p in schema["properties"].items() if "default" not in p] == ["warehouse_id"]
    values.update(tables=tables, warehouse_id="abc123", slack_secret=slack,
                  keys=keys, demo=demo)
    for k, p in schema["properties"].items():
        if "pattern" in p:
            assert re.fullmatch(p["pattern"], values[k]), k
    proj = root / "template" / "{{.project_name}}"
    bundle = yaml.safe_load(_render((proj / "databricks.yml.tmpl").read_text(), values))
    assert bundle["bundle"]["name"] == "bricksentry_check"
    job_yml = yaml.safe_load(_render((proj / "resources" / "bricksentry_check.job.yml.tmpl").read_text(), values))
    jobs = job_yml["resources"]["jobs"]
    assert ("bricksentry_check" in jobs) == bool(tables)
    j = jobs.get("bricksentry_check")
    if j:
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

    assert ("bricksentry_demo" in jobs) == (demo == "yes")
    if demo == "yes":
        dj = jobs["bricksentry_demo"]
        make, check = dj["tasks"]
        assert make["spark_python_task"]["python_file"] == "../src/bricksentry_demo.py"
        assert check["depends_on"] == [{"task_key": "make_bad_change"}]
        dargs = job.build_parser().parse_args(check["spark_python_task"]["parameters"])
        assert dargs.tables == "main.bricksentry_scratch.bricksentry_demo_orders" and dargs.key == ["order_id"]
        assert dargs.since is None and dargs.since_minutes is None and dargs.fail_on == "block"
        assert (dargs.slack_secret is not None) == bool(slack)
        assert all(t["max_retries"] == 0 and t["disable_auto_optimization"] for t in dj["tasks"])
        assert dj["environments"][0]["spec"]["dependencies"] == [
            f"bricksentry[databricks]=={values['bricksentry_version']}"]
    msg = _render(schema["success_message"], values)
    assert ("bundle run bricksentry_demo" in msg) == (demo == "yes")
    readme = _render((proj / "README.md.tmpl").read_text(), values)
    assert ("See a block first" in readme) == (demo == "yes")
    assert ("no tables to watch yet" in readme) == (not tables)
    assert ("Then any change" in msg) == bool(tables)
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


# --- demo job: the known-bad change ---------------------------------------------------
class RecordingBackend:
    def __init__(self):
        self.sql: list[str] = []
        self.schemas: list[str] = []

    def qualified(self, t):
        return ".".join(f"`{p}`" for p in t.split("."))

    def ensure_schema(self, s):
        self.schemas.append(s)

    def execute(self, q):
        self.sql.append(q)


def test_job_demo_sql_and_guard():
    from bricksentry import job_demo

    b, lines = RecordingBackend(), []
    assert job_demo.run(["--table", "main.scratch.bricksentry_demo_orders"], backend=b, out=lines.append) == 0
    assert b.schemas == ["main.scratch"]
    seed, merge = b.sql
    assert seed.startswith("CREATE OR REPLACE TABLE `main`.`scratch`.`bricksentry_demo_orders` AS")
    assert "MERGE INTO `main`.`scratch`.`bricksentry_demo_orders` AS t" in merge
    assert "t.region = s.region" in merge and "lower(region)" in merge
    assert "Expect BLOCK" in lines[-1]
    for bad in ("main.sales.orders", "orders", "main.scratch.x; DROP TABLE y"):
        with pytest.raises(SystemExit):
            job_demo.run(["--table", bad], backend=RecordingBackend())


def test_job_demo_change_is_blocked_end_to_end():
    """The demo's bad MERGE, replayed on DuckDB history, is a block with the right undo."""
    db = FakeDelta()
    db.execute("CREATE TABLE sales.demo_v1 AS SELECT * FROM sales.orders_v1")
    # the MERGE's effect: 25 orders inserted again (ON never matches)
    db.execute("CREATE TABLE sales.demo_v2 AS SELECT * FROM sales.demo_v1 "
               "UNION ALL SELECT order_id, round(amount * 1.05, 2) FROM sales.demo_v1 WHERE order_id % 80 = 0")
    db._history = [{"version": 2, "operation": "MERGE", "timestamp": "2026-11-12 10:00:00"},
                   {"version": 1, "operation": "CREATE OR REPLACE TABLE AS SELECT", "timestamp": "2026-11-12 09:59:00"},
                   {"version": 0, "operation": "CREATE TABLE AS SELECT", "timestamp": "2026-11-01 00:00:00"}]
    db.execute("CREATE TABLE sales.demo AS SELECT * FROM sales.demo_v2")
    lines: list[str] = []
    code = job.run(["--tables", "sales.demo", "--key", "order_id", "--scratch-schema", "scratch"],
                   backend=db, out=lines.append)
    text = "\n".join(lines)
    assert code == 1 and "BLOCK" in text and "duplicate-keys: 25" in text
    assert 'TO VERSION AS OF 1' in text


def test_missing_scratch_catalog_message():
    from bricksentry import job_demo

    class NoCatalog(FakeDelta):
        def ensure_schema(self, schema):
            raise RuntimeError("[NO_SUCH_CATALOG_EXCEPTION] Catalog 'main' was not found. SQLSTATE: 42704")

    with pytest.raises(RuntimeError, match="scratch is in the default catalog, which doesn't exist"):
        sentry(NoCatalog()).check_change("sales.orders")
    b = RecordingBackend()
    b.ensure_schema = NoCatalog.ensure_schema.__get__(b)
    with pytest.raises(SystemExit, match="Set the scratch schema"):
        job_demo.run(["--table", "main.scratch.bricksentry_demo_orders"], backend=b)

    class Other(FakeDelta):
        def ensure_schema(self, schema):
            raise RuntimeError("PERMISSION_DENIED")

    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        sentry(Other()).check_change("sales.orders")
