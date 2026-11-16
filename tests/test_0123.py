"""0.1.23: exact totals, one-to-one matching of duplicated keys, and an audit trail
on every assessment."""

from bricksentry import Config, Sentry, __version__
from bricksentry.backends.databricks_backend import DatabricksBackend
from bricksentry.diff import diff_tables
from bricksentry.lineage import StaticLineage
from bricksentry.models import Assessment
from bricksentry.policy import Policy
from bricksentry.report import to_markdown


# --- exact totals ----------------------------------------------------------------
def test_decimal_totals_are_exact(backend):
    backend.execute("CREATE SCHEMA m")
    backend.execute("CREATE TABLE m.base AS SELECT i AS id, CAST(0.10 AS DECIMAL(18, 2)) AS amount "
                    "FROM range(1, 1000001) t(i)")
    backend.execute("CREATE TABLE m.cand AS SELECT * FROM m.base")
    d = diff_tables(backend, "m.base", "m.cand", ["id"])
    amount = d.columns[0]
    # Adding 0.1 a million times as DOUBLE drifts (100000.00000133...); as DECIMAL it doesn't.
    assert amount.base_sum == 100000.0 and amount.candidate_sum == 100000.0
    assert amount.sum_change_pct == 0


def test_sum_sql_by_type():
    b = DatabricksBackend(warehouse_id="wh", client=object())
    assert b.exact_sum("`a`", "decimal(18,2)") == "SUM(`a`)"
    assert b.exact_sum("`a`", "bigint") == "SUM(CAST(`a` AS DECIMAL(38, 0)))"  # no BIGINT overflow
    assert b.exact_sum("`a`", "double") == "SUM(CAST(`a` AS DOUBLE))"
    assert b.row_hash(["a", "b"]) == "xxhash64(`a`, `b`)"


# --- duplicated keys are matched one to one --------------------------------------
def test_many_null_keys_do_not_multiply(backend):
    backend.execute("CREATE SCHEMA n")
    backend.execute("CREATE TABLE n.base AS SELECT CASE WHEN i <= 2000 THEN NULL ELSE i END AS id, i AS v "
                    "FROM range(1, 3001) t(i)")
    backend.execute("CREATE TABLE n.cand AS SELECT * FROM n.base")
    d = diff_tables(backend, "n.base", "n.cand", ["id"])
    # 2,000 NULL keys on each side used to join into 4,000,000 pairs.
    assert (d.added_rows, d.removed_rows, d.changed_rows, d.unchanged_rows) == (0, 0, 0, 3000)
    assert d.null_keys_base == 2000 and d.is_identical
    assert d.notes and "paired one to one" in d.notes[0]


def test_a_new_copy_of_a_key_counts_as_added(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders "
                    "UNION ALL SELECT order_id, region, amount, status FROM sales.orders WHERE order_id <= 3")
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"])
    assert (d.added_rows, d.removed_rows, d.changed_rows, d.unchanged_rows) == (3, 0, 0, 1000)
    assert d.duplicate_keys_candidate == 3


def test_changed_copy_of_a_duplicated_key(backend):
    backend.execute("CREATE SCHEMA k")
    backend.execute("CREATE TABLE k.base AS SELECT * FROM (VALUES (1, 'a'), (1, 'b'), (2, 'c')) t(id, v)")
    backend.execute("CREATE TABLE k.cand AS SELECT * FROM (VALUES (1, 'a'), (1, 'z'), (2, 'c')) t(id, v)")
    d = diff_tables(backend, "k.base", "k.cand", ["id"])
    assert d.base_rows == d.candidate_rows == 3
    assert d.added_rows == d.removed_rows == 0
    assert d.changed_rows == 1 and d.unchanged_rows == 2


def test_no_note_without_duplicates(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    assert diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"]).notes == []


# --- audit trail ------------------------------------------------------------------
def _sentry(backend, policy=None):
    cfg = Config.from_dict({"assessment_store": False, "policy": policy,
                            "tables": {"sales.orders": {"keys": ["order_id"]}}})
    return Sentry(cfg, backend=backend, lineage=StaticLineage({}))


def test_assessment_records_when_what_and_who(backend, monkeypatch):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)
    monkeypatch.setenv("BRICKSENTRY_ACTOR", "nightly-orders-job")
    a = _sentry(backend).assess(["sales.orders"], candidates={"sales.orders": "sales_ci.orders"})
    assert a.created_at and a.created_at.endswith("+00:00")
    assert a.bricksentry_version == __version__
    assert a.policy_sha256 == Policy().sha256 and len(a.policy_sha256) == 64
    assert a.triggered_by == "nightly-orders-job"

    d = a.to_dict()
    assert {"created_at", "bricksentry_version", "policy_sha256", "triggered_by"} <= set(d)
    again = Assessment.from_dict(d)
    assert (again.created_at, again.policy_sha256, again.triggered_by) == (
        a.created_at, a.policy_sha256, "nightly-orders-job")

    md = to_markdown(a)
    assert f"bricksentry {__version__}" in md and f"policy `{a.policy_sha256[:12]}`" in md
    assert "triggered by nightly-orders-job" in md and " UTC" in md


def test_github_actor_and_nobody(backend, monkeypatch):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    monkeypatch.delenv("BRICKSENTRY_ACTOR", raising=False)
    monkeypatch.setenv("GITHUB_ACTOR", "octocat")
    a = _sentry(backend).assess(["sales.orders"], candidates={"sales.orders": "sales_ci.orders"})
    assert a.triggered_by == "github:octocat"
    monkeypatch.delenv("GITHUB_ACTOR")
    a = _sentry(backend).assess(["sales.orders"], candidates={"sales.orders": "sales_ci.orders"})
    assert a.triggered_by is None
    assert "triggered by" not in to_markdown(a)


def test_policy_fingerprint_changes_with_the_rules():
    a = Policy({"rules": [{"name": "x", "when": {"duplicate_keys": True}, "then": "block"}]})
    b = Policy({"rules": [{"name": "x", "when": {"duplicate_keys": True}, "then": "warn"}]})
    c = Policy({"rules": [{"then": "block", "when": {"duplicate_keys": True}, "name": "x"}]})  # same, reordered
    assert a.sha256 != b.sha256
    assert a.sha256 == c.sha256


def test_old_saved_assessments_still_load():
    old = {"id": "abc", "verdict": "pass", "tables": [], "notify": []}
    a = Assessment.from_dict(old)
    assert a.created_at is None and a.policy_sha256 is None
