# Changelog

## Unreleased

Bundle template changes. `bundle init` reads the template from GitHub, so these apply right away; no new package version is needed.

- `tables` is optional: leave it empty to deploy only the demo job and see a block before choosing your own tables. The only input without a default is now `warehouse_id`. The demo question comes first.
- The template README documents non-interactive init (`--config-file`) for CI, scripts and AI agents.
- `scratch_schema` defaults to `<your default catalog>.bricksentry_scratch` instead of `main.bricksentry_scratch`. Workspaces without a `main` catalog had their first demo run fail with "catalog not found" instead of the intended BLOCK.
- Library (in the next release): when the scratch schema's catalog doesn't exist, `check-last-change`, the job task and the demo task say so and how to fix it, instead of a raw `NO_SUCH_CATALOG` error.

## 0.1.18 (2026-11-12)

See a block in your own workspace within minutes of `bundle init`.

- The job template asks whether to add a **demo job** (default yes). `databricks bundle run bricksentry_demo` creates a 20,000-row demo table in your scratch schema, runs a MERGE whose ON condition compares region case-sensitively (so 25 orders are inserted again as duplicates), then checks that change. The run ends red with BLOCK, the rules that fired and the `RESTORE` that undoes it, plus the Slack alert if you set one up. It writes only to `bricksentry_demo_*` tables.
- `python -m bricksentry.job_demo`: the task that makes the demo change.
- Slack is now presented as optional in the template prompt, and the success message lists the next commands.
- The template README no longer says the trigger is paused in the dev target: the job sets `pause_status: UNPAUSED` on its trigger, which development mode respects.

## 0.1.17 (2026-11-12)

- `bricksentry demo` now ends with a pipeline commit: a nightly MERGE with a wrong match condition lands, `check-last-change` finds it in the table history (skipping the OPTIMIZE after it), blocks it for 25 duplicate keys, prints the `RESTORE` and applies it. It replaces the old step 2, which only described a rollback because DuckDB has no table history; the demo now keeps versions itself. It still runs offline in about a second and ends with the one-command Databricks setup.
- The README opens with three steps, each on more real data: the 30-second demo on your laptop (`uvx --from "bricksentry[duckdb]" bricksentry demo`, nothing to install first), checking every change to your tables on Databricks with the bundle template, and stopping bad changes inside your pipeline job, in CI or for AI agents.
- New demo screenshot, generated from the real output by `scripts/render_demo_image.py`.

## 0.1.16 (2026-11-11)

From the live test of 0.1.15 on Databricks serverless:

- Fix: two checks of the same table at the same time (two jobs watching it, or a job and a notebook) used the same scratch clone names, so one check's cleanup could drop the other's clone mid-diff (`TABLE_OR_VIEW_NOT_FOUND`). Each check now uses its own clone names.
- The job template's task also sets `disable_auto_optimization: true`. On serverless, auto-optimization retries failed tasks even with `max_retries: 0`, so a `block` was still run twice.
- The job template now pins the bricksentry version (new `bricksentry_version` prompt, default the current release). Correction to the 0.1.15 release notes: a job deployed with `>=0.1.14` does **not** pick up new releases by itself, because serverless resolves a job's dependencies once and caches them. To upgrade, change the version in the job's YAML and redeploy; the generated README says so.

## 0.1.15 (2026-11-11)

From the first live test of the 0.1.14 job template on Databricks serverless (deployed with `databricks bundle deploy`, started by the table update trigger and by hand):

- Fix: every run of the job task was marked failed, even "pass" and "nothing to check", because Databricks treats any `SystemExit` from a Python script task as an error, including exit code 0. `python -m bricksentry.job` now only exits when the task should fail (verdict at or above `--fail-on`, or an error).
- The bundle template now asks for key columns (`keys`, e.g. `main.hr.employees=employee_id`), for tables without a `PRIMARY KEY`. Before, those runs failed with "No key columns" until `--key` was added by hand. The job task takes several at once with `--keys "t1=a,b; t2=c"`.
- The template's task sets `max_retries: 0`, so a `block` verdict fails the run once instead of being retried.
- Fix: the source distribution on PyPI had only some of the bundle template's files (and the Databricks App kit was missing). It now includes `templates/` and `deploy/` in full.

## 0.1.14 (2026-11-11)

Zero-friction adoption: check every pipeline run without changing the pipeline.

- **`bricksentry check-last-change <table>`** checks a change that already landed, from the table's Delta history. It finds the newest data change (WRITE, MERGE, UPDATE, DELETE, COPY INTO, ...; OPTIMIZE and VACUUM are skipped), compares the version before with the version after (two shallow clones in `sandbox_schema`, dropped afterwards), runs lineage and policy, sends the alert, and prints the `RESTORE` that undoes it. `--version N` checks a specific version; `--since TIME` checks everything after that time as one change, e.g. all a job run wrote. Keys come from config, `--key`, or the table's `PRIMARY KEY`. Python: `Sentry.check_change(table)`.
- **Asset Bundle template** in `templates/job-check`: `databricks bundle init https://github.com/bricksentry/bricksentry --template-dir templates/job-check` creates a serverless job with a table update trigger that checks every change to up to 10 tables, alerts on Slack (webhook read from a secret scope) and fails the run at the verdict you choose. Its README shows how to add the check as a task inside an existing job so a `block` stops the downstream tasks.
- **`python -m bricksentry.job`**: the job-task entry point used by the template. Takes the table list the trigger provides, a Slack or Teams webhook from a secret scope, and a link back to the run for the alert.
- Fix: in `bricksentry.yml`, `notify: {on: block}` was ignored, because YAML reads an unquoted `on` key as `true`. It is now read correctly (quoting it was the workaround).

## 0.1.13 (2026-11-11)

From the first live test of column lineage on Databricks (the 0.1.12 column features were verified against real `system.access.column_lineage` and column tags):

- Fix: column-level results listed columns of tables that had since been dropped, because lineage history keeps them. They are now left out, the same way the table-level blast radius does it. When existence can't be checked (no access to `information_schema`), they are kept, so missing privileges never hide anything.

## 0.1.12 (2026-11-11)

- **Column-level signals.** After the diff, bricksentry knows which columns a change touched (changed values, new NULLs, dropped or retyped). It reads their Unity Catalog tags from `information_schema.column_tags`, and follows `system.access.column_lineage` from those columns only, up to `lineage.column_depth` hops (default 2), to the downstream columns they feed. Assessments, summaries and the pull-request report show the changed columns, their tags and the column impact.
- New policy conditions: `changed_column_tag` (e.g. `pii`), `downstream_column_tag` (e.g. `certified`) and `changed_column` (glob). The default policy now asks a human when a column tagged `pii` changes (`pii-column-changed`); `strict`, `agent-safe` and `certified` also flag changes that feed a certified column (`certified-column-feed`), and `pii` flags changes that feed pii columns.
- `whats_downstream` takes an optional `columns` list, so an agent can see before editing which downstream columns a column feeds and how they are tagged.
- Static lineage (`type: static`) supports `column_edges` and `column_tags`; the README now shows a full static lineage example.
- `python -m bricksentry` runs the CLI, for environments where the console command isn't on the PATH.
- If column tags or column lineage can't be read, the check still runs and the summary says what was unavailable.

## 0.1.11 (2026-11-11)

> **Published by hand, without provenance.** GitHub Actions stopped starting jobs in this repository on 2026-11-11 (from about 20:20 UTC), so the release workflow could not run. `bricksentry` 0.1.11 was built from commit `b3d9c32` and uploaded manually with `twine`. That is why PyPI shows "loss of provenance" for this release: attestations are only created by the automated workflow. The 29 files inside the published wheel were compared with an independent build of the same commit and are identical. `bricksentry-mcp` was not published for 0.1.11. The next release goes through the normal workflow again, with provenance.
>
> SHA-256 of the published files:
> - `bricksentry-0.1.11-py3-none-any.whl`: `67f914626c71784d473693c8bbe72665b4de9a48c1265dbb1d1d0afe65cb76f1`
> - `bricksentry-0.1.11.tar.gz`: `6a894e20157734061850be33c1ca6af9dea160ba9f859babcc207939bc1c6913`
>
> Check a download with `shasum -a 256 <file>` (macOS) or `sha256sum <file>` (Linux).

From a full live test of 0.1.9 on Databricks serverless:

- Fix: when the Databricks SDK could not find credentials, the CLI showed the SDK's raw error. Run as a subprocess of a notebook it failed with a cryptic `parent_header` error. It now says it could not sign in to Databricks, lists the ways to provide credentials, and suggests the Python API inside notebooks.
- The `lineage.type` false warning that test also found was already fixed in 0.1.10.

## 0.1.10 (2026-11-11)

- **Run the MCP server as a Databricks App.** New kit in `deploy/databricks-app`: `app.yaml`, an Asset Bundle with the SQL warehouse resource, a pinned `requirements.txt`, a starter `bricksentry.yml` and the Unity Catalog grants for the app's service principal. Deploy with `databricks bundle deploy` and `databricks bundle run`; agents connect to `https://<app-url>/mcp`.
- `bricksentry mcp --transport streamable-http` now listens on `0.0.0.0:$DATABRICKS_APP_PORT` on Databricks Apps, and takes `--host` / `--port` (or `BRICKSENTRY_HOST` / `BRICKSENTRY_PORT`) elsewhere. It runs stateless, so it works behind a load balancer.
- New `--read-only` (or `BRICKSENTRY_READ_ONLY=1`) leaves out `preview_write`, so a shared server never writes, not even to a sandbox clone.
- Fix: with mcp 1.x, the HTTP server rejected requests that came through a proxy (HTTP 421) because localhost-only host checking stayed on. It now applies only when listening on localhost, as in mcp 2.
- Fix: the config typo warnings added in 0.1.8 flagged valid lineage settings such as `lineage.type`, `exclude` and `exclude_dropped`. All lineage keys bricksentry reads are now recognised, and a test keeps the list in sync with the code.

## 0.1.9 (2026-11-11)

- New notebook: **what did my last pipeline run do?** Point it at a Delta table; it finds the latest data change in the table's history, compares it with the version before using two shallow clones, and shows the verdict, rules, blast radius and the `RESTORE` command. Read-only on your table. Import from `examples/databricks/bricksentry_check_my_table.py`.
- New issue form, "What bricksentry found", for sharing what a check caught, missed or got wrong. The notebook prefills it without table or column names.
- Fix: when the lineage lookup failed (for example no `SELECT` on `system.access.table_lineage`), the verdict said "diff could not run" and was raised to `needs_human`, although the diff had run. Now it adds a `lineage-unavailable` hit with a one-line reason and the summary says the blast radius is unknown. It is a `warn` in the default and finance policies and `needs_human` in strict, agent-safe, pii and certified. New policy condition: `lineage_error`.

## 0.1.8 (2026-11-11)

From the first real Slack alert sent from a Databricks workspace:

- Totals checks skip columns where a total means nothing. `numeric-total-moved` and diff summaries now ignore keys and columns that look like IDs, codes, years and other date parts, coordinates, ratings, scores or rates, so a real move in `sum(bed_count)` is no longer buried under `sum(established_year)`. Choose exactly with `measures: [...]` per table, or `measures: all` for the old behaviour. Rules that name a column still check it.
- Slack alerts can @-mention reviewers: map owners to Slack member or group ids under `notify.mentions`.
- Alerts link back to the run: the Databricks job run when the check runs in a job (new), the PR or Actions run in GitHub, or any URL in `BRICKSENTRY_RUN_URL` / `notify.run_url`.
- New `bricksentry test-alert` sends a clearly labelled sample alert to check the webhook setup (`--verdict`, `--owner`). Exits 1 if delivery fails.
- More robust delivery: one retry when Slack or Teams answers 429 or 5xx or the connection fails; clear messages for revoked webhooks (404/410); the webhook URL never appears in an error; Slack text is escaped so table names or values can't break or inject formatting; long reasons are clipped. Teams cards use plain text runs, so names like `my_table_name` show exactly. Bad `notify.on` or `mentions` values give a clear error.
- Config typos are reported. Unknown keys in bricksentry.yml (top level, `tables`, `notify`, `lineage`) print a warning with the closest valid key; also warns about tables without keys, short table names on Databricks, and webhook URLs written into the file.
- `bricksentry-mcp --help` shows `Usage: bricksentry-mcp [OPTIONS]`, and the MCP server reports the bricksentry version to clients (the version used to be empty).

## 0.1.7 (2026-11-11)

- New companion package `bricksentry-mcp`: `uvx bricksentry-mcp --config /full/path/bricksentry.yml` starts the MCP server with everything it needs. Before, `uvx bricksentry mcp` installed the package without its MCP extra and failed, and that is what MCP Registry clients ran. The registry listing now points at `bricksentry-mcp`.
- Setup docs for Claude Code, the Claude desktop app and Cursor now use full config paths, which clients need because they start the server from their own folder.
- `run_demo()` called from Python without DuckDB now raises one clear ImportError with the install command, instead of a CLI error with a traceback (reported in a Databricks notebook test).
- New `agent-safe` policy template for changes written by AI agents.

## 0.1.6 (2026-11-11)

- Slack and Teams alerts. Set `notify:` in bricksentry.yml (or `BRICKSENTRY_SLACK_WEBHOOK` / `BRICKSENTRY_TEAMS_WEBHOOK`) and `assess` / `preview` post the verdict, the rules that fired, the reviewers and a PR link when the verdict reaches `notify.on` (default `needs_human`). `--no-notify` turns it off; `Sentry.notify()` does it from Python; the GitHub Action takes `slack-webhook`, `teams-webhook` and `notify-on`. A failed webhook never changes the verdict.
- Policy templates: `default`, `strict`, `pii`, `certified` and `finance`. Use `policy: builtin:<name>`, `bricksentry init --policy <name>`, or list them with `bricksentry policies`.
- When `preview_write` refuses DROP, TRUNCATE, ALTER, GRANT, REVOKE, VACUUM or RESTORE, the error now says what to do instead.
- Listed in the official MCP Registry as `io.github.bricksentry/bricksentry` (published automatically on each release).
- A complete copy-paste GitHub Actions workflow in the README.

## 0.1.5 (2026-11-10)

- Clearer MCP tool definitions for agents. Each tool now says when to use it versus its sibling tools and what it returns, and every parameter has a description in the tool schema (for example the `candidates` map format for `assess_change`). No behavior changes.

## 0.1.4 (2026-11-10)

From a second end-to-end run on Databricks serverless against a 2-million-row table:

- Fix: a sampled diff (`sample_pct`) could report "no change" when the change sat outside the sample, for example one deleted row in 2 million at a 10% sample. A full-table checksum now guards every sampled diff: if the sample looks identical but the tables differ, the diff re-runs on all rows and says so in the summary (`sample_note`). Identical tables stay sampled and cheap. Turn it off with `sample_guard: false`.
- Fix: `explain_diff` and `safe_to_ship` returned "unknown assessment id" when called from a different `Sentry` instance or process. Assessments are now saved as JSON (default `~/.bricksentry/assessments`, or `assessment_store:` in bricksentry.yml, or env `BRICKSENTRY_STORE`) and loaded by id. Set `assessment_store: false` to keep them in memory only.

## 0.1.3 (2026-11-10)

- Blast radius no longer lists bricksentry's own `preview_write` clones, or tables that Unity Catalog lineage remembers but that no longer exist (when `information_schema` can confirm it). Before, every preview added a stale downstream entry. New lineage settings: `exclude` (name patterns, default `["*__preview_*"]`) and `exclude_dropped` (default true).
- Databricks quickstart notebook (`examples/databricks/bricksentry_quickstart.py`, plus an `.ipynb` copy) that runs the full loop on a demo table in your workspace.

## 0.1.2 (2026-11-10)

- Fix: rows whose key is NULL are now matched null-safely. Before, each such row was counted as both added and removed (found on a real Databricks table with a NULL `state`).
- New `null_keys` policy condition and default `null-keys` rule (warn) when a change introduces rows with a NULL key; summaries mention NULL keys.

## 0.1.1 (2026-11-10)

- New `bricksentry demo` command: the full agent safety loop on a local 20,000-row example in about 30 seconds (`pip install "bricksentry[duckdb]"`). `--report FILE` also writes the pull-request comment.
- Fix: the "Review requested from" line in PR comments no longer runs into the line above it.
- Project links (website, docs, source, issues, changelog) in package metadata.
- CI on Python 3.10 to 3.13, and release automation through PyPI Trusted Publishing.
- CONTRIBUTING, SECURITY and issue templates.

## 0.1.0 (2026-11-10)

- Key-based data diff with SQL pushdown: schema changes, added/removed/changed rows, per-column changes, nulls, numeric totals, duplicate keys, samples, segment attribution, deterministic hash sampling.
- Downstream impact from Unity Catalog lineage system tables (tables, jobs, notebooks, pipelines, dashboards, queries) with owners and tags, plus static lineage for local work.
- Policy-as-code verdicts: pass / warn / needs_human / block.
- `preview_write`: run one write against a disposable shallow clone, with SQL guards.
- Rollback plans from Delta history.
- CLI (`diff`, `impact`, `assess`, `preview`, `rollback`, `mcp`, `init`), markdown PR report, GitHub PR comment, composite GitHub Action.
- MCP server with six agent tools; the same tools as plain Python functions.
- Backends: Databricks SQL warehouse (Statement Execution API) and DuckDB.
