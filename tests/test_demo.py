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
    out = capsys.readouterr().out
    assert "duplicate-keys" in out and "Production still has 20,000 rows" in out
    assert agent_tools._SENTRY is None  # demo restores global agent state


def test_demo_cli_writes_report(tmp_path):
    report = tmp_path / "report.md"
    res = CliRunner().invoke(main, ["demo", "--report", str(report)])
    assert res.exit_code == 0, res.output
    assert "BLOCK" in res.output
    text = report.read_text()
    assert "Needs human review" in text and "finance-analytics" in text
