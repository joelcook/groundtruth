# MCP Tool Contract

The set of read-only tools the model is allowed to call. Three things must agree
on this contract, or your evals are testing a different assistant than you ship:

1. the MCP server (`groundtruth/mcp.py`) — what's actually exposed
2. the eval harness (`groundtruth/evals.py`) — `TOOL_DISPATCH`
3. the system prompt (`prompts/system_prompt.md`) — which facts route where

`scripts/check_mcp_runtime.py` verifies (1) against `EXPECTED_TOOLS` at runtime.

## Hard-fact tools (exact values; never paraphrased)

| tool | signature | returns |
|---|---|---|
| `list_branches` | `()` | branches with address, phone, region |
| `get_hours` | `(branch="central", day=None)` | open/close per day (24h), or closed |
| `list_events` | `(category=None, upcoming_only=True, limit=25)` | events, newest first |
| `get_event` | `(query)` | events matching a title/category/notes string |
| `list_programs` | `(audience=None)` | programs, optionally by audience |
| `get_program` | `(name_or_id)` | one program incl. `registration_required` |
| `get_fee` | `(fee_type=None)` | fees/fines in dollars |
| `get_contact` | `(department=None)` | phone/email by department |

Every row returned carries a `source_url` (or `source_path`) so the answer can be
traced to where the fact came from.

## Narrative tools (explanation; approximate is fine)

| tool | signature | returns |
|---|---|---|
| `search_wiki` | `(query, limit=5)` | ranked source-cited snippets |
| `read_wiki_page` | `(path_or_slug, max_chars=10000)` | one full page by slug/path/title |

## Annotations & transport

- All tools are annotated `readOnlyHint: true`, `openWorldHint: false`.
- Transport: streamable-HTTP at `/mcp` (or stdio with `--stdio`).
- Auth: when `GT_MCP_API_KEY` is set, requests must send
  `Authorization: Bearer <key>` or `x-ms-apikey: <key>`; otherwise `401`.

## Changing the contract

When you add, remove, or rename a tool: update `groundtruth/mcp.py`, the
`TOOL_DISPATCH` map and any schemas in `groundtruth/evals.py`, `EXPECTED_TOOLS`
in `scripts/check_mcp_runtime.py`, the routing table in
`prompts/system_prompt.md`, and add/curate eval rows that exercise it. Then run
the gates.
