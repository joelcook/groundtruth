# Troubleshooting

Start with `./gt doctor`. It checks Python, dependencies, your project, and
optional tools, and tells you what to run.

## Setup

**`./gt: Permission denied`**: run `chmod +x gt`, or use `python3 gt ...`.

**"needs Python 3.10 or newer"**: install a newer Python from python.org and run
it explicitly: `python3.12 gt demo`. The `.venv/` it creates remembers it.

**Windows**: use `py gt demo`. Everything else is the same.

**Something's half-installed**: `rm -rf .venv && ./gt demo` rebuilds from scratch.

**"No module named 'mcp.server.fastmcp'"**: you have the `mcp` 2.x SDK.
groundtruth uses 1.x (`mcp>=1.27,<2`). Run `./gt setup`.

## Facts

**"field 'Suite 200' must be letters, numbers, underscores (a comma inside
{braces}...)"**: an unquoted comma in a `{...}` row. Quote the value:
`{address: "1 Main St, Suite 200"}`.

**A phone number shows up as a number, or loses its leading zero**: quote it:
`phone: "555-0100"`, `zip: "02134"`.

**"can't use 'limit' as a parameter name"**: `limit` and `upcoming_only` are
reserved. Use `arg: name` (or anything else).

**I added a tool and the client doesn't see it**: restart the server (or the
client, for stdio). Data changes reload live; new tools need a restart.

**A lookup returns the wrong row**: add `aliases:` to the right row, or narrow
`match:` to the fields that should be searched. `./gt ask --offline "..."` shows
what each tool returns.

**"sources/pages/x.md changed since the last review"**: the evidence behind
some facts or pages changed. `./gt review` lists them; compare with `git diff`,
fix what's stale, then `./gt review --done`.

**"template examples are still live"**: the template's fictional rows and pages
are still being served. Replace them, or `./gt examples --remove`.

## Wiki and evals

**"date 'June 20, 2026' in prose"** (or duration, percentage, phone number...):
move the value to `facts/` and refer to the tool, or justify it with
`<!-- fact-ok: why -->` on the line above.

**"the prompt mentions `get_x`, which isn't a tool"**: rename or remove it in
`prompts/system_prompt.md`. Using `{{FACT_TOOLS}}` there keeps the routing table
in sync automatically.

**"search_wiki doesn't surface 'x' for this question"**: put the words people use
in the page's `title` and `tags`, or add a synonym in `groundtruth.yaml`.

**Live evals fail with "no Anthropic credentials"**: set `ANTHROPIC_API_KEY` or run
`ant auth login`. The free `./gt eval` needs neither.

## Server

**401 unauthorized**: the client isn't sending the key. Use
`Authorization: Bearer <GT_MCP_API_KEY>` (or `x-api-key: <key>`).

**421 / "Invalid Host header" after deploying**: the server has no API key, so it
only accepts localhost by default. Set `GT_ALLOWED_HOSTS=your.host`, or set
`GT_MCP_API_KEY`.

**Port 8765 is busy**: `./gt serve --port 8790`.

**Claude Desktop shows the server as failed**: run the exact command from
`./gt connect claude-desktop` in a terminal. It should sit silently waiting for
input (that's stdio). Any error it prints is the problem.
