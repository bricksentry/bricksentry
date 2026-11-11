"""0.1.8: measures, alert mentions/links/robustness, test-alert, config warnings."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from click.testing import CliRunner

from bricksentry import notify
from bricksentry.cli import main
from bricksentry.config import Config, config_warnings
from bricksentry.explain import explain_diff
from bricksentry.measures import is_measure, looks_like_measure
from bricksentry.models import Assessment, ColumnChange, RuleHit, SchemaChange, TableAssessment, TableDiff, Verdict
from bricksentry.policy import Policy

URL = "https://hooks.slack.com/services/T000/B000/SECRETSECRET"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SERVER_URL", "DATABRICKS_RUNTIME_VERSION",
              "BRICKSENTRY_RUN_URL", "BRICKSENTRY_SLACK_WEBHOOK", "BRICKSENTRY_TEAMS_WEBHOOK",
              "BRICKSENTRY_NOTIFY_ON", "BRICKSENTRY_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)


# --- measures ---------------------------------------------------------------
@pytest.mark.parametrize("col,ok", [
    ("bed_count", True), ("amount", True), ("num_orders", True), ("totalRevenue", True),
    ("established_year", False), ("rating", False), ("customer_id", False), ("orderYear", False),
    ("lat", False), ("zip", False), ("discount_pct", False), ("hospital_rank", False),
])
def test_looks_like_measure(col, ok):
    assert looks_like_measure(col) is ok


def test_is_measure_config_wins():
    assert is_measure("rating", measures="all")
    assert is_measure("rating", measures=["Rating"])
    assert not is_measure("amount", measures=["revenue"])
    assert not is_measure("order_total", keys=["order_total"])


def _diff(cols: dict[str, float]) -> TableDiff:
    return TableDiff(
        table="main.h.hospitals", base="b", candidate="c", keys=["hospital_id"],
        base_rows=1000, candidate_rows=900, added_rows=0, removed_rows=100, changed_rows=0, unchanged_rows=900,
        schema=SchemaChange(),
        columns=[ColumnChange(column=c, changed_rows=0, base_sum=100.0, candidate_sum=100.0 * (1 + p / 100))
                 for c, p in cols.items()],
    )


def test_numeric_total_rule_skips_noise_columns():
    d = _diff({"bed_count": -10.0, "established_year": -9.95, "rating": -9.94, "hospital_id": -9.9})
    rules = {"rules": [{"name": "numeric-total-moved", "when": {"sum_change_pct_gt": 5}, "then": "warn"}]}
    v, hits = Policy(rules).evaluate("main.h.hospitals", d, [], {})
    assert v == Verdict.WARN
    assert "bed_count" in hits[0].reason
    assert "established_year" not in hits[0].reason and "rating" not in hits[0].reason


def test_numeric_total_rule_no_hit_when_only_noise_moved():
    d = _diff({"bed_count": 0.0, "established_year": -9.95})
    rules = {"rules": [{"name": "t", "when": {"sum_change_pct_gt": 5}, "then": "warn"}]}
    assert Policy(rules).evaluate("t", d, [], {})[0] == Verdict.PASS


def test_named_column_rule_still_checks_rating():
    d = _diff({"rating": -9.94})
    rules = {"rules": [{"name": "r", "when": {"column": "rating", "sum_change_pct_gt": 5}, "then": "needs_human"}]}
    assert Policy(rules).evaluate("t", d, [], {})[0] == Verdict.NEEDS_HUMAN


def test_measures_all_restores_old_behaviour():
    d = _diff({"established_year": -9.95})
    rules = {"rules": [{"name": "t", "when": {"sum_change_pct_gt": 5}, "then": "warn"}]}
    assert Policy(rules).evaluate("t", d, [], {}, measures="all")[0] == Verdict.WARN


def test_explain_lists_only_measures():
    text = explain_diff(_diff({"bed_count": -10.0, "established_year": -9.95}))
    assert "sum(bed_count)" in text and "established_year" not in text


def test_table_measures_from_config():
    c = Config.from_dict({"tables": {"main.s.o": {"keys": ["id"], "measures": ["amount"]},
                                     "main.s.p": {"keys": ["id"], "measures": "all"}}})
    assert c.spec("main.s.o").measures == ["amount"]
    assert c.spec("main.s.p").measures == "all"
    assert c.spec("main.s.x").measures is None


# --- config warnings ----------------------------------------------------------
def test_config_warnings_catch_typos():
    w = config_warnings({
        "backend": {"type": "databricks"},
        "sample_pc": 10,
        "notify": {"slack_webhok": "env:X", "teams_webhook": "https://example.webhook.office.com/x"},
        "tables": {"orders": {"key": ["id"], "measure": ["amount"]}, "main.s.t": {}},
    })
    text = "\n".join(w)
    assert "'sample_pc'" in text and "did you mean 'sample_pct'" in text
    assert "did you mean 'slack_webhook'" in text
    assert "did you mean 'measures'" in text
    assert "full catalog.schema.table" in text
    assert "main.s.t: no keys set" in text
    assert "teams_webhook: the webhook URL is written in the file" in text


def test_clean_config_has_no_warnings():
    assert config_warnings({"backend": {"type": "duckdb"}, "tables": {"sales.orders": {"keys": ["id"]}},
                            "notify": {"on": "block", "slack_webhook": "env:S", "mentions": {"a": "U1234567"}}}) == []


def test_config_load_warns_in_python(tmp_path):
    p = tmp_path / "bricksentry.yml"
    p.write_text("sampel_pct: 10\n")
    with pytest.warns(UserWarning, match="sampel_pct"):
        Config.load(p)
    assert Config.load(p, warn=False).warnings


def test_cli_prints_config_warnings(tmp_path):
    p = tmp_path / "bricksentry.yml"
    p.write_text("notify: {slack_webhok: env:X}\n")
    r = CliRunner().invoke(main, ["test-alert", "-c", str(p)])
    assert "did you mean 'slack_webhook'" in r.output


# --- alerts -------------------------------------------------------------------
def _assessment(owner="finance-analytics", reason="removed 9.95% of rows (> 1%)", n_hits=1) -> Assessment:
    hits = [RuleHit("row-loss", Verdict.NEEDS_HUMAN, "main.h.hospitals_clean", reason) for _ in range(n_hits)]
    ta = TableAssessment("main.h.hospitals_clean", None, [], owner, {}, None, Verdict.NEEDS_HUMAN, hits)
    return Assessment("abc123", [ta], Verdict.NEEDS_HUMAN, notify=[owner] if owner else [])


def test_slack_mentions_users_groups_and_unmapped():
    a = _assessment()
    a.notify = ["finance-analytics", "Jane@Acme.com", "nobody@acme.com", "oncall"]
    m = {"finance-analytics": "S0123ABCD", "jane@acme.com": "U0456EFGH", "oncall": "here"}
    body = notify.slack_payload(a, mentions=m)["blocks"][1]["text"]["text"]
    assert "<!subteam^S0123ABCD>" in body and "<@U0456EFGH>" in body and "<!here>" in body
    assert "nobody@acme.com" in body


def test_settings_lowercase_mention_keys():
    s = notify.NotifySettings.from_config({"slack_webhook": URL, "mentions": {"Jane@Acme.com": "U0456EFGH"}})
    assert s.mentions == {"jane@acme.com": "U0456EFGH"}


def test_slack_escapes_control_characters():
    a = _assessment(reason="value <script> & <@U999> injected")
    body = notify.slack_payload(a)["blocks"][1]["text"]["text"]
    assert "<script>" not in body and "&lt;script&gt;" in body and "&amp;" in body
    assert "<@U999>" not in body


def test_long_reason_and_many_hits_are_clipped():
    a = _assessment(reason="x" * 2000, n_hits=9)
    p = notify.slack_payload(a)
    body = p["blocks"][1]["text"]["text"]
    assert len(body) < 3000 and "…and 4 more" in body
    assert p["blocks"][2]["type"] == "context" and "abc123" in p["blocks"][2]["elements"][0]["text"]


def test_teams_uses_plain_text_runs():
    p = notify.teams_payload(_assessment())
    card = p["attachments"][0]["content"]
    texts = [i["text"] for b in card["body"] for i in b.get("inlines", [])]
    assert any("main.h.hospitals_clean" in t for t in texts)  # underscores survive (no markdown)
    assert all(b["type"] == "RichTextBlock" for b in card["body"])


def test_run_link_priority(monkeypatch):
    assert notify.run_link() is None
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/data")
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    assert notify.run_link()[0] == "https://github.com/acme/data/actions/runs/77"
    assert notify.run_link(pr="5") == ("https://github.com/acme/data/pull/5", "Open pull request")
    assert notify.run_link(pr="5", run_url="https://ci.example.com/r/1") == ("https://ci.example.com/r/1", "Open run")
    # a non-http(s) run_url is ignored, so the GitHub link is used instead
    assert notify.run_link(run_url="javascript:alert(1)") == ("https://github.com/acme/data/actions/runs/77", "Open GitHub run")


def test_databricks_job_run_link(monkeypatch):
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "16.4")
    monkeypatch.setattr(notify, "_databricks_context", lambda: {
        "jobId": "111", "multitaskParentRunId": "222", "currentRunId": "333",
        "browserHostName": "dbc-1.cloud.databricks.com", "orgId": "42"})
    assert notify.run_link() == ("https://dbc-1.cloud.databricks.com/jobs/111/runs/222?o=42", "Open Databricks run")
    body = notify.slack_payload(_assessment())["blocks"][1]["text"]["text"]
    assert "<https://dbc-1.cloud.databricks.com/jobs/111/runs/222?o=42|Open Databricks run>" in body


def test_databricks_notebook_without_job_has_no_link(monkeypatch):
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "16.4")
    monkeypatch.setattr(notify, "_databricks_context", lambda: {"browserHostName": "dbc-1.cloud.databricks.com"})
    assert notify.run_link() is None

    def boom():
        raise RuntimeError("no dbutils on serverless")
    monkeypatch.setattr(notify, "_databricks_context", boom)
    assert notify.run_link() is None


def _http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError(URL, code, "err", headers or {}, io.BytesIO(body))


def test_retry_once_on_429_then_send(monkeypatch):
    calls = []

    def post(url, payload, timeout=15):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, headers={"Retry-After": "2"})
    monkeypatch.setattr(notify, "_post", post)
    out = notify.send(_assessment(), notify.NotifySettings(slack_webhook=URL))
    assert out == ["slack: sent"] and len(calls) == 2


def test_no_retry_on_404_and_url_is_redacted(monkeypatch):
    calls = []

    def post(url, payload, timeout=15):
        calls.append(1)
        raise _http_error(404, body=f"no_service {URL}".encode())
    monkeypatch.setattr(notify, "_post", post)
    out = notify.send(_assessment(), notify.NotifySettings(slack_webhook=URL))
    assert len(calls) == 1
    assert out[0].startswith("slack: failed (HTTP 404") and "revoked" in out[0]
    assert "SECRET" not in out[0] and "hooks.slack.com" not in out[0]


def test_connection_error_retries_then_reports(monkeypatch):
    calls = []

    def post(url, payload, timeout=15):
        calls.append(1)
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(notify, "_post", post)
    out = notify.send(_assessment(), notify.NotifySettings(slack_webhook=URL))
    assert len(calls) == 2 and out == ["slack: failed (could not connect: timed out)"]


def test_bad_notify_on_is_a_clear_error():
    with pytest.raises(ValueError, match="notify.on must be one of"):
        notify.NotifySettings.from_config({"on": "high"})
    with pytest.raises(ValueError, match="mentions"):
        notify.NotifySettings.from_config({"mentions": ["U1"]})


def test_payload_is_valid_json_both_channels():
    a = _assessment()
    json.dumps(notify.slack_payload(a))
    json.dumps(notify.teams_payload(a))


# --- test-alert ---------------------------------------------------------------
def test_test_alert_needs_a_webhook():
    r = CliRunner().invoke(main, ["test-alert"])
    assert r.exit_code != 0 and "no webhook configured" in r.output


def test_test_alert_sends_labelled_sample(monkeypatch):
    sent = []
    monkeypatch.setenv("BRICKSENTRY_SLACK_WEBHOOK", URL)
    monkeypatch.setattr(notify, "_post", lambda url, payload, timeout=15: sent.append(payload))
    r = CliRunner().invoke(main, ["test-alert", "--verdict", "block", "--owner", "data-team"])
    assert r.exit_code == 0, r.output
    assert "slack: sent" in r.output
    text = json.dumps(sent[0])
    assert "test alert" in text and "Blocked" in text and "data-team" in text


def test_test_alert_exit_1_on_failure(monkeypatch):
    monkeypatch.setenv("BRICKSENTRY_SLACK_WEBHOOK", URL)

    def post(url, payload, timeout=15):
        raise _http_error(403)
    monkeypatch.setattr(notify, "_post", post)
    r = CliRunner().invoke(main, ["test-alert"])
    assert r.exit_code == 1 and "slack: failed (HTTP 403" in r.output
