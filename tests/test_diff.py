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
