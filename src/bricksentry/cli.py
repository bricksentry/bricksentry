"""bricksentry CLI.

Exit codes: 0 = verdict below --fail-on, 1 = verdict at or above --fail-on,
2 = usage / configuration error.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from bricksentry import __version__
from bricksentry.config import Config
from bricksentry.models import Verdict
from bricksentry.preview import PreviewGuardError


def _config(config_path) -> Config:
    cfg = Config.load(config_path, warn=False)
    for w in cfg.warnings:
        click.echo(click.style("warning: ", fg="yellow") + w, err=True)
    return cfg


def _sentry(config_path):
    from bricksentry.assess import Sentry

    return Sentry(_config(config_path))


def _emit(obj, fmt: str, markdown_fn=None):
    if fmt == "json":
        click.echo(json.dumps(obj if isinstance(obj, (dict, list)) else obj.to_dict(), indent=2, default=str))
    elif fmt == "markdown" and markdown_fn:
        click.echo(markdown_fn())
    else:
        click.echo(obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))


def _exit_for(verdict: Verdict, fail_on: str) -> None:
    if verdict.rank >= Verdict(fail_on).rank:
        sys.exit(1)


config_opt = click.option("--config", "-c", "config_path", type=click.Path(), default=None,
                          help="Path to bricksentry.yml (default: ./bricksentry.yml or $BRICKSENTRY_CONFIG)")
notify_opt = click.option("--notify/--no-notify", "notify", default=True, show_default=True,
                          help="Send a Slack/Teams alert if webhooks are configured (see notify: in bricksentry.yml)")
fail_opt = click.option("--fail-on", type=click.Choice([v.value for v in Verdict]), default="block",
                        show_default=True, help="Exit 1 when the verdict is at least this severe")


@click.group()
@click.version_option(__version__, prog_name="bricksentry")
def main():
    """Change-safety for Databricks pipelines and the AI agents that edit them."""


@main.command()
@click.argument("base")
@click.argument("candidate")
@click.option("--key", "-k", "keys", multiple=True, help="Key column (repeatable). Defaults to config.")
@click.option("--sample-pct", type=float, default=None, help="Diff a deterministic hash sample (0-100).")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
@config_opt
def diff(base, candidate, keys, sample_pct, fmt, config_path):
    """Diff BASE (production) against CANDIDATE (your change)."""
    from bricksentry.explain import explain_diff

    s = _sentry(config_path)
    d = s.diff(base, candidate, keys=list(keys) or None, table=base, sample_pct=sample_pct)
    _emit(d.to_dict() if fmt == "json" else explain_diff(d, s.config.spec(base).measures), fmt)


@main.command()
@click.argument("table")
@click.option("--depth", type=int, default=None)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
@config_opt
def impact(table, depth, fmt, config_path):
    """Show the downstream blast radius of TABLE."""
    from bricksentry.explain import explain_impact

    s = _sentry(config_path)
    nodes = s.downstream(table, depth)
    if fmt == "json":
        _emit({"table": table, "downstream": [n.to_dict() for n in nodes]}, fmt)
        return
    click.echo(explain_impact(table, nodes))
    for n in sorted(nodes, key=lambda n: (n.depth, n.kind, n.name)):
        tags = ",".join(n.tags) if n.tags else ""
        click.echo(f"  {'  ' * (n.depth - 1)}└ [{n.kind}] {n.name}  {n.owner or ''} {tags}".rstrip())


@main.command()
@click.option("--table", "-t", "tables", multiple=True, help="Table to assess (repeatable).")
@click.option("--from-git", "git_base", default=None, help="Detect changed tables from `git diff BASE...HEAD`.")
@click.option("--candidate", "cand_pairs", multiple=True, help="Override: TABLE=CANDIDATE_TABLE")
@click.option("--pr", default=None, help="PR number for candidate_pattern {pr}.")
@click.option("--format", "fmt", type=click.Choice(["text", "json", "markdown"]), default="text")
@click.option("--output", "-o", type=click.Path(), default=None, help="Also write the markdown report here.")
@click.option("--comment", is_flag=True, help="Post/update the report as a GitHub PR comment.")
@notify_opt
@fail_opt
@config_opt
def assess(tables, git_base, cand_pairs, pr, fmt, output, comment, notify, fail_on, config_path):
    """Assess a change: data diff + blast radius + policy verdict + rollback plan."""
    from bricksentry.changes import changed_files, tables_for_files
    from bricksentry.report import to_markdown

    s = _sentry(config_path)
    tables = list(tables)
    if git_base:
        files = changed_files(git_base)
        found = tables_for_files(s.config, files)
        click.echo(f"{len(files)} changed file(s) -> {len(found)} table(s): {', '.join(found) or 'none'}", err=True)
        tables += [t for t in found if t not in tables]
    if not tables:
        click.echo("Nothing to assess: no tables given or matched.", err=True)
        sys.exit(0)
    candidates = dict(p.split("=", 1) for p in cand_pairs)
    pr = pr or os.environ.get("BRICKSENTRY_PR")
    a = s.assess(tables, candidates=candidates, pr=pr)
    md = to_markdown(a)
    if output:
        Path(output).write_text(md)
    if comment:
        from bricksentry.github import upsert_pr_comment

        url = upsert_pr_comment(md, pr=pr)
        click.echo(f"PR comment: {url}", err=True)
    if notify:
        for line in s.notify(a, pr=pr):
            click.echo(f"notify {line}", err=True)
    if fmt == "text":
        click.echo(f"{a.verdict.value.upper()} (risk {a.verdict.risk_tier})")
        for t in a.tables:
            click.echo(f"- {t.summary}")
            for h in t.hits:
                click.echo(f"    [{h.verdict.value}] {h.rule}: {h.reason}")
        if a.notify:
            click.echo(f"Notify: {', '.join(a.notify)}")
    else:
        _emit(a, fmt, lambda: md)
    _exit_for(a.verdict, fail_on)


@main.command()
@click.argument("target")
@click.option("--sql", "sql_text", default=None, help="The write statement. Use {{target}} or the table name.")
@click.option("--file", "sql_file", type=click.Path(exists=True), default=None)
@click.option("--key", "-k", "keys", multiple=True)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
@notify_opt
@fail_opt
@config_opt
def preview(target, sql_text, sql_file, keys, fmt, notify, fail_on, config_path):
    """Run a write against a sandbox clone of TARGET and show what it would change."""
    if not (sql_text or sql_file):
        raise click.UsageError("pass --sql or --file")
    sql = sql_text or Path(sql_file).read_text()
    s = _sentry(config_path)
    try:
        out = s.preview_write(sql, target, keys=list(keys) or None)
    except PreviewGuardError as e:
        # A refused statement is an expected outcome, not a crash: say why and what to do instead.
        click.echo(click.style("Refused: ", fg="red", bold=True) + str(e), err=True)
        raise SystemExit(2) from None
    except ValueError as e:  # e.g. no key columns configured: a configuration error, exit 2
        err = click.ClickException(str(e))
        err.exit_code = 2
        raise err from None
    if notify:
        a = s.get_assessment(out["id"])
        if a is not None:
            for line in s.notify(a):
                click.echo(f"notify {line}", err=True)
    if fmt == "json":
        _emit(out, fmt)
    else:
        click.echo(f"{out['verdict'].upper()} (risk {out['risk_tier']})")
        for t in out["tables"]:
            click.echo(f"- {t['summary']}")
            for h in t["rule_hits"]:
                click.echo(f"    [{h['verdict']}] {h['rule']}: {h['reason']}")
    _exit_for(Verdict(out["verdict"]), fail_on)


@main.command()
@click.option("--report", "report_path", type=click.Path(), default=None,
              help="Also write the final pull-request comment (markdown) to this file.")
def demo(report_path):
    """Run the agent safety loop on a local 20,000-row example (needs bricksentry[duckdb])."""
    from bricksentry.demo import run_demo

    try:
        run_demo(report_path)
    except ImportError as e:
        raise click.ClickException(str(e)) from e


@main.command("check-last-change")
@click.argument("table")
@click.option("--key", "-k", "keys", multiple=True,
              help="Key column (repeatable). Defaults to config, then the table's PRIMARY KEY.")
@click.option("--version", "version", type=int, default=None, help="Check this table version instead.")
@click.option("--since", default=None,
              help="Check every data change after this time as one change (ISO 8601 or epoch ms), "
                   "e.g. the job's start time {{job.start_time.iso_datetime}}.")
@click.option("--sample-pct", type=float, default=None, help="Diff a deterministic hash sample (0-100).")
@click.option("--allow-no-change", is_flag=True,
              help="Exit 0 when there is no data change to check (e.g. the run only ran OPTIMIZE).")
@click.option("--format", "fmt", type=click.Choice(["text", "json", "markdown"]), default="text")
@click.option("--output", "-o", type=click.Path(), default=None, help="Also write the markdown report here.")
@notify_opt
@fail_opt
@config_opt
def check_last_change(table, keys, version, since, sample_pct, allow_no_change, fmt, output, notify, fail_on,
                      config_path):
    """Check the change a pipeline already made to TABLE, from its Delta history.

    Compares the version before the change with the version after it (two shallow clones in
    sandbox_schema, dropped afterwards), then runs lineage and policy. Prints the RESTORE that
    undoes it. Exit 1 when the verdict is at least --fail-on, so a job task after your
    pipeline stops the tasks downstream of it.
    """
    from bricksentry.history import NoChangeError
    from bricksentry.report import to_markdown

    if version is not None and since:
        raise click.UsageError("pass --version or --since, not both")
    s = _sentry(config_path)
    try:
        a, change = s.check_change(table, keys=list(keys) or None, version=version, since=since,
                                   sample_pct=sample_pct)
    except NoChangeError as e:
        click.echo(str(e), err=True)
        sys.exit(0 if allow_no_change else 2)
    except (NotImplementedError, ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e
    md = to_markdown(a)
    if output:
        Path(output).write_text(md)
    if notify:
        for line in s.notify(a):
            click.echo(f"notify {line}", err=True)
    if fmt == "json":
        click.echo(json.dumps({**a.to_dict(), "change": change.to_dict()}, indent=2, default=str))
    elif fmt == "markdown":
        click.echo(md)
    else:
        click.echo(f"{a.verdict.value.upper()} (risk {a.verdict.risk_tier})")
        for t in a.tables:
            click.echo(f"- {t.summary}")
            for h in t.hits:
                click.echo(f"    [{h.verdict.value}] {h.rule}: {h.reason}")
            if t.rollback_warning:
                click.echo(click.style("Warning before you undo: ", fg="yellow", bold=True) + t.rollback_warning)
            click.echo(f"Undo: {t.rollback}")
        if a.notify:
            click.echo(f"Notify: {', '.join(a.notify)}")
    _exit_for(a.verdict, fail_on)


@main.command()
@click.argument("table")
@config_opt
def rollback(table, config_path):
    """Print the SQL that restores TABLE to its current version."""
    click.echo(_sentry(config_path).rollback_plan(table))


@main.command()
@click.option("--transport", type=click.Choice(["stdio", "sse", "streamable-http"]), default="stdio",
              show_default=True, help="stdio for local agents; streamable-http to serve over HTTP (e.g. Databricks Apps)")
@click.option("--host", default=None,
              help="HTTP only. Default 127.0.0.1, or 0.0.0.0 on Databricks Apps. Env: BRICKSENTRY_HOST")
@click.option("--port", type=int, default=None,
              help="HTTP only. Default 8000, or $DATABRICKS_APP_PORT on Databricks Apps. Env: BRICKSENTRY_PORT")
@click.option("--read-only", is_flag=True, default=False,
              help="Leave out preview_write, so the server never writes, not even to a sandbox clone. "
                   "Env: BRICKSENTRY_READ_ONLY=1")
@config_opt
def mcp(transport, host, port, read_only, config_path):
    """Start the MCP server for AI agents."""
    if config_path:
        os.environ["BRICKSENTRY_CONFIG"] = str(Path(config_path).resolve())
    read_only = read_only or os.environ.get("BRICKSENTRY_READ_ONLY", "").strip().lower() in ("1", "true", "yes")
    from bricksentry.mcp_server import serve

    serve(transport, host=host, port=port, read_only=read_only)


_INIT_CONFIG = """# bricksentry configuration
backend:
  type: databricks          # or duckdb for local work
  # warehouse_id: <sql-warehouse-id>   # or env DATABRICKS_WAREHOUSE_ID
  # profile: DEFAULT                   # ~/.databrickscfg profile (optional)

sandbox_schema: main.bricksentry_sandbox   # where preview_write puts disposable clones

# Where your CI build writes the candidate (changed) version of each table.
candidate_pattern: "{catalog}.{schema}_pr_{pr}.{table}"

# sample_pct: 10           # diff a deterministic 10% key sample on big tables

# Optional Slack / Teams alerts. Keep webhook URLs in environment variables or CI secrets.
# Test with: bricksentry test-alert
# notify:
#   on: needs_human
#   slack_webhook: env:SLACK_WEBHOOK_URL
#   teams_webhook: env:TEAMS_WEBHOOK_URL
#   mentions:                    # owner -> Slack id, so reviewers get @-mentioned
#     finance-analytics: S0123ABCD

lineage:
  depth: 3
  lookback_days: 90

tables:
  main.sales.orders:
    keys: [order_id]
    segment_by: [region]
    measures: [amount, quantity]   # totals worth checking; IDs, years and ratings are skipped by default
    paths: ["src/pipelines/orders/**", "resources/orders_job.yml"]

policy: policy.yml
"""

_INIT_POLICY = """default: pass
notify_owners_on: needs_human
rules:
  - {name: diff-failed,          when: {diff_error: true},              then: needs_human}
  - {name: duplicate-keys,       when: {duplicate_keys: true},          then: block}
  - {name: null-keys,            when: {null_keys: true},               then: warn}
  - {name: dropped-column,       when: {column_removed: true},          then: needs_human}
  - {name: type-change,          when: {column_type_changed: true},     then: warn}
  - {name: certified-downstream, when: {downstream_tag: certified},     then: needs_human}
  - {name: pii-table,            when: {tag: pii},                      then: needs_human}
  - {name: pii-column-changed,   when: {changed_column_tag: pii},       then: needs_human}
  - {name: row-loss,             when: {removed_pct_gt: 1},             then: needs_human}
  - {name: many-rows-changed,    when: {changed_pct_gt: 10},            then: warn}
  - {name: numeric-total-moved,  when: {sum_change_pct_gt: 5},          then: warn}
  - {name: wide-blast-radius,    when: {downstream_count_gt: 25},       then: warn}
"""


@main.command()
@click.option("--force", is_flag=True)
@click.option("--policy", "template", default="default", show_default=True,
              help="Policy template for policy.yml (see `bricksentry policies`)")
def init(force, template):
    """Write a starter bricksentry.yml and policy.yml."""
    from bricksentry.policy_templates import TEMPLATES, to_yaml

    if template.lower() not in TEMPLATES:
        raise click.UsageError(f"unknown template {template!r}; choose from {', '.join(sorted(TEMPLATES))}")
    policy_body = _INIT_POLICY if template.lower() == "default" else to_yaml(template)
    for name, body in (("bricksentry.yml", _INIT_CONFIG), ("policy.yml", policy_body)):
        p = Path(name)
        if p.exists() and not force:
            click.echo(f"skip {name} (exists; use --force)")
            continue
        p.write_text(body)
        click.echo(f"wrote {name}")


@main.command()
@click.argument("name", required=False)
def policies(name):
    """List the built-in policy templates, or print one as policy.yml."""
    from bricksentry.policy_templates import TEMPLATES, to_yaml

    if name:
        if name.lower() not in TEMPLATES:
            raise click.UsageError(f"unknown template {name!r}; choose from {', '.join(sorted(TEMPLATES))}")
        click.echo(to_yaml(name), nl=False)
        return
    width = max(len(n) for n in TEMPLATES)
    for n, t in TEMPLATES.items():
        click.echo(f"{n.ljust(width)}  {t['description']}")
    click.echo("\nUse one with `policy: builtin:<name>` in bricksentry.yml, or `bricksentry init --policy <name>`.")


@main.command("test-alert")
@click.option("--verdict", type=click.Choice([v.value for v in Verdict]), default="needs_human", show_default=True,
              help="Verdict of the sample alert")
@click.option("--owner", default=None,
              help="Reviewer to show. Use a name from notify.mentions to check that @-mentions work.")
@config_opt
def test_alert(verdict, owner, config_path):
    """Send a clearly labelled sample alert to your Slack / Teams webhooks.

    Reads no tables. Exits 1 if a webhook failed or none is configured."""
    from bricksentry.notify import NotifySettings, sample_assessment, send

    cfg = _config(config_path)
    try:
        settings = NotifySettings.from_config(cfg.notify)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    if not settings.enabled:
        raise click.ClickException(
            "no webhook configured: set BRICKSENTRY_SLACK_WEBHOOK or BRICKSENTRY_TEAMS_WEBHOOK, "
            "or add notify: to bricksentry.yml"
        )
    lines = send(sample_assessment(Verdict(verdict), owner), settings, force=True)
    for line in lines:
        click.echo(line)
    if any(not line.endswith(": sent") for line in lines):
        sys.exit(1)


def _run(cmd: click.Command, prog_name: str | None = None) -> None:
    try:
        cmd.main(prog_name=prog_name, standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.ClickException as e:
        e.show()
        sys.exit(2)
    except click.exceptions.Abort:
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:
        click.echo(f"bricksentry: error: {e}", err=True)
        sys.exit(2)


def entry() -> None:
    """Console entry point: turn unexpected errors into a clean message and exit code 2."""
    _run(main)


def mcp_entry() -> None:
    """Console entry for the ``bricksentry-mcp`` package: same as ``bricksentry mcp``."""
    _run(mcp, prog_name="bricksentry-mcp")


if __name__ == "__main__":  # pragma: no cover
    entry()
