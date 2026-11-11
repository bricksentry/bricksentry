# Run the bricksentry MCP server as a Databricks App

Host bricksentry inside your workspace so any agent (Claude Code, Cursor, Agent Bricks, your own
LangGraph or OpenAI Agents SDK code) can call it over HTTPS. bricksentry runs as the app's service
principal: there are no tokens to manage, Unity Catalog grants decide what it can read, and access to
the server itself is controlled with the app's permissions. Unity AI Gateway can monitor its traffic
next to your other MCP servers.

This folder is a complete app:

| File | What it is |
|---|---|
| `app.yaml` | Starts `bricksentry mcp --transport streamable-http --read-only`. bricksentry listens on `0.0.0.0:$DATABRICKS_APP_PORT` by itself. |
| `requirements.txt` | The pinned bricksentry release. |
| `bricksentry.yml` | Backend, policy (`builtin:agent-safe`) and the tables agents may check. **Edit `tables:` first.** |
| `databricks.yml` | Asset Bundle that creates the app with a SQL warehouse resource. |
| `grants.sql` | The Unity Catalog grants for the app's service principal. |

## Deploy (about 10 minutes)

You need the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) logged in to your workspace,
permission to create apps, and a SQL warehouse ID.

```bash
# 1. Copy this folder and edit bricksentry.yml: list your tables and their keys.
cp -r deploy/databricks-app my-bricksentry-app && cd my-bricksentry-app

# 2. Create and start the app.
databricks bundle deploy --var warehouse_id=<your-sql-warehouse-id>
databricks bundle run bricksentry_mcp
```

3. **Grant access.** Open the app in **Compute → Apps → mcp-bricksentry**, copy its service principal's
   application ID, replace `<app-sp>` in `grants.sql`, and run it in the SQL editor.
4. **Check it.** The app page shows the URL. The MCP endpoint is `https://<app-url>/mcp`. The app log
   should show `bricksentry MCP server (streamable-http, read-only) on http://0.0.0.0:<port>/mcp`.

Without the bundle: create an app in the UI (**Compute → Apps → Create app → Custom**), add a SQL
warehouse resource with the key `sql-warehouse` and **Can use**, then `databricks sync . <workspace-path>` and
`databricks apps deploy mcp-bricksentry --source-code-path <workspace-path>`.

## Connect an agent

Databricks Apps sit behind workspace sign-in, so clients send a Databricks OAuth token.

**Agent code in Databricks** (recommended), with `pip install databricks-mcp`:

```python
from databricks.sdk import WorkspaceClient
from databricks_mcp import DatabricksMCPClient

client = DatabricksMCPClient(server_url="https://<app-url>/mcp", workspace_client=WorkspaceClient())
print([t.name for t in client.list_tools()])
```

**Claude Code or another HTTP client** on your laptop:

```bash
TOKEN=$(databricks auth token --host https://<workspace-host> | jq -r .access_token)
claude mcp add --transport http bricksentry https://<app-url>/mcp --header "Authorization: Bearer $TOKEN"
```

OAuth tokens expire after about an hour; refresh with the same command. Give each agent identity
**Can use** on the app (app page → Permissions).

## Read-only or previews

`--read-only` (the default here) leaves out `preview_write`, so the app only reads: agents use
`whats_downstream`, `rollback_plan`, `assess_change`, `explain_diff` and `safe_to_ship`. To let agents
preview a write on a disposable shallow clone, remove `--read-only` from `app.yaml` and run the
sandbox grants at the end of `grants.sql`.

## Unity Catalog MCP Services

Unity Catalog can register an MCP server as an *MCP Service* governed by Unity AI Gateway, but that is
for servers hosted outside Databricks: apps on Databricks Apps can't be registered as MCP Services, and
Databricks says to connect to them directly instead. For bricksentry, the app above is the
recommended setup. If you do host it elsewhere, it needs a public HTTPS URL with Streamable HTTP, and
its own Databricks credentials for the SQL warehouse; see
[Register an external MCP server](https://docs.databricks.com/aws/en/ai-gateway/register-mcp-service).

## Notes

- Saved assessments live in `/tmp` inside the app, so `safe_to_ship` and `explain_diff` find an
  assessment from the same app instance. Call them right after `assess_change`, as agents normally do.
- Every check runs as the app's service principal, so all callers see what that principal can read.
  Grant it only the schemas agents should check.
- App compute is billed at Databricks Apps rates.
