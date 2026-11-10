"""v0.1.4: sampled diffs can't miss a change, and assessments outlive their Sentry."""

from bricksentry import Config, Sentry, agent_tools
from bricksentry.diff import diff_tables


def _one_row_outside_sample(backend, pct):
    """The key of a row that a pct% sample does not include."""
    pred = backend.sample_predicate(["order_id"], pct)
    return backend.scalar(f"SELECT MIN(order_id) FROM sales.orders WHERE NOT ({pred})")


def test_small_change_outside_sample_is_caught(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    k = _one_row_outside_sample(backend, 10)
    backend.execute(f"DELETE FROM sales_ci.orders WHERE order_id = {k}")

    unguarded = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"],
                            sample_pct=10, sample_guard=False)
    assert unguarded.is_identical  # the old behaviour: the sample cannot see it

    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"], sample_pct=10)
    assert not d.is_identical
    assert d.removed_rows == 1
    assert d.base_rows == 1000 and d.candidate_rows == 999  # full counts, not the sample
    assert d.sampled_pct is None and d.sample_note


def test_value_change_outside_sample_is_caught(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    k = _one_row_outside_sample(backend, 10)
    backend.execute(f"UPDATE sales_ci.orders SET amount = amount + 1 WHERE order_id = {k}")
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"], sample_pct=10)
    assert d.changed_rows == 1


def test_identical_tables_stay_sampled(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    d = diff_tables(backend, "sales.orders", "sales_ci.orders", ["order_id"], sample_pct=10)
    assert d.is_identical and d.sampled_pct == 10 and d.sample_note is None


def test_guard_never_passes_a_changed_table_via_sentry(backend, config):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    k = _one_row_outside_sample(backend, 10)
    backend.execute(f"DELETE FROM sales_ci.orders WHERE order_id = {k}")
    config.sample_pct = 10
    s = Sentry(config, backend=backend)
    a = s.assess(["sales.orders"], candidates={"sales.orders": "sales_ci.orders"})
    assert a.verdict.value != "pass"  # the certified downstream table makes it needs_human
    assert "re-run on all rows" in a.tables[0].summary


def test_assessment_found_from_another_sentry(backend, config, tmp_path):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    backend.execute("DELETE FROM sales_ci.orders WHERE order_id <= 50")
    config.assessment_store = str(tmp_path / "store")
    a = Sentry(config, backend=backend).assess(["sales.orders"], candidates={"sales.orders": "sales_ci.orders"})

    other = Sentry(config, backend=backend)  # a fresh instance: nothing in memory
    agent_tools.set_sentry(other)
    try:
        exp = agent_tools.explain_diff(assessment_id=a.id)
        assert exp["assessment_id"] == a.id and "50 rows removed" in exp["explanations"]["sales.orders"]
        gate = agent_tools.safe_to_ship(a.id)
        assert gate["verdict"] == a.verdict.value
        assert gate["rule_hits"] == [h.to_dict() for h in a.hits]
        loaded = other.get_assessment(a.id)
        assert loaded.tables[0].diff.removed_rows == 50
        assert agent_tools.safe_to_ship("../../etc/passwd").get("error")
    finally:
        agent_tools.set_sentry(None)


def test_store_can_be_disabled(backend, config, tmp_path):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    config.assessment_store = False
    a = Sentry(config, backend=backend).assess(["sales.orders"], candidates={"sales.orders": "sales_ci.orders"})
    assert Sentry(config, backend=backend).get_assessment(a.id) is None


def test_config_reads_new_keys():
    c = Config.from_dict({"sample_guard": False, "assessment_store": "state/assess"})
    assert c.sample_guard is False and str(c.store_dir()).endswith("state/assess")
