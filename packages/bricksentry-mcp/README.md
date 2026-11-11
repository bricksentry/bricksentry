# bricksentry-mcp

<!-- mcp-name: io.github.bricksentry/bricksentry -->

The MCP server for [bricksentry](https://github.com/bricksentry/bricksentry), the safety check for data changes in the AI era. This package only installs `bricksentry` with its MCP and Databricks extras and gives you one command, so agents can start the server with `uvx`:

```bash
uvx bricksentry-mcp --config /full/path/to/bricksentry.yml
```

**Claude Code**

```bash
claude mcp add bricksentry -- uvx bricksentry-mcp --config /full/path/to/bricksentry.yml
```

**Claude desktop, Cursor and other clients** (`mcpServers` config):

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

Use a full path to `bricksentry.yml`: clients start the server from their own working folder. If an app can't find `uvx`, use its full path (`which uvx`).

`bricksentry-mcp` accepts the same options as `bricksentry mcp`, for example `--transport streamable-http`. Docs: [bricksentry.org/docs/agents](https://bricksentry.org/docs/agents). License: Apache-2.0.
