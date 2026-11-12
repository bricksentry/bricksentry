from click.testing import CliRunner

from bricksentry import agent_tools
from bricksentry.cli import main
from bricksentry.demo import run_demo


def test_demo_results(capsys):
    r = run_demo()
    assert r["downstream_count"] == 5
    assert r["preview_verdict"] == "needs_human"
    assert r["production_rows_after_preview"] == 20000
    assert r["buggy_verdict"] == "block"
    assert r["fixed_verdict"] == "needs_human"
    assert r["notify"] == ["finance-analytics", "sales-data-eng"]
    assert r["commit_verdict"] == "block"
    assert r["undo"] == "RESTORE TABLE sales.orders TO VERSION AS OF 0"
    assert r["rows_after_undo"] == 20000
    out = capsys.readouterr().out
    assert "duplicate-keys" in out and "Production still has 20,000 rows" in out
    assert "v2 OPTIMIZE (skipped" in out and "back to 20,000 rows, 0 duplicate keys" in out
    assert "--template-dir templates/job-check" in out
    assert "[5/5]" in out and "[6/" not in out
    assert agent_tools._SENTRY is None  # demo restores global agent state


def test_demo_cli_writes_report(tmp_path):
    report = tmp_path / "report.md"
    res = CliRunner().invoke(main, ["demo", "--report", str(report)])
    assert res.exit_code == 0, res.output
    assert "BLOCK" in res.output
    text = report.read_text()
    assert "Needs human review" in text and "finance-analytics" in text
