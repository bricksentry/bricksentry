import pytest

from bricksentry import Verdict
from bricksentry.config import Config
from bricksentry.policy import Policy, PolicyError
from bricksentry.report import MARKER, to_markdown


def test_clean_change_passes_but_flags_dashboard(sentry, backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    a = sentry.assess(["sales.orders"])
    # identical data: certified downstream still requires a human by default policy
    assert a.verdict == Verdict.NEEDS_HUMAN
    assert {h.rule for h in a.hits} == {"certified-downstream"}
    assert a.notify == ["finance-team", "sales-eng"]


def test_duplicate_keys_block(sentry, backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders UNION ALL SELECT * FROM sales.orders LIMIT 1010")
    a = sentry.assess(["sales.orders"])
    assert a.verdict == Verdict.BLOCK
    d = a.to_dict()
    assert d["safe_to_ship"] is False and d["risk_tier"] == "blocked"


def test_missing_candidate_needs_human(sentry):
    a = sentry.assess(["sales.orders"])
    assert a.verdict == Verdict.NEEDS_HUMAN
    assert any(h.rule == "diff-failed" for h in a.hits)


def test_custom_policy_and_column_rule(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT order_id, region, amount * 1.03 AS amount, status FROM sales.orders")
    from bricksentry import Sentry

    cfg = Config.from_dict(
        {
            "tables": {"sales.orders": {"keys": ["order_id"]}},
            "lineage": {"type": "static"},
            "policy": {
                "default": "pass",
                "rules": [{"name": "revenue", "when": {"column": "amount", "sum_change_pct_gt": 2}, "then": "needs_human"}],
            },
        }
    )
    a = Sentry(cfg, backend=backend).assess(["sales.orders"])
    assert a.verdict == Verdict.NEEDS_HUMAN
    assert "sum(amount) +3.00%" in a.hits[0].reason


def test_unknown_condition_rejected():
    with pytest.raises(PolicyError):
        Policy({"rules": [{"name": "x", "when": {"made_up": 1}, "then": "warn"}]})


def test_markdown_report(sentry, backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders WHERE order_id > 50")
    md = to_markdown(sentry.assess(["sales.orders"]))
    assert MARKER in md
    assert "Needs human review" in md
    assert "Blast radius" in md and "finance-team" in md


def test_candidate_pattern():
    cfg = Config.from_dict({"candidate_pattern": "{catalog}.{schema}_pr_{pr}.{table}"})
    assert cfg.candidate_for("main.sales.orders", pr="42") == "main.sales_pr_42.orders"


def test_changed_tables_from_files():
    from bricksentry.changes import tables_for_files

    cfg = Config.from_dict({"tables": {"main.sales.orders": {"keys": ["id"], "paths": ["src/orders/**", "resources/orders.yml"]}}})
    assert tables_for_files(cfg, ["src/orders/transform.py"]) == ["main.sales.orders"]
    assert tables_for_files(cfg, ["README.md"]) == []
