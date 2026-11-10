import pytest

from bricksentry.diff import DiffError, diff_tables


def test_identical(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"])
    assert d.is_identical
    assert d.base_rows == d.candidate_rows == 1000
    assert d.unchanged_rows == 1000


def test_added_removed_changed_and_segments(backend):
    backend.execute(
        """
        CREATE TABLE sales_ci.orders AS
        SELECT order_id, region,
               CASE WHEN region = 'APAC' THEN amount * 0.9 ELSE amount END AS amount, status
        FROM sales.orders WHERE order_id <= 990
        UNION ALL SELECT 5000, 'EMEA', 1.0, 'ok'
        """
    )
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"], segment_by=["region"])
    assert d.removed_rows == 10
    assert d.added_rows == 1
    apac_kept = sum(1 for i in range(1, 991) if i % 3 == 0)
    assert d.changed_rows == apac_kept
    amount = next(c for c in d.columns if c.column == "amount")
    assert amount.changed_rows == apac_kept
    assert amount.sum_change_pct < 0
    assert amount.samples and {"order_id", "base_value", "candidate_value"} <= set(amount.samples[0])
    assert d.segments[0].column == "region" and d.segments[0].value == "APAC"


def test_schema_changes_and_type_cast(backend):
    backend.execute(
        "CREATE TABLE sales_ci.orders AS SELECT order_id, region, CAST(amount AS VARCHAR) AS amount, 1 AS new_col FROM sales.orders"
    )
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"])
    assert "status" in d.schema.removed
    assert "new_col" in d.schema.added
    assert "amount" in d.schema.type_changed
    assert not d.is_identical


def test_duplicate_keys(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders UNION ALL SELECT * FROM sales.orders WHERE order_id < 4")
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"])
    assert d.duplicate_keys_base == 0
    assert d.duplicate_keys_candidate == 3


def test_sampling_is_deterministic(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    d1 = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"], sample_pct=20)
    d2 = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"], sample_pct=20)
    assert d1.base_rows == d2.base_rows
    assert 100 < d1.base_rows < 300
    assert d1.is_identical  # same keys sampled on both sides


def test_requires_keys(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    with pytest.raises(DiffError):
        diff_tables(backend, "sales.orders", "sales_ci.orders", [])
    with pytest.raises(DiffError):
        diff_tables(backend, "sales.orders", "sales_ci.orders", ["nope"])


def test_composite_keys(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    backend.execute("UPDATE sales_ci.orders SET status = 'late' WHERE order_id = 7")
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id", "region"])
    assert d.changed_rows == 1


def test_null_keys_are_matched_not_double_counted(backend):
    # Regression: a row whose key is NULL used to count as both added and removed.
    backend.execute("CREATE SCHEMA s")
    backend.execute(
        "CREATE TABLE s.base AS SELECT * FROM (VALUES (NULL, 1.0), ('CA', 2.0), ('TX', 3.0), ('NY', 4.0)) t(state, v)"
    )
    # Unchanged NULL-key row: must be matched, not reported as added + removed.
    backend.execute("CREATE TABLE s.same AS SELECT * FROM s.base")
    d = diff_tables(backend, "s.base", "s.same", ["state"])
    assert (d.added_rows, d.removed_rows, d.changed_rows) == (0, 0, 0)
    assert d.is_identical and d.null_keys_base == 1

    # NULL-key row changed: one changed row, nothing added or removed.
    backend.execute("CREATE TABLE s.chg AS SELECT state, CASE WHEN state IS NULL THEN 9.0 ELSE v END AS v FROM s.base")
    d = diff_tables(backend, "s.base", "s.chg", ["state"])
    assert (d.added_rows, d.removed_rows, d.changed_rows) == (0, 0, 1)

    # DELETE of three rows including the NULL-key row.
    backend.execute("CREATE TABLE s.del AS SELECT * FROM s.base WHERE state = 'NY'")
    d = diff_tables(backend, "s.base", "s.del", ["state"])
    assert (d.added_rows, d.removed_rows) == (0, 3)
