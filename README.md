# Welcome to bricksentry

<!-- mcp-name: io.github.bricksentry/bricksentry -->

[![PyPI](https://img.shields.io/pypi/v/bricksentry)](https://pypi.org/project/bricksentry/) [![Python](https://img.shields.io/pypi/pyversions/bricksentry)](https://pypi.org/project/bricksentry/) [![Tests](https://github.com/bricksentry/bricksentry/actions/workflows/ci.yml/badge.svg)](https://github.com/bricksentry/bricksentry/actions/workflows/ci.yml) [![Lint](https://github.com/bricksentry/bricksentry/actions/workflows/lint.yml/badge.svg)](https://github.com/bricksentry/bricksentry/actions/workflows/lint.yml) [![Coverage](https://img.shields.io/badge/coverage-%E2%89%A575%25%20enforced-brightgreen)](https://github.com/bricksentry/bricksentry/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

***The safety check for data changes in the AI era.***

Change-safety for Databricks pipelines and the AI agents that edit them: know what a change does to your data before it ships.

**[Try it in 30 seconds](#try-it-in-30-seconds-no-databricks-needed)** · **[Run the Databricks notebook](#run-it-in-your-databricks-workspace)** · **[Docs: bricksentry.org](https://bricksentry.org)** · **[LinkedIn](https://www.linkedin.com/company/bricksentry-org/)**

Built by Brahmareddy, Databricks Champion. Open source under Apache-2.0.

*Why the name?* Bricks plus entry. Your tables and pipelines are the bricks of the lakehouse, and every change to them is an entry. bricksentry checks each entry, reports what it does, and stands guard before it lands.

## Try it in 30 seconds (no Databricks needed)

```bash
pip install "bricksentry[duckdb]"
bricksentry demo
```

An AI agent changes a 20,000-row revenue table. bricksentry previews its DELETE on a clone (production untouched), **blocks** its pull request for a join that duplicates 40 orders, and routes the fixed version to the owners of a certified table downstream. Everything runs locally on DuckDB.

![bricksentry demo: an agent's change is previewed, blocked for duplicate keys, then routed to the table owners](https://raw.githubusercontent.com/bricksentry/bricksentry/main/docs/images/demo.png)

## Run it in your Databricks workspace

> **Databricks quickstart notebook:** in your workspace, go to **Workspace → Import → URL** and paste
> `https://raw.githubusercontent.com/bricksentry/bricksentry/main/examples/databricks/bricksentry_quickstart.py`

The [quickstart notebook](examples/databricks/bricksentry_quickstart.py) creates a small demo table in a schema you choose and runs the same loop on Databricks: Unity Catalog blast radius, a real `RESTORE` rollback plan, a preview on a shallow clone, a blocked pull request and the fix. Then it checks one of your own tables, read-only, and cleans up after itself.

You need a SQL warehouse, `CREATE TABLE` on the schema you pick, and `SELECT` on `system.access.table_lineage` for the blast radius. A Jupyter copy is in [`bricksentry_quickstart.ipynb`](examples/databricks/bricksentry_quickstart.ipynb).

## What it does

Before a change reaches production, bricksentry answers three questions, in a
pull-request comment for humans and in structured JSON for agents:

1. **What does it do to the data?** A key-based data diff: added, removed and changed rows, schema changes, per-column changes, numeric totals, duplicate keys, and *where* the change is concentrated ("100% of affected rows are region = APAC").
2. **Who gets hurt?** The downstream blast radius from Unity Catalog lineage: tables, jobs, notebooks, Lakeflow pipelines, dashboards and queries, with owners and tags.
3. **Is it safe, and how do I undo it?** A deterministic policy verdict (`pass` · `warn` · `needs_human` · `block`) and the exact `RESTORE TABLE … VERSION AS OF n` rollback.

For your Databricks workspace:

```bash
pip install "bricksentry[databricks,mcp]"
bricksentry init        # writes bricksentry.yml and policy.yml
```

## Why it exists

AI now writes much of our pipeline code, and checking what that code does to the data hasn't kept up. Code review can't see a silent 3% revenue shift or a join that fans out. The tools that do catch this mostly cover dbt, or need a paid platform. Most Databricks work is PySpark notebooks, Jobs and Lakeflow pipelines, and bricksentry is built for that work:

- **Unity Catalog runtime lineage**, not SQL parsing, so PySpark notebooks and jobs are included.
- **SQL pushdown**: all diffing runs on your SQL warehouse, so no Spark session is needed and calls stay cheap enough for an agent loop.
- **One engine, three faces**: CLI and GitHub Action · Python API · MCP server.

## For AI agents: the MCP server

The quickest way to connect an agent is the `bricksentry-mcp` package, which installs bricksentry with its MCP and Databricks extras. With [uv](https://docs.astral.sh/uv/) there is nothing to install first:

```bash
# Claude Code
claude mcp add bricksentry -- uvx bricksentry-mcp --config /full/path/to/bricksentry.yml
```

```json
{
  "mcpServers": {
    "bricksentry": {
      "command": "uvx",
      "args": ["bricksentry-mcp", "--config", "/full/path/to/bricksentry.yml"]
    }
  }
}
```

The JSON works for the Claude desktop app, Cursor and other MCP clients. Use a **full path** to `bricksentry.yml`, because clients start the server from their own working folder. If an app can't find `uvx`, use its full path (`which uvx`). To share the setup through a repo, `claude mcp add -s project ...` writes it to `.mcp.json`; a relative path then works when Claude Code starts from the repo root. No config yet? `uvx --from bricksentry bricksentry init` writes a starter one.

Already installed bricksentry with pip? `pip install "bricksentry[mcp,databricks]"` and run `bricksentry mcp --config /full/path/to/bricksentry.yml` instead. Add `--transport streamable-http` for remote agents.

bricksentry is listed in the [official MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.bricksentry/bricksentry) as `io.github.bricksentry/bricksentry`.

| Tool | What the agent gets |
|---|---|
| `whats_downstream(table, depth)` | Downstream assets with owners and tags. Call it *before* editing. |
| `rollback_plan(table)` | The restore SQL. Capture it *before* changing anything. |
| `preview_write(sql, target, keys?)` | Runs one write against a **disposable shallow clone**, never production, and returns the diff plus verdict. |
| `assess_change(tables, candidates?, pr?)` | Diffs prod vs. the candidate built by your change, plus lineage and policy. Returns a verdict and an `id`. |
| `explain_diff(assessment_id or table)` | Plain-English summary. |
| `safe_to_ship(assessment_id)` | `safe_to_ship`, the reasons, and an explicit `next_step` to follow. |

Designed for agents:

- **The verdict never comes from an LLM.** Rules in `policy.yml` decide, so the same change always gets the same answer.
- **Read-only by default.** Only `preview_write` writes, and only to a sandbox clone. It accepts a single INSERT / MERGE / UPDATE / DELETE / CREATE OR REPLACE whose target must be the clone, and it refuses DROP, TRUNCATE, ALTER, GRANT and multiple statements.
- **Errors come back as `{"error": ...}`**, so agents can recover instead of crashing.
- **Bounded cost:** deterministic hash sampling (`sample_pct`), capped samples, and lineage limited by depth and lookback window. A full-table checksum guards the sample: if the sample shows no change but the tables differ, the diff re-runs on every row, so a one-row change can't slip through.
- **Assessments are saved** as JSON (default `~/.bricksentry/assessments`), so `explain_diff` and `safe_to_ship` work from another process or a fresh `Sentry`.

The same tools are plain Python functions in `bricksentry.agent_tools`, so you can use them from LangGraph, the OpenAI Agents SDK or the Databricks Agent Framework without MCP.

To watch the whole loop, run `bricksentry demo` (see the top of this page). `bricksentry demo --report report.md` also writes the pull-request comment a reviewer would see.

## For data engineers: CLI and CI

```bash
bricksentry init                                        # starter bricksentry.yml + policy.yml
bricksentry init --policy finance                       # ...or start from a policy template
bricksentry policies                                    # list the policy templates
bricksentry impact main.sales.orders                    # blast radius
bricksentry diff main.sales.orders main.sales_pr_42.orders
bricksentry preview main.sales.orders --sql "MERGE INTO {{target}} t USING updates u ON t.order_id = u.order_id WHEN MATCHED THEN UPDATE SET *"
bricksentry assess --from-git origin/main --pr 42 --format markdown --fail-on needs_human
bricksentry rollback main.sales.orders
```

Exit codes: `0` below `--fail-on`, `1` at or above it, `2` for configuration errors.

**In GitHub Actions.** Build your PR's version of the pipeline into a per-PR schema (for example with a Databricks Asset Bundle `ci` target), then add:

```yaml
- run: pip install "bricksentry[databricks]"
- run: >
    bricksentry assess
    --from-git origin/${{ github.base_ref }}
    --pr ${{ github.event.pull_request.number }}
    --comment --output bricksentry-report.md
    --fail-on needs_human
  env:
    GITHUB_TOKEN: ${{ github.token }}
    DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
    DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID }}
    DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET }}
    DATABRICKS_WAREHOUSE_ID: ${{ secrets.DATABRICKS_WAREHOUSE_ID }}
```

It detects changed tables from the files in the PR and posts or updates one PR comment, like this one for the demo's fan-out bug:

![A bricksentry pull-request comment: blocked for 40 duplicate keys, with the data diff and blast radius](https://raw.githubusercontent.com/bricksentry/bricksentry/main/docs/images/pr-comment.png)

Check out with `fetch-depth: 0` and give the job `pull-requests: write` permission.

Or use the bundled GitHub Action, which also writes the report to the job summary:

```yaml
- uses: bricksentry/bricksentry@v0.1.7
  with:
    config: bricksentry.yml
    fail-on: needs_human
```

A complete workflow you can copy to `.github/workflows/bricksentry.yml`:

```yaml
name: bricksentry
on: pull_request

permissions:
  contents: read
  pull-requests: write

jobs:
  check-data:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      # Build this PR's version of your pipeline into a per-PR schema first,
      # for example: databricks bundle deploy -t ci && databricks bundle run -t ci
      - uses: bricksentry/bricksentry@v0.1.7
        with:
          config: bricksentry.yml
          fail-on: needs_human
          slack-webhook: ${{ secrets.SLACK_WEBHOOK_URL }}    # optional
          teams-webhook: ${{ secrets.TEAMS_WEBHOOK_URL }}    # optional
        env:
          DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
          DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID }}
          DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET }}
          DATABRICKS_WAREHOUSE_ID: ${{ secrets.DATABRICKS_WAREHOUSE_ID }}
```

To make the check binding, mark this job as a required status check in your branch protection rules. Then a `needs_human` or `block` verdict stops the merge, whatever the author or an agent decides.

### Alerts in Slack and Teams

When the verdict is `needs_human` or worse, bricksentry can post a short alert: the verdict, the rules that fired, who should review, and a link to the PR.

```yaml
# bricksentry.yml
notify:
  on: needs_human                         # alert at this verdict or worse
  slack_webhook: env:SLACK_WEBHOOK_URL    # Slack incoming webhook
  teams_webhook: env:TEAMS_WEBHOOK_URL    # Teams Workflows "webhook request" URL
```

Keep webhook URLs in secrets or environment variables, never in the file. `BRICKSENTRY_SLACK_WEBHOOK` and `BRICKSENTRY_TEAMS_WEBHOOK` work too. `assess` and `preview` send alerts automatically when a webhook is set (`--no-notify` turns them off), and in Python call `sentry.notify(assessment)`. A failed webhook is reported but never changes the verdict or fails the run.

## Python API

```python
from bricksentry import Sentry

s = Sentry()                                   # reads ./bricksentry.yml
a = s.assess(["main.sales.orders"], pr="42")
print(a.verdict, a.notify)
print(a.tables[0].summary)

s.preview_write("DELETE FROM main.sales.orders WHERE status = 'test'", "main.sales.orders")
```

## Configuration

```yaml
backend: {type: databricks, warehouse_id: abc123}      # auth: standard Databricks SDK chain
sandbox_schema: main.bricksentry_sandbox
candidate_pattern: "{catalog}.{schema}_pr_{pr}.{table}"
sample_pct: 10                                         # sample_guard re-checks the whole table (on by default)
assessment_store: ~/.bricksentry/assessments           # or false to keep assessments in memory
lineage: {type: databricks, depth: 3, lookback_days: 90}   # also: exclude, exclude_dropped
tables:
  main.sales.orders:
    keys: [order_id]
    segment_by: [region]
    paths: ["src/pipelines/orders/**", "resources/orders_job.yml"]
policy: policy.yml            # or builtin:<template>, see "Policy templates" below
```

### Policy example

A small `policy.yml` with one rule per severity. `pass` is the default: a change gets it when no rule matches.

```yaml
default: pass                       # no rule matched -> pass
notify_owners_on: needs_human       # tag downstream owners from this level up

rules:
  - name: type-change               # a column changed type -> warn
    when: {column_type_changed: true}
    then: warn

  - name: revenue-moved             # total revenue moved more than 2% -> a human signs off
    when: {column: amount, sum_change_pct_gt: 2}
    then: needs_human

  - name: duplicate-keys            # a join fanned out and duplicated keys -> block the PR
    when: {duplicate_keys: true}
    then: block
```

| The change | Verdict |
|---|---|
| A filter fix that changes 40 rows, nothing else | `pass` |
| `order_date` goes from `STRING` to `DATE` | `warn` |
| A pricing fix moves `SUM(amount)` by 3.1% | `needs_human` |
| A new join duplicates 40 `order_id`s | `block` |

If several rules match, the most severe one wins. The full starter policy is in [`examples/databricks/policy.yml`](examples/databricks/policy.yml), and `bricksentry init` writes one for you.

### Policy templates

Start from a ready-made policy instead of writing one:

| Template | For |
|---|---|
| `agent-safe` | Changes written by AI agents: anything beyond a small, clean change goes to a human |
| `default` | Balanced: block duplicate keys, a human for row loss, dropped columns, PII and certified downstream |
| `strict` | Production-critical pipelines: small changes need a human, failed checks block |
| `pii` | Tables with personal data: any change to a `pii`-tagged table, or one feeding it, needs a human |
| `certified` | Certified or gold tables that dashboards and AI rely on |
| `finance` | Revenue and money tables: totals can't move quietly, double counting is blocked |

Use one directly with `policy: builtin:finance`, or write an editable copy with `bricksentry init --policy finance`. `bricksentry policies finance` prints it. Tags come from Unity Catalog, so tag your tables `pii` or `certified` for those templates to apply.

### Policy conditions

`table` (glob) · `tag` · `downstream_tag` · `downstream_kind` · `downstream_count_gt` · `removed_pct_gt` · `added_pct_gt` · `changed_pct_gt` · `row_count_change_pct_gt` · `row_count_drop_pct_gt` · `column_removed` · `column_added` · `column_type_changed` · `duplicate_keys` · `null_keys` · `sum_change_pct_gt` / `null_increase_gt` (optionally with `column`) · `any_change` · `diff_error`

All conditions in a `when` must hold. The most severe matching rule sets the verdict. Unknown conditions are rejected at load time, so a typo can't silently disable a rule.

## Permissions needed on Databricks

- `CAN USE` on a SQL warehouse.
- `SELECT` on the tables you assess and their candidates.
- `SELECT` on `system.access.table_lineage`, plus `system.information_schema` for owners and tags. Without these, bricksentry still diffs; lineage is just empty.
- `CREATE TABLE` on `sandbox_schema`, for `preview_write`.

## Known limits (v0.1.x)

- **Lineage is recorded at runtime.** Brand-new tables have no downstream history yet. Unity Catalog doesn't capture RDD operations, and UDFs hide column-level mapping.
- **Keys are required.** Tables without a stable key can't be diffed row by row yet (an aggregate-profile mode is planned).
- **Changed-table detection uses path globs** from config. It doesn't parse Asset Bundles yet.
- **Validated on one Databricks workspace so far** (serverless, November 2026, on a 2-million-row table with 0.1.4): lineage, Delta rollback plans, previews on shallow clones, every verdict level, and the sampling guard catching a single changed row. The automated tests use DuckDB and a mocked SDK, so please [open an issue](https://github.com/bricksentry/bricksentry/issues) if something behaves differently on yours.

## Roadmap

**Top priority: validation across more workspaces.** Classic and serverless compute, AWS, Azure and GCP, and workspaces with restricted system-table access. If you run the quickstart notebook, please [share what you see](https://github.com/bricksentry/bricksentry/issues), whether it works or not.

Next: dbt support, so dbt models on Databricks get the same diff, blast radius and verdict (planned for 0.2) · Asset Bundle parsing for automatic table detection · column-level blast radius from `system.access.column_lineage` · an aggregate diff for keyless tables · Lakeflow expectations in the verdict · `bricksentry watch` for scheduled drift checks · a Databricks App UI.

## Contributing

Bug reports from real pipelines help the most. See [CONTRIBUTING.md](CONTRIBUTING.md), and [SECURITY.md](SECURITY.md) for reporting vulnerabilities privately.

## License

Apache-2.0. Created and maintained by Brahma - Databricks Champion.

## Trademarks

bricksentry is an independent open-source project. It is not affiliated with, endorsed by, or sponsored by Databricks, Inc. or Functional Software, Inc. (Sentry). Databricks, Unity Catalog and Lakeflow are trademarks of Databricks, Inc. Delta Lake is a trademark of the Linux Foundation, and Apache Spark and PySpark are trademarks of the Apache Software Foundation. All other names are the trademarks of their respective owners and are used here only to describe compatibility. Thank you.
