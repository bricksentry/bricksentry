"""0.1.21: the CLI preview command reports a refused statement cleanly instead of a traceback."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from test_0114 import FakeDelta

from bricksentry.cli import main


@pytest.mark.parametrize("sql", ["DROP TABLE sales.orders", "TRUNCATE TABLE sales.orders",
                                 "ALTER TABLE sales.orders DROP COLUMN amount",
                                 "GRANT SELECT ON sales.orders TO everyone",
                                 "DELETE FROM sales.orders; DROP TABLE sales.orders"])
def test_refused_statement_exits_2_without_traceback(monkeypatch, tmp_path, sql):
    monkeypatch.setattr("bricksentry.assess.make_backend", lambda spec: FakeDelta())
    cfg = tmp_path / "bricksentry.yml"
    cfg.write_text("sandbox_schema: scratch\nassessment_store: false\nlineage: {type: static}\n")
    r = CliRunner().invoke(main, ["preview", "sales.orders", "--sql", sql, "--key", "order_id", "-c", str(cfg)])
    assert r.exit_code == 2, r.output
    assert "Refused: " in r.output
    assert isinstance(r.exception, SystemExit)  # a clean exit, not an unhandled error


def test_missing_keys_is_a_clean_error(monkeypatch, tmp_path):
    monkeypatch.setattr("bricksentry.assess.make_backend", lambda spec: FakeDelta())
    cfg = tmp_path / "bricksentry.yml"
    cfg.write_text("sandbox_schema: scratch\nassessment_store: false\nlineage: {type: static}\n")
    r = CliRunner().invoke(main, ["preview", "other.table", "--sql", "DELETE FROM other.table", "-c", str(cfg)])
    assert r.exit_code == 2 and "No keys configured for other.table" in r.output
    assert isinstance(r.exception, SystemExit)
