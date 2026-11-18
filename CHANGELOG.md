# Changelog

## Unreleased

- **Views and SQL functions are held to `agent_tables`.** A listed view over an unlisted table, or a SQL function whose body reads one (`SET x = get_secret()`), let an agent's preview read that table and learn its row counts (values stayed hidden). bricksentry now reads every view's definition and every user function's body from Unity Catalog (`system.information_schema.views` and `.routines`) and checks the tables inside, recursively; an unqualified function name is matched in every schema. A definition that can't be read, or a catalog that can't be read, means the preview is refused. Found in the pre-release check of the plan check below, which can't see inside views or functions on serverless.
- **A second opinion from the engine's query plan.** On Databricks, before an agent's preview runs, bricksentry also runs `EXPLAIN EXTENDED` (planning only) and checks every table the plan names; a plan that can't be read, or doesn't contain the target, means the preview is refused.
- **Faster pinned diffs.** Reading the schema at the pinned version now doubles as the time-travel probe, and only the candidate's version is re-checked afterwards (a pinned base can't affect the result), so each diff runs three fewer metadata queries. A base that moves during the check is no longer noted, because its pinned reads are unaffected.
- bricksentry is now in **public beta**: the package is marked Beta, and the README says what you can rely on during the beta and what may still change before 1.0.

## 0.1.28 (2026-11-17)

Security fixes from live-test round 9 on 0.1.27 (MERGE, allowlist bypasses, metadata injection, remote access, governance features). Agents still never received production values; these close the remaining ways to learn about tables they shouldn't. Every round-9 attack was re-run on the fixes and refused or handled; the 15-scenario battery passed 15 of 15 on Databricks, and a live Unity Catalog row filter gave `needs_human` instead of a silent pass.

- **`agent_tables` covers every table an agent touches.** It only checked the target, so an agent could read a forbidden table inside an allowed write (subquery, join, CTE, `MERGE ... USING`, `INSERT ... SELECT`) or name it as the candidate in `assess_change`, and learn its row counts. Statements are now parsed with a real SQL parser (sqlglot, a new dependency) and every table they read must be listed; agent-named candidates too; `explain_diff` and `safe_to_ship` refuse assessments of unlisted tables. What can't be checked by name is refused: table functions (`table_changes`, `read_files`), `IDENTIFIER(...)`, path-based tables, and SQL that doesn't parse. **If you use `agent_tables`:** sources of MERGE and INSERT ... SELECT must now be listed too, written with their full name (e.g. `main.sales.updates`), or agent previews that read them are refused. The CLI and Python API are unaffected.
- **Column masks and row filters can't pass silently.** A masked column with 50 real changes diffed as unchanged. bricksentry now reads masks and row filters from Unity Catalog: masked columns aren't compared (`columns-not-compared`) and a row filter on either table gets `needs_human` (`row-filter`).
- **Behavior change: the MCP server needs a token on the network.** Over HTTP on anything but localhost it had no authentication, so anyone who could reach the port could call the tools. It now refuses to start there unless `BRICKSENTRY_MCP_TOKEN` is set (clients send `Authorization: Bearer <token>`) or `--insecure-no-auth` is passed for a network behind your own authenticating proxy. Databricks Apps needs no token (its sign-in is in front). Localhost gets DNS-rebinding protection again; `BRICKSENTRY_ALLOWED_HOSTS` lists extra host names.
- **Free text in tags and owners no longer reaches agents.** Tag values such as "IGNORE ALL PREVIOUS INSTRUCTIONS…" were passed to agents verbatim. Short, plain names and values (`pii`, `true`, `finance-team`) still are; longer or free text shows as `[text hidden: N chars]`, also where it was copied into summaries. Verdicts were never affected.
- `assess --from-git` gives one clear line (exit 2) when git fails or isn't installed, instead of a traceback.
- Quoted-value redaction in agent errors no longer eats apostrophes in words ("can't") and catches values that contain one ('O'Brien').
- New validation scenario `agent_allowlist` (the battery is now 15).

## 0.1.27 (2026-11-17)

A safety release. A live round of leak, data-shape and race tests on 0.1.26 found four problems; two could mislead. A verification round re-ran every original attack on the fixes: no production value reached an agent, mid-diff rewrites returned the pinned state 3 of 3 times, MAP diffs and the key-order trap were exact, and the 14-scenario battery passed 14 of 14. Pinning makes a diff slower (a 1M-row diff took about 10 s instead of 5 s); that will be reduced in a later release.

- **Agents never get production values.** `preview_write` returned up to 5 before→after values per changed column, so an agent with no access to a table could "update" chosen rows and read their old values (SSNs, salaries) back, because bricksentry does the reading. Every agent tool (MCP and `bricksentry.agent_tools`) now returns counts, columns, schema, null counts, percentages and verdicts, never values: no example values on either side (an agent can copy a value into the "new" side), no absolute totals (a total over one row is the value; percentages are rounded to one decimal), no segment values, and quoted values are removed from engine errors. Humans keep the detail in the CLI, Python API and reports, except values in `pii`-tagged tables and columns, which reports never show. Rule reasons no longer contain totals ("sum(amount) moved from 0").
- **`agent_tables`** in bricksentry.yml lists the tables agents may assess or preview (globs, any case); others are refused. Counts alone can still be probed (`DELETE ... WHERE ssn LIKE '1%'`), which only limiting tables can stop. Every agent `assess_change` / `preview_write` is logged to `bricksentry.audit` and `audit.jsonl` in the assessment store. The README has a table of what each tool can reveal.
- **Every diff reads one consistent version of each table.** The diff runs several queries; a table rewritten between them mixed two states (a candidate replaced mid-diff gave changed=49 when the old state had 5 and the new 50). Both tables are now pinned to their Delta version at the start (`VERSION AS OF`, schema included), and a candidate that changes during the check gets `needs_human` ("changed-during-check"). Objects that can't time travel are checked before and after.
- **MAP columns no longer fail the whole diff.** Spark can't compare or hash maps, so one MAP column gave `diff-failed`. Maps are compared by their entries sorted by key (equal maps written in another order are equal); maps nested in structs or arrays as JSON. A column the engine still can't compare is left out with a note and the check asks a human (`columns-not-compared`), instead of losing the diff.
- **Float rounding.** FLOAT/DOUBLE stay exact by default, but a column that changed everywhere while its total stayed the same gets a note that it's likely rounding. New opt-in `float_tolerance` (relative, e.g. `1e-9`); NaN, infinity and NULL still compare exactly; DECIMAL is always exact.
- **Key names match case-insensitively** (`keys: [ID]` finds `id`), like the engine.
- New validation scenarios `agent_sees_no_values` and `map_column` (the battery is now 14).

## 0.1.26 (2026-11-17)

Fixes for what two worst-case live tests of 0.1.25 on Databricks found. A pre-release check passed on two Databricks Free Edition workspaces (47 of 47 on the first), including the 12-scenario validation battery and before/after repros of every fix.

- **A changed copy of a duplicated key no longer shifts how the other copies pair.** 0.1.23 numbered the copies of a key by row hash and matched copy *n* with copy *n*, so one changed copy could move every copy after it: 2,000 rows sharing a NULL key with 2 real changes reported over a thousand changed rows (50,000 such rows: 36,563). Identical copies are now matched first; only the copies left over are paired. The same test now reports exactly 2 changed. New validation scenario `null_keys_changed`.
- **A total moving away from zero now fires `sum_change_pct_gt`.** A column whose total went from 0 to 5 had no percentage change, so the rule never matched. It now matches, with the reason "sum(col) 0 -> 5", and the plain-English explanation lists it under totals.
- **An overflowing exact total no longer loses the whole diff.** If a `DECIMAL(38)` total overflows, totals fall back to `DOUBLE` (or are skipped), the row counts and changes are still reported, and the diff notes it.
- **The rollback warning lists newer table changes that aren't data** (renamed columns, table properties, constraints): `RESTORE` undoes those too. Maintenance (OPTIMIZE, VACUUM, ANALYZE) is still ignored.
- **Clearer policy mistakes.** A verdict typo (`then: blok`) names the valid verdicts; verdicts are accepted in any case; `policy: strict` without `builtin:` says to write `builtin:strict`; a policy with no rules warns that every change will pass.
- **Behavior change: no-op pull requests to pii and certified-fed tables now pass.** The default rules `pii-table` and `certified-downstream` now have `any_change: true`, like `dashboard-downstream`: they fire on any added, removed or changed row, any schema change or new duplicate keys, but not when the candidate is identical to production (before, that got `needs_human` with nothing to review). A failed or missing diff still counts as a change, for every `any_change` rule. To require a review regardless, add a custom rule such as `{name: pii-review, when: {tag: pii}, then: needs_human}`. New validation scenario `pii_noop` (identical rebuild: pass; 1-row edit: needs_human). Found in a live test of 0.1.25.
- `preview_write` / `bricksentry preview` show one line when the clone or the statement fails ("The statement failed on the preview clone, so production was not touched: …") instead of the engine's full error. MCP tool errors keep only the first line.
- The MCP tools `whats_downstream` and `rollback_plan` report a mistyped or missing table as "Table … not found" instead of an empty downstream list (which read as "nothing is downstream") or advice to take a snapshot. Found in the pre-release check.
- A misspelled key under `backend:` in `bricksentry.yml` (say `path:` instead of `database:`) is rejected with the valid keys, instead of being ignored and connecting somewhere else. Found in the pre-release check.

## 0.1.25 (2026-11-16)

- **Validation you can reproduce.** `VALIDATION.md` records every live test on Databricks with its numbers, the results that weren't perfect and the bugs each test found. `bricksentry.validation.run_validation()` and the new notebook `examples/databricks/bricksentry_validation.py` run the same ten risky changes on your own workspace in a scratch schema and drop everything afterwards. A new issue form, "Validation report", collects results from other setups.
- Fix: a sampled diff (`sample_pct`) skipped the full-table check when the schema had also changed, so changed values in a retyped column outside the sample could go uncounted. The verdict still flagged the type change. The check now runs whenever the sample shows no row changes. Found by the new validation battery.
- The battery passed 10 of 10 on Databricks (111 s on a 2X-Small serverless warehouse), including the two scenarios that need Delta history. The certified-table scenario now keeps the column type, so its strict verdict comes from `certified-table-changed` alone (on Databricks, doubling a decimal widened its type).

## 0.1.24 (2026-11-16)

Small fixes from two adversarial live tests of 0.1.23 on Databricks, including the job task running inside a real Databricks job.

- The job task log no longer ends with IPython's "To exit: use 'exit', 'quit', or Ctrl-D." after a failing verdict (Databricks runs `spark_python_task` scripts inside IPython). The exit code is unchanged.
- `check-last-change` and the job task report a mistyped or missing table in one line ("Table main.hr.employes not found. Check the catalog, schema and table name…"), and a missing permission as "No permission to read …", instead of the engine's full error with the echoed SQL and unrelated hints. Other history errors keep only their first line. Found in a live test of 0.1.23.
- `bricksentry test-alert --help` said it exits 1 when no webhook is configured; it exits 2 (configuration error), which the help now says. Found in the live test of 0.1.23.
- README: "Validated on" covers the live run of 0.1.23.
- README: the default policy is lenient on changes to a certified table itself (a doubled total only warns); use `builtin:certified` or `builtin:finance` to require a human. From an adversarial live test of 0.1.23.

## 0.1.23 (2026-11-16)

From a review of accuracy, auditability and documentation:

- **Exact totals.** Sums of decimal and integer columns are added as `DECIMAL(38, x)` instead of `DOUBLE`, so totals on large tables don't drift by cents and big integer columns can't overflow. Floating-point columns are still summed as `DOUBLE`.
- **Duplicated keys are matched one to one.** When a key appears more than once (including many rows with a NULL key, which match each other), copies are numbered and copy *n* is matched with copy *n*. Before, every copy matched every other copy: 2,000 NULL-key rows on each side joined into 4,000,000 pairs, and a duplicated row counted as a second match instead of an added row. A bad MERGE that duplicates rows now shows them as added, and the diff notes that copies were paired. Tables without duplicate keys run exactly as before.
- **Audit trail on every assessment:** `created_at` (UTC), `bricksentry_version`, `policy_sha256` (a fingerprint of the rules behind the verdict) and `triggered_by` (`BRICKSENTRY_ACTOR`, or the GitHub actor in GitHub Actions). In the JSON and the pull-request report footer. Older saved assessments still load.
- README: permissions needed per feature, what a check costs, and the audit trail.
- Links to the agent docs no longer end in `).` where READMEs and notebooks are shown as plain text (some directories turned them into a broken link). Package description and keywords mention change impact analysis and deployment guardrails.

## 0.1.22 (2026-11-15)

Security hardening, following a review on the Canopii MCP Trust Index:

- **Outbound requests are checked.** Slack/Teams webhooks and the GitHub API must use `https://`, must not resolve to loopback, private, link-local or cloud-metadata addresses, and redirects are never followed. A webhook that fails the check is skipped with a one-line reason; the verdict is unchanged. Set `BRICKSENTRY_ALLOW_PRIVATE_URLS=1` for targets on a private network such as GitHub Enterprise Server.
- **Container image** for the MCP server: `deploy/container/Dockerfile` (non-root user, config mounted read-only). CI builds it on every push and checks that it lists the MCP tools.
- `SECURITY.md` explains the network behaviour, why the MCP server listens on `0.0.0.0` only on Databricks Apps, and how releases are published.
- `bricksentry preview` with no `keys` configured for the table now exits with code 2 (configuration error), as the CLI help and README document; 0.1.21 exited 1. Found in the live test of 0.1.21.

## 0.1.21 (2026-11-13)

- `bricksentry preview` printed a raw Python traceback when it refused a statement (DROP, TRUNCATE, ALTER, GRANT, several statements). It now prints the refusal and what to do instead, and exits with code 2. A missing `keys` setting is also reported as one clear line. Agents were not affected: the MCP tools already returned the refusal as an error message. Found in a live test on Databricks.
- The test suite passes from the source distribution: the two checks that read repo-only files (the issue form, the companion package) skip when those files aren't there.
- README: GitHub Action examples use `@v0.1.20`, and "Validated on" now covers the live runs up to 0.1.20.

## 0.1.20 (2026-11-13)

From a question on X: what happens to a valid write that lands after the bad change you want to undo?

- **"Newer data changes exist" warning.** `check-last-change` reads the table history again after the check. If data changes were committed after the checked version (because you passed `--version`, or a write landed while the check ran), it warns right above the `RESTORE` that restoring also undoes them, and lists them. The warning appears in the CLI, the job task log, the pull-request report, Slack and Teams alerts, and the JSON (`rollback_warning` on each table, `newer_changes` on the change). OPTIMIZE and VACUUM don't count.
- README: before acting on an older alert, run `check-last-change --version N` again to see what was written since.

## 0.1.19 (2026-11-12)

- `bricksentry demo` showed black boxes instead of colored verdict labels in macOS Terminal, which doesn't support 24-bit color. Terminals that don't set `COLORTERM=truecolor` now get the standard named colors; the others keep the website's colors.
- When the scratch schema's catalog doesn't exist, `check-last-change`, the job task and the demo task say so and how to fix it, instead of a raw `NO_SUCH_CATALOG` error.
- README: how to run the demo inside a Databricks or Jupyter notebook (`%pip install`, then `run_demo()`).
- The job template pins 0.1.19 by default.

Bundle template changes, already live because `bundle init` reads the template from GitHub:

- `tables` is optional: leave it empty to deploy only the demo job and see a block before choosing your own tables. The only input without a default is now `warehouse_id`. The demo question comes first.
- The template README documents non-interactive init (`--config-file`) for CI, scripts and AI agents.
- `scratch_schema` defaults to `<your default catalog>.bricksentry_scratch` instead of `main.bricksentry_scratch`. Workspaces without a `main` catalog had their first demo run fail with "catalog not found" instead of the intended BLOCK.

## 0.1.18 (2026-11-12)

See a block in your own workspace within minutes of `bundle init`.

- The job template asks whether to add a **demo job** (default yes). `databricks bundle run bricksentry_demo` creates a 20,000-row demo table in your scratch schema, runs a MERGE whose ON condition compares region case-sensitively (so 25 orders are inserted again as duplicates), then checks that change. The run ends red with BLOCK, the rules that fired and the `RESTORE` that undoes it, plus the Slack alert if you set one up. It writes only to `bricksentry_demo_*` tables.
- `python -m bricksentry.job_demo`: the task that makes the demo change.
- Slack is now presented as optional in the template prompt, and the success message lists the next commands.
- The template README no longer says the trigger is paused in the dev target: the job sets `pause_status: UNPAUSED` on its trigger, which development mode respects.

## 0.1.17 (2026-11-12)

Not published to PyPI; these changes shipped in 0.1.18.

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
