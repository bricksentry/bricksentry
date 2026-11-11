"""0.1.9: a failed lineage lookup is reported as unknown blast radius, not as a failed diff."""

from __future__ import annotations

from bricksentry import Config, Sentry
from bricksentry.policy import KNOWN_CONDITIONS
from bricksentry.policy_templates import TEMPLATES


def _broken_lineage(sentry, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("PERMISSION_DENIED: User does not have SELECT on table system.access.table_lineage\n"
                              "  at line 1\n  very long stack")
    monkeypatch.setattr(sentry.lineage, "downstream", boom)


def _candidate(backend):
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders")
    backend.execute("UPDATE sales_ci.orders SET amount = amount + 1 WHERE order_id <= 5")


def test_lineage_failure_is_a_warning_not_a_diff_failure(sentry, backend, monkeypatch):
    _candidate(backend)
    _broken_lineage(sentry, monkeypatch)
    a = sentry.assess(["sales.orders"])
    rules = {h.rule: h for h in a.hits}
    assert "diff-failed" not in rules
    assert rules["lineage-unavailable"].verdict.value == "warn"
    assert "PERMISSION_DENIED" in rules["lineage-unavailable"].reason
    assert "\n" not in rules["lineage-unavailable"].reason          # one line, no stack
    assert a.verdict.value == "warn"
    t = a.tables[0]
    assert t.diff is not None and t.diff.changed_rows == 5          # the diff still ran
    assert "blast radius unknown" in t.summary


def test_strict_template_asks_a_human_when_lineage_is_unknown(backend, config, monkeypatch):
    _candidate(backend)
    cfg = Config.from_dict({"tables": {"sales.orders": {"keys": ["order_id"]}}, "policy": "builtin:strict",
                            "lineage": {"type": "static", "edges": {}}})
    s = Sentry(cfg, backend=backend)
    _broken_lineage(s, monkeypatch)
    a = s.assess(["sales.orders"])
    assert any(h.rule == "lineage-unavailable" and h.verdict.value == "needs_human" for h in a.hits)


def test_every_template_handles_lineage_errors():
    assert "lineage_error" in KNOWN_CONDITIONS
    for name, t in TEMPLATES.items():
        conds = [r["when"] for r in t["policy"]["rules"]]
        assert {"lineage_error": True} in conds, name


def test_working_lineage_has_no_lineage_hit(sentry, backend):
    _candidate(backend)
    a = sentry.assess(["sales.orders"])
    assert all(h.rule != "lineage-unavailable" for h in a.hits)
