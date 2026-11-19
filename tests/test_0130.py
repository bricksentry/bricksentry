"""0.1.30: CREATE OR REPLACE TABLE previews write into a fresh sandbox table."""

from __future__ import annotations

from bricksentry import Config, Sentry
from bricksentry.backends.duckdb_backend import DuckDBBackend


class NoReplaceOnClone(DuckDBBackend):
    """Like serverless: replacing the shallow clone itself is refused."""

    def execute(self, sql):
        if "CREATE OR REPLACE TABLE" in sql.upper() and "__bs_" in sql and not sql.split("AS")[0].strip().endswith('_out"'):
            raise RuntimeError("[CLUSTER_BY_AUTO_UNSUPPORTED_TABLE_TYPE_ERROR] CLUSTER BY AUTO is only supported on UC Managed tables")
        return super().execute(sql)


def _sentry(b):
    b.execute("CREATE SCHEMA hr")
    b.execute("CREATE TABLE hr.gold AS SELECT * FROM (VALUES ('eng', 8), ('hr', 7), ('fin', 7)) t(department, headcount)")
    return Sentry(Config.from_dict({"sandbox_schema": "sandbox", "assessment_store": False,
                                    "tables": {"hr.gold": {"keys": ["department"]}}}), backend=b)


def test_create_or_replace_preview_reads_the_target_and_writes_a_fresh_table():
    b = NoReplaceOnClone()
    s = _sentry(b)
    out = s.preview_write(
        "CREATE OR REPLACE TABLE {{target}} AS SELECT department, headcount + 1 AS headcount FROM {{target}} "
        "WHERE department <> 'fin'", "hr.gold")
    d = out["tables"][0]["diff"]
    assert (d["changed_rows"], d["removed_rows"], d["added_rows"]) == (2, 1, 0)
    assert "_out" in out["preview"]["executed_sql"].split(" AS ")[0]
    left = b.query("SELECT table_name FROM information_schema.tables WHERE table_schema = 'sandbox'")
    assert left == []  # clone and result both dropped
    assert b.query("SELECT SUM(headcount) AS n FROM hr.gold")[0]["n"] == 22  # production untouched


def test_other_writes_still_use_the_clone():
    b = DuckDBBackend()
    s = _sentry(b)
    out = s.preview_write("UPDATE {{target}} SET headcount = 0 WHERE department = 'hr'", "hr.gold")
    assert out["tables"][0]["diff"]["changed_rows"] == 1
    assert "_out" not in out["preview"]["executed_sql"]


def test_redaction_keeps_punctuation_only_lists():
    from bricksentry.explain import redact_values as r

    msg = "[DELTA_INVALID_CHARACTERS_IN_COLUMN_NAMES] Found invalid character(s) among ' ,;{}()\\n\\t=' in the column names"
    assert "' ,;{}()" in r(msg)
    assert r("The value '123-45-6789' can't be cast") == "The value '…' can't be cast"
