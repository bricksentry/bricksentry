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
