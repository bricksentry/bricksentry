# Security policy

bricksentry runs SQL against your data warehouse and can be called by AI agents, so we take reports seriously.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue for security problems.

Especially relevant:

- Any way for `preview_write` to modify a table other than its sandbox clone
- SQL injection through table names, keys or config values
- Ways for an agent to obtain a `pass` verdict that the policy should not allow

You'll get an acknowledgement as soon as possible, and a fix or mitigation plan once the report is confirmed.

## How bricksentry handles your environment

**Network.** bricksentry talks to your SQL warehouse through the Databricks SDK, and makes only two other kinds of outbound HTTP call: Slack/Teams webhooks you configure, and the GitHub API when you ask it to comment on a pull request. Both must use `https://`, must not resolve to loopback, private, link-local or other non-public addresses (so a misconfigured URL can't reach internal services or cloud metadata endpoints), and redirects are never followed. If a legitimate target is on a private network, such as GitHub Enterprise Server or a webhook relay in your VPC, set `BRICKSENTRY_ALLOW_PRIVATE_URLS=1`. Webhook URLs are never written to logs or error messages.

**MCP server.** Over HTTP it listens on `127.0.0.1` by default. It listens on `0.0.0.0` only on Databricks Apps (when `DATABRICKS_APP_PORT` is set), because the platform requires it and puts its own authentication in front. `--read-only` removes `preview_write`, so a shared server never writes, not even to a sandbox clone. To isolate it further, run it in a container: see `deploy/container/Dockerfile` (non-root user, config mounted read-only).

**Writes.** The only tool that writes, `preview_write`, runs one statement on a disposable shallow clone and refuses DROP, TRUNCATE, ALTER, GRANT and multiple statements. Verdicts come from your policy file, never from an LLM.

**Secrets.** Credentials come from the environment, the Databricks SDK's own configuration or a secret scope; bricksentry never stores them.

**Releases.** Releases are built and published by GitHub Actions with PyPI trusted publishing (no long-lived upload token), from a tagged commit on `main`. Releases published by the workflow carry PyPI provenance attestations, which you can check on each file's page on PyPI.

## Supported versions

Only the latest release receives security fixes during the 0.x series.
