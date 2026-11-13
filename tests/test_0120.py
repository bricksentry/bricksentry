"""0.1.20: warn when newer data changes exist, since the RESTORE would undo them too."""

from __future__ import annotations

import json

from click.testing import CliRunner
from test_0114 import HISTORY, FakeDelta, sentry

from bricksentry.cli import main
from bricksentry.history import Change, newer_data_changes, rollback_warning
from bricksentry.models import TableAssessment
from bricksentry.notify import slack_payload, teams_payload
from bricksentry.report import to_markdown


def test_newer_data_changes_skips_maintenance_and_sorts():
    hist = [{"version": 7, "operation": "WRITE", "timestamp": "t7"}, {"version": 6, "operation": "OPTIMIZE"},
            {"version": 5, "operation": "MERGE", "timestamp": "t5"}, {"version": 4, "operation": "WRITE"}]
    assert [h["version"] for h in newer_data_changes(hist, 4)] == [5, 7]
    assert newer_data_changes(hist, 7) == []


def test_warning_text_lists_newest_first_and_caps():
    c = Change("t", 2, 1, "DELETE", "", [2], newer=[{"version": v, "operation": "WRITE", "timestamp": ""}
                                                   for v in range(3, 8)])
    w = rollback_warning(c)
    assert w.startswith("Newer data changes exist: v7 (WRITE), v6 (WRITE), v5 (WRITE) and 2 more.")
    assert "Restoring to v1 also undoes them" in w
    assert rollback_warning(Change("t", 2, 1, "DELETE", "", [2])) is None


def test_latest_change_has_no_warning():
    a, change = sentry().check_change("sales.orders")
    assert change.newer == [] and a.tables[0].rollback_warning is None
    assert "Before you restore" not in to_markdown(a)


def test_older_version_warns_about_newer_change():
    a, change = sentry().check_change("sales.orders", version=2)
    t = a.tables[0]
    assert [h["version"] for h in change.newer] == [3]  # the OPTIMIZE at v4 is not a data change
    assert t.rollback == 'RESTORE TABLE "sales"."orders" TO VERSION AS OF 1'
    assert t.rollback_warning.startswith("Newer data changes exist: v3 (MERGE, 2026-11-11 10:00:00).")
    assert "Restoring to v1 also undoes it. Re-run that write" in t.rollback_warning
    md = to_markdown(a)
    assert md.index("Before you restore") < md.index("RESTORE TABLE")
    assert "Before you restore sales.orders" in json.dumps(slack_payload(a))
    assert "Before you restore sales.orders" in json.dumps(teams_payload(a))
    assert TableAssessment.from_dict(t.to_dict()).rollback_warning == t.rollback_warning


def test_write_landing_during_the_check_is_caught():
    class Busy(FakeDelta):
        reads = 0

        def history(self, table, limit=50):
            self.reads += 1
            if self.reads == 1:
                return HISTORY
            return [{"version": 5, "timestamp": "2026-11-11 10:10:00", "operation": "WRITE"}, *HISTORY]

    a, change = sentry(Busy()).check_change("sales.orders")
    assert change.version == 3 and [h["version"] for h in change.newer] == [5]
    assert "v5 (WRITE" in a.tables[0].rollback_warning


def test_cli_text_and_json(monkeypatch, tmp_path):
    db = FakeDelta()
    monkeypatch.setattr("bricksentry.assess.make_backend", lambda spec: db)
    cfg = tmp_path / "bricksentry.yml"
    cfg.write_text("sandbox_schema: scratch\nassessment_store: false\nlineage: {type: static}\n")
    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", str(cfg), "--no-notify",
                                  "--version", "2"])
    assert r.exit_code in (0, 1), r.output
    assert r.output.index("Warning before you undo: Newer data changes exist: v3") < r.output.index("Undo: RESTORE")
    r = CliRunner().invoke(main, ["check-last-change", "sales.orders", "-c", str(cfg), "--no-notify",
                                  "--version", "2", "--format", "json"])
    d = json.loads(r.output)
    assert d["change"]["newer_changes"][0]["version"] == 3
    assert d["tables"][0]["rollback_warning"].startswith("Newer data changes exist")
