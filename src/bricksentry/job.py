"""Run bricksentry as a Databricks job task: check what the pipeline just did to its tables.

Add it as a task after your pipeline task (or as a job with a table update trigger)::

    python -m bricksentry.job --tables main.sales.orders --warehouse-id abc123 \\
        --since {{job.start_time.iso_datetime}} --slack-secret bricksentry/slack_webhook

For each table it runs ``check_change`` (see :mod:`bricksentry.history`), prints the verdict and
the RESTORE that undoes the change, sends the Slack / Teams alert, and exits 1 when the worst
verdict is at least ``--fail-on`` so the tasks that depend on this one don't run.
The bundle template in ``templates/job-check`` sets all of this up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from bricksentry.models import Verdict

FAIL_CHOICES = [v.value for v in Verdict] + ["never"]


def parse_tables(value: str | None) -> list[str]:
    """Comma-separated names, or the JSON list Databricks gives for
    ``{{job.trigger.table_update.updated_tables}}``."""
    v = (value or "").strip()
    if not v:
        return []
    if v.startswith("["):
        items = json.loads(v)
    else:
        items = v.split(",")
    out: list[str] = []
    for t in items:
        t = str(t).strip().strip("`")
        if t and t not in out:
            out.append(t)
    return out


def parse_keys(values: list[str]) -> dict[str, list[str]]:
    """``--key order_id`` (every table) or ``--key main.sales.orders=order_id,day``."""
    out: dict[str, list[str]] = {}
    for v in values or []:
        table, _, cols = v.rpartition("=")
        out[table.strip() or "*"] = [c.strip() for c in cols.split(",") if c.strip()]
    return out


def read_secret(ref: str | None) -> str | None:  # pragma: no cover - needs Databricks
    """``scope/key`` from a Databricks secret scope."""
    if not ref:
        return None
    scope, _, key = ref.partition("/")
    if not (scope and key):
        raise SystemExit(f"--*-secret must be scope/key, got {ref!r}")
    try:
        from databricks.sdk.runtime import dbutils

        return dbutils.secrets.get(scope, key)
    except Exception:
        import base64

        from databricks.sdk import WorkspaceClient

        return base64.b64decode(WorkspaceClient().secrets.get_secret(scope, key).value).decode()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bricksentry.job", description=__doc__.split("\n\n")[0])
    p.add_argument("--tables", required=True,
                   help="Tables to check: a,b,c or a JSON list ({{job.trigger.table_update.updated_tables}})")
    p.add_argument("--warehouse-id", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"))
    p.add_argument("--config", default=None, help="Optional bricksentry.yml (keys, policy, lineage, notify)")
    p.add_argument("--key", action="append", default=[], help="order_id or TABLE=col1,col2 (repeatable)")
    p.add_argument("--keys", default=None,
                   help="Several at once, separated by ';': 'main.s.orders=order_id; main.s.items=order_id,line'")
    p.add_argument("--policy", default=None, help="Built-in policy name (default, strict, finance, pii, ...)")
    p.add_argument("--scratch-schema", default=None, help="catalog.schema for the temporary shallow clones")
    p.add_argument("--default-tables", default=None,
                   help="Used when --tables is empty or an unresolved {{...}} value (e.g. a manual run)")
    p.add_argument("--since", default=None, help="Check every change after this time (the job start time)")
    p.add_argument("--since-minutes", type=float, default=None,
                   help="Check every change in the last N minutes (for jobs started by a table update trigger)")
    p.add_argument("--fail-on", default="block", choices=FAIL_CHOICES)
    p.add_argument("--sample-pct", type=float, default=None)
    p.add_argument("--slack-secret", default=None, help="scope/key of a Slack incoming-webhook URL")
    p.add_argument("--teams-secret", default=None, help="scope/key of a Teams workflow webhook URL")
    p.add_argument("--notify-on", default=None, choices=[v.value for v in Verdict])
    p.add_argument("--run-url", default=None, help="Link on the alert, e.g. {{workspace.url}}/jobs/...")
    return p


def make_sentry(args: argparse.Namespace, backend: Any = None):
    from bricksentry.assess import Sentry
    from bricksentry.config import Config

    if args.config:
        cfg = Config.load(args.config, warn=False)
    else:
        cfg = Config.from_dict({"assessment_store": False})
    if args.warehouse_id:
        cfg.backend = {**(cfg.backend or {}), "type": "databricks", "warehouse_id": args.warehouse_id}
    elif not cfg.backend or cfg.backend.get("type") == "duckdb":
        cfg.backend = {"type": "databricks"}
    if args.policy:
        name = args.policy if args.policy.startswith("builtin:") else f"builtin:{args.policy}"
        cfg.policy = Config.from_dict({"policy": name}).policy
    if args.scratch_schema:
        cfg.sandbox_schema = args.scratch_schema
    notify = dict(cfg.notify or {})
    slack, teams = read_secret(args.slack_secret), read_secret(args.teams_secret)
    if slack:
        notify["slack_webhook"] = slack
    if teams:
        notify["teams_webhook"] = teams
    if args.notify_on:
        notify["on"] = args.notify_on
    if args.run_url and str(args.run_url).startswith(("https://", "http://")):
        notify["run_url"] = args.run_url
    cfg.notify = notify
    return Sentry(cfg, backend=backend)


def run(argv: list[str] | None = None, backend: Any = None, out=print) -> int:
    """Returns the exit code (0 ok, 1 verdict at or above --fail-on, 2 error)."""
    from bricksentry.history import NoChangeError

    args = build_parser().parse_args(argv)
    try:
        tables = [] if "{{" in (args.tables or "") else parse_tables(args.tables)
    except ValueError:
        tables = []
    tables = tables or parse_tables(args.default_tables)
    since = args.since
    if since and "{{" in str(since):
        since = None
    if not since and args.since_minutes:
        since = (datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)).isoformat()
    if not tables:
        out("bricksentry: no tables to check.")
        return 0
    keys = parse_keys(args.key + [k for k in (args.keys or "").split(";") if k.strip()])
    s = make_sentry(args, backend=backend)
    worst: Verdict | None = None
    errors = 0
    for table in tables:
        try:
            a, change = s.check_change(table, keys=keys.get(table) or keys.get("*"), since=since,
                                       sample_pct=args.sample_pct)
        except NoChangeError as e:
            out(f"{table}: nothing to check. {e}")
            continue
        except Exception as e:
            errors += 1
            out(f"{table}: ERROR {str(e).strip()[:500]}")
            continue
        worst = a.verdict if worst is None or a.verdict.rank > worst.rank else worst
        t = a.tables[0]
        out(f"{table}: {a.verdict.value.upper()} (risk {a.verdict.risk_tier})")
        out(f"  {t.summary}")
        for h in t.hits:
            out(f"    [{h.verdict.value}] {h.rule}: {h.reason}")
        out(f"  Undo: {t.rollback}")
        for line in s.notify(a):
            out(f"  notify {line}")
    if errors:
        return 2
    if worst is not None and args.fail_on != "never" and worst.rank >= Verdict(args.fail_on).rank:
        out(f"bricksentry: verdict {worst.value} is at or above --fail-on {args.fail_on}; failing this task "
            "so downstream tasks don't run.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> None:
    """Exit only on failure. Databricks reports any SystemExit from a spark_python_task,
    even ``SystemExit(0)``, as a task error, so a clean run must simply return."""
    code = run(argv)
    if code:
        sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
