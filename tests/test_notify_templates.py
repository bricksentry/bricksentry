import json

import pytest
from click.testing import CliRunner

from bricksentry import Config, Sentry, notify
from bricksentry.cli import main
from bricksentry.models import Verdict
from bricksentry.policy import Policy
from bricksentry.policy_templates import TEMPLATES, get_template, to_yaml
from bricksentry.preview import PreviewGuardError, rewrite_for_preview

# --- alerts -------------------------------------------------------------------


@pytest.fixture
def risky(sentry, backend):
    """An assessment that needs a human: 10% of rows deleted."""
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders WHERE order_id % 10 <> 0")
    return sentry.assess(["sales.orders"])


@pytest.fixture
def posted(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_post", lambda url, payload, timeout=15: calls.append((url, payload)))
    return calls


def test_settings_read_env_references(monkeypatch):
    monkeypatch.setenv("MY_SLACK", "https://hooks.slack.com/services/X")
    s = notify.NotifySettings.from_config({"slack_webhook": "env:MY_SLACK", "on": "warn"})
    assert s.slack_webhook == "https://hooks.slack.com/services/X"
    assert s.on == Verdict.WARN and s.enabled
    assert notify.NotifySettings.from_config({"teams_webhook": "${MISSING_VAR}"}).teams_webhook is None


def test_settings_fall_back_to_bricksentry_env(monkeypatch):
    monkeypatch.setenv("BRICKSENTRY_TEAMS_WEBHOOK", "https://example.webhook.office.com/x")
    assert notify.NotifySettings.from_config(None).teams_webhook.endswith("/x")


def test_send_slack_and_teams(risky, posted, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/pipelines")
    s = notify.NotifySettings(slack_webhook="https://hooks.slack.com/a", teams_webhook="https://teams.example/b")
    out = notify.send(risky, s, pr="42")
    assert out == ["slack: sent", "teams: sent"]
    slack, teams = posted[0][1], posted[1][1]
    text = json.dumps(slack)
    assert "Needs human review" in text and "row-loss" in text and "acme/pipelines/pull/42" in text
    card = teams["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["actions"][0]["url"].endswith("/acme/pipelines/pull/42")


def test_send_respects_threshold(risky, posted):
    assert risky.verdict == Verdict.NEEDS_HUMAN
    out = notify.send(risky, notify.NotifySettings(on=Verdict.BLOCK, slack_webhook="https://hooks.slack.com/a"))
    assert out and out[0].startswith("no alert") and posted == []


def test_send_never_raises(risky, monkeypatch):
    def boom(url, payload, timeout=15):
        raise OSError("network down")

    monkeypatch.setattr(notify, "_post", boom)
    out = notify.send(risky, notify.NotifySettings(slack_webhook="https://hooks.slack.com/a"))
    assert out == ["slack: failed (OSError: network down)"]


def test_send_refuses_plain_http(risky, posted):
    out = notify.send(risky, notify.NotifySettings(slack_webhook="http://insecure.example/a"))
    assert "must start with https" in out[0] and posted == []


def test_sentry_notify_uses_config(backend, posted, monkeypatch):
    monkeypatch.setenv("SLACK_URL", "https://hooks.slack.com/z")
    cfg = Config.from_dict({
        "tables": {"sales.orders": {"keys": ["order_id"]}},
        "notify": {"slack_webhook": "env:SLACK_URL"},
    })
    s = Sentry(cfg, backend=backend)
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT * FROM sales.orders WHERE order_id % 10 <> 0")
    a = s.assess(["sales.orders"])
    assert s.notify(a) == ["slack: sent"]
    assert posted[0][0] == "https://hooks.slack.com/z"


# --- policy templates -----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_is_a_valid_policy(name):
    Policy(get_template(name))  # raises on unknown conditions or verdicts


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_template_yaml_round_trips(name):
    import yaml

    assert yaml.safe_load(to_yaml(name))["rules"] == get_template(name)["rules"]


def test_builtin_policy_in_config(backend):
    cfg = Config.from_dict({"tables": {"sales.orders": {"keys": ["order_id"]}}, "policy": "builtin:finance"})
    s = Sentry(cfg, backend=backend)
    backend.execute("CREATE TABLE sales_ci.orders AS SELECT order_id, region, amount * 1.01 AS amount, status FROM sales.orders")
    a = s.assess(["sales.orders"])
    assert "numeric-total-moved" in {h.rule for h in a.hits}
    assert a.verdict == Verdict.NEEDS_HUMAN


def test_unknown_template_is_a_clear_error():
    with pytest.raises(KeyError, match="Available"):
        get_template("nope")


def test_cli_policies_and_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["policies"])
    assert r.exit_code == 0 and "finance" in r.output and "builtin:" in r.output
    r = CliRunner().invoke(main, ["init", "--policy", "pii"])
    assert r.exit_code == 0
    assert "pii-table-changed" in (tmp_path / "policy.yml").read_text()


# --- preview refusals ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql,hint",
    [
        ("DROP TABLE sales.orders", "DELETE FROM {{target}}"),
        ("TRUNCATE TABLE sales.orders", "DELETE FROM {{target}}"),
        ("ALTER TABLE sales.orders DROP COLUMN status", "CREATE OR REPLACE TABLE {{target}} AS SELECT"),
        ("GRANT SELECT ON TABLE sales.orders TO analysts", "not data changes"),
    ],
)
def test_refusals_suggest_a_safe_alternative(sql, hint):
    with pytest.raises(PreviewGuardError) as e:
        rewrite_for_preview(sql, "sales.orders", "sandbox.c1", lambda x: x)
    assert hint in str(e.value)


# --- 0.1.7 ------------------------------------------------------------------


def test_companion_package_matches_release():
    import json
    from pathlib import Path

    import bricksentry

    tomllib = pytest.importorskip("tomllib")  # Python 3.11+

    root = Path(__file__).resolve().parents[1]
    v = bricksentry.__version__
    comp = tomllib.load(open(root / "packages/bricksentry-mcp/pyproject.toml", "rb"))["project"]
    assert comp["version"] == v
    assert comp["dependencies"] == [f"bricksentry[mcp,databricks]=={v}"]
    server = json.load(open(root / "server.json"))
    assert server["version"] == v and server["packages"][0]["version"] == v
    assert server["packages"][0]["identifier"] == "bricksentry-mcp"
    marker = f"mcp-name: {server['name']}"
    assert marker in (root / "packages/bricksentry-mcp/README.md").read_text()


def test_mcp_entry_runs_the_mcp_command(monkeypatch):
    import sys

    from bricksentry import cli

    seen = {}
    monkeypatch.setattr(sys, "argv", ["bricksentry-mcp", "--transport", "sse"])
    monkeypatch.setattr(cli, "entry", lambda: seen.setdefault("argv", list(sys.argv)))
    cli.mcp_entry()
    assert seen["argv"] == ["bricksentry-mcp", "mcp", "--transport", "sse"]


def test_demo_without_duckdb_gives_one_clear_line(monkeypatch):
    import builtins

    from bricksentry.demo import run_demo

    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "duckdb":
            raise ImportError("No module named 'duckdb'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ImportError, match="pip install") as e:
        run_demo()
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_agent_safe_template_is_strict_for_agents():
    p = get_template("agent-safe")
    rules = {r["name"]: r["then"] for r in p["rules"]}
    assert rules["dropped-column"] == "block" and rules["row-loss"] == "needs_human"


def test_mcp_server_reports_its_version():
    import asyncio

    pytest.importorskip("mcp")
    import bricksentry
    from bricksentry.mcp_server import build_server

    server = build_server()
    low = getattr(server, "_mcp_server", None) or getattr(server, "_lowlevel_server", None)
    version = getattr(server, "version", None) or getattr(low, "version", None)
    assert version == bricksentry.__version__
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 6
