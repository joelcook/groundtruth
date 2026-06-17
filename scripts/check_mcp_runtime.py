#!/usr/bin/env python3
"""Runtime contract smoke test for the MCP server.

Connects exactly like the production client does, runs the MCP handshake, checks
the advertised tool list, and calls one safe read-only tool. This is the probe
the deploy/preflight scripts run to confirm a live endpoint actually works.

    # against a running HTTP server:
    python scripts/check_mcp_runtime.py --endpoint http://127.0.0.1:8765/mcp [--api-key KEY]

    # against the stdio transport (launches the server itself):
    python scripts/check_mcp_runtime.py --stdio

Exits 0 if every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {
    "list_branches", "get_hours", "list_events", "get_event", "list_programs",
    "get_program", "get_fee", "get_contact", "search_wiki", "read_wiki_page",
}


async def _check(session) -> int:
    init = await session.initialize()
    name = init.serverInfo.name
    print(f"ok initialize: serverInfo.name={name}")

    tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    print(f"ok tools/list: {len(names)} tools advertised")
    missing = EXPECTED_TOOLS - names
    if missing:
        print(f"FAIL: missing expected tools: {sorted(missing)}", file=sys.stderr)
        return 1

    result = await session.call_tool("list_branches", {})
    rows = result.structuredContent if result.structuredContent is not None else result.content
    count = len(rows.get("result", [])) if isinstance(rows, dict) else len(rows or [])
    print(f"ok tools/call: list_branches returned {count} row(s)")
    return 0


async def _run_http(endpoint: str, api_key: str | None) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with streamablehttp_client(endpoint, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            rc = await _check(session)
    if rc == 0:
        print(f"ok MCP runtime contract: {endpoint}")
    return rc


async def _run_stdio() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "scripts" / "serve_mcp.py"), "--stdio"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            rc = await _check(session)
    if rc == 0:
        print("ok MCP runtime contract: stdio")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--stdio", action="store_true")
    args = ap.parse_args()
    if args.stdio:
        return anyio.run(_run_stdio)
    return anyio.run(_run_http, args.endpoint, args.api_key)


if __name__ == "__main__":
    raise SystemExit(main())
