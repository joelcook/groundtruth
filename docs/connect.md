# Connect a client

`./gt connect` prints all of this with your real paths filled in. `./gt connect
CLIENT` prints one. Local clients start the server themselves over stdio, so
there's nothing to keep running.

## Claude Desktop

```bash
./gt connect claude-desktop --install
```

That adds your assistant to Claude Desktop's config (backing up the old file
first). Restart Claude Desktop. Or paste the printed JSON into
`claude_desktop_config.json` yourself.

## Claude Code

```bash
claude mcp add my-assistant -- /path/to/groundtruth/.venv/bin/python -m groundtruth -p /path/to/my-assistant serve --stdio
```

Add `--scope project` to share it with your team through `.mcp.json`.

## Codex CLI

```bash
codex mcp add my-assistant -- /path/to/groundtruth/.venv/bin/python -m groundtruth -p /path/to/my-assistant serve --stdio
```

## Cursor and Windsurf

`./gt connect cursor --install` (or `windsurf`), or paste the JSON into
`~/.cursor/mcp.json` / `~/.codeium/windsurf/mcp_config.json`.

## VS Code (Copilot agent mode)

Paste the printed JSON into `.vscode/mcp.json` in your workspace.

## Hosted platforms

These need the HTTP server reachable on the internet over HTTPS. See
[deploy.md](deploy.md).

**Copilot Studio**: add a Model Context Protocol tool to your agent with your
server's `/mcp` URL, and send your key as the header
`Authorization: Bearer <GT_MCP_API_KEY>` (`x-api-key` and `x-ms-apikey` work
too). Paste `prompts/system_prompt.md` into the agent's instructions, so the
agent and your evals agree.

**ChatGPT and claude.ai**: add a custom connector pointing at your `/mcp` URL.
These currently support OAuth or no authentication, not an API-key header.
groundtruth serves public knowledge, so a common setup is no `GT_MCP_API_KEY`,
`GT_ALLOWED_HOSTS=your.host`, behind HTTPS. Check each platform's current
connector docs.

**Your own app**: any MCP client library. Streamable HTTP at `/mcp`, the
`Authorization: Bearer` header, health at `/healthz`. Or skip MCP and call
`groundtruth.server.call_tool(project, name, args)` from Python.

## Check it

```bash
./gt watchdog                                   # starts the server over stdio and tests it
./gt watchdog --url https://your.host/mcp       # tests a deployed one
```
