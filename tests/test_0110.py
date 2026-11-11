"""0.1.10: serve MCP over HTTP on Databricks Apps, read-only mode, and the deploy kit."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import bricksentry
from bricksentry import mcp_server
from bricksentry.cli import main
from bricksentry.config import Config

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "deploy" / "databricks-app"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("DATABRICKS_APP_PORT", "BRICKSENTRY_HOST", "BRICKSENTRY_PORT", "BRICKSENTRY_READ_ONLY"):
        monkeypatch.delenv(k, raising=False)


def test_http_address_defaults_and_overrides(monkeypatch):
    assert mcp_server.http_address() == ("127.0.0.1", 8000)
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8123")
    assert mcp_server.http_address() == ("0.0.0.0", 8123)            # Databricks Apps
    monkeypatch.setenv("BRICKSENTRY_PORT", "9000")
    monkeypatch.setenv("BRICKSENTRY_HOST", "10.0.0.5")
    assert mcp_server.http_address() == ("10.0.0.5", 9000)
    assert mcp_server.http_address("127.0.0.1", 7000) == ("127.0.0.1", 7000)  # flags win


def test_read_only_leaves_out_preview_write():
    names = [t.__name__ for t in mcp_server.tools(read_only=True)]
    assert "preview_write" not in names and len(names) == 5
    assert len(mcp_server.tools()) == 6


def test_cli_passes_http_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server, "serve", lambda transport, **kw: seen.update(transport=transport, **kw))
    r = CliRunner().invoke(main, ["mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9100",
                                  "--read-only"])
    assert r.exit_code == 0, r.output
    assert seen == {"transport": "streamable-http", "host": "0.0.0.0", "port": 9100, "read_only": True}


def test_read_only_from_env(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server, "serve", lambda transport, **kw: seen.update(kw))
    monkeypatch.setenv("BRICKSENTRY_READ_ONLY", "true")
    CliRunner().invoke(main, ["mcp"])
    assert seen["read_only"] is True


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(port: int, body: dict, session: str | None = None) -> tuple[str, dict]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Host": "mcp-bricksentry-1234.aws.databricksapps.com",   # what the Apps proxy sends
               "MCP-Protocol-Version": "2025-06-18"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode(), dict(r.headers)


def test_streamable_http_server_like_databricks_apps(tmp_path):
    pytest.importorskip("mcp")
    cfg = tmp_path / "bricksentry.yml"
    cfg.write_text("backend: {type: duckdb}\nassessment_store: false\n")
    port = _free_port()
    env = dict(os.environ, DATABRICKS_APP_PORT=str(port))
    p = subprocess.Popen([sys.executable, "-m", "bricksentry.cli", "mcp", "--transport", "streamable-http",
                          "--read-only", "--config", str(cfg)], env=env, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        init, headers = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert '"bricksentry"' in init and bricksentry.__version__ in init
        session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        listed, _ = _post(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session)
        for name in ("assess_change", "safe_to_ship", "whats_downstream", "rollback_plan", "explain_diff"):
            assert f'"{name}"' in listed
        assert '"preview_write"' not in listed
    finally:
        p.terminate()
        err = p.communicate(timeout=15)[1]
    assert f"on http://0.0.0.0:{port}/mcp" in err


# --- the deploy kit -------------------------------------------------------------
def test_app_yaml_starts_a_valid_command():
    app = yaml.safe_load((KIT / "app.yaml").read_text())
    cmd = app["command"]
    assert cmd[:2] == ["bricksentry", "mcp"] and "--read-only" in cmd
    # every option in the command is one the CLI accepts
    mcp_cmd = main.commands["mcp"]
    ctx = mcp_cmd.make_context("mcp", list(cmd[2:]))
    assert ctx.params["transport"] == "streamable-http"
    assert ctx.params["config_path"] == "bricksentry.yml"
    env = {e["name"]: e for e in app["env"]}
    assert env["DATABRICKS_WAREHOUSE_ID"]["valueFrom"] == "sql-warehouse"


def test_bundle_declares_the_resource_app_yaml_uses():
    bundle = yaml.safe_load((KIT / "databricks.yml").read_text())
    app = bundle["resources"]["apps"]["bricksentry_mcp"]
    names = {r["name"] for r in app["resources"]}
    assert "sql-warehouse" in names
    assert app["resources"][0]["sql_warehouse"]["permission"] == "CAN_USE"


def test_requirements_pin_this_release():
    assert (KIT / "requirements.txt").read_text().strip() == f"bricksentry[databricks,mcp]=={bricksentry.__version__}"


def test_kit_config_loads_cleanly():
    data = yaml.safe_load((KIT / "bricksentry.yml").read_text())
    cfg = Config.from_dict(data)
    assert cfg.warnings == []
    assert cfg.policy is not None and cfg.spec("main.sales.orders").keys == ["order_id"]


def test_grants_cover_reads_and_lineage():
    sql = (KIT / "grants.sql").read_text()
    for needed in ("GRANT SELECT ON SCHEMA", "system.access.table_lineage", "`<app-sp>`"):
        assert needed in sql


def test_every_lineage_key_the_code_reads_is_known():
    """Config typo warnings must never flag a key that bricksentry actually uses."""
    import re

    from bricksentry.config import LINEAGE_KEYS, TOP_LEVEL_KEYS

    used = set(re.findall(r'cfg\.get\("([a-z_]+)"', (ROOT / "src/bricksentry/lineage.py").read_text()))
    used |= set(re.findall(r'lineage\.get\("([a-z_]+)"', (ROOT / "src/bricksentry/assess.py").read_text()))
    assert used and used <= LINEAGE_KEYS, used - LINEAGE_KEYS
    top = set(re.findall(r'data\.get\("([a-z_]+)"', (ROOT / "src/bricksentry/config.py").read_text()))
    assert top - {"notify", "tables"} <= TOP_LEVEL_KEYS, top - TOP_LEVEL_KEYS


def test_documented_lineage_config_has_no_warnings():
    from bricksentry.config import config_warnings

    assert config_warnings({"lineage": {"type": "databricks", "depth": 3, "lookback_days": 90,
                                        "exclude": ["*_sandbox.*"], "exclude_dropped": True}}) == []
