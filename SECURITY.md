# Security policy

bricksentry runs SQL against your data warehouse and can be called by AI agents, so we take reports seriously.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue for security problems.

Especially relevant:

- Any way for `preview_write` to modify a table other than its sandbox clone
- SQL injection through table names, keys or config values
- Ways for an agent to obtain a `pass` verdict that the policy should not allow

You'll get an acknowledgement as soon as possible, and a fix or mitigation plan once the report is confirmed.

## Supported versions

Only the latest release receives security fixes during the 0.x series.
