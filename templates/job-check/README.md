# Databricks Asset Bundle: check every pipeline run

```bash
databricks bundle init https://github.com/bricksentry/bricksentry --template-dir templates/job-check
cd bricksentry_check && databricks bundle deploy
```

You're asked for the tables to watch, a SQL warehouse ID, a scratch schema, the policy and
(optionally) a Slack webhook secret. The bundle deploys a serverless job with a table update
trigger: when a watched table changes, the job checks the change (`bricksentry.job`, which uses
`bricksentry check-last-change`), alerts, and prints the `RESTORE` that undoes it. The generated
README shows how to put the same check inside your own pipeline job so a `block` stops the
tasks downstream.

## Without prompts (CI, scripts, AI agents)

`bundle init` asks its questions interactively. To answer them from a file instead:

```bash
cat > bricksentry-init.json <<'JSON'
{"project_name": "bricksentry_check", "demo": "yes", "tables": "",
 "warehouse_id": "<your SQL warehouse ID>", "scratch_schema": "main.bricksentry_scratch"}
JSON
databricks bundle init https://github.com/bricksentry/bricksentry --template-dir templates/job-check \
  --config-file bricksentry-init.json
```

Every other input has a default; the full list with descriptions is in
[`databricks_template_schema.json`](databricks_template_schema.json). Leave `tables` empty to
deploy only the demo job.
