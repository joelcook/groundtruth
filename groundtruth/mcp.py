"""The MCP server — the read-only tool surface the model actually talks to.

The model never imports ``groundtruth.db`` or reads files off disk. It speaks
MCP, and this is the whole contract: a handful of read-only tools, split clean
between **hard facts** (the database — exact, harm-if-wrong) and **narrative**
(the wiki — approximate, explanatory).

    hard facts:  get_hours  list_events  get_event  list_programs  get_program
                 get_fee    get_contact  list_branches
    narrative:   search_wiki  read_wiki_page

Run it:
    python scripts/serve_mcp.py                 # streamable-HTTP on :8765/mcp
    python scripts/serve_mcp.py --stdio         # for a local MCP client

Auth: if GT_MCP_API_KEY is set, every HTTP request must send
``Authorization: Bearer <key>`` (or ``x-ms-apikey: <key>``). Unset = open, for
local dev only.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from groundtruth import db, observability, search  # noqa: E402

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

mcp = FastMCP(
    "groundtruth",
    instructions=(
        "Grounded knowledge tools for a public library. Use the hard-fact tools "
        "for any date, dollar amount, opening time, phone number, or "
        "registration requirement — never state those from memory. Use "
        "search_wiki / read_wiki_page for how/why/process questions."
    ),
)


def _logged(name: str, fn, **kwargs):
    """Call a backing function, timing and logging the tool invocation."""
    rid = observability.new_request_id()
    t0 = time.perf_counter()
    ok = True
    result = None
    try:
        result = fn(**kwargs)
        return result
    except Exception:
        ok = False
        raise
    finally:
        count = len(result) if isinstance(result, (list, dict)) else None
        observability.log_tool_call(
            name, kwargs, ok=ok, ms=(time.perf_counter() - t0) * 1000,
            result_count=count, request_id=rid,
        )


# ----------------------------------------------------------- hard-fact tools

@mcp.tool(annotations=READ_ONLY)
def list_branches() -> list[dict]:
    """List all library branches with address, phone, and region."""
    return _logged("list_branches", db.list_branches)


@mcp.tool(annotations=READ_ONLY)
def get_hours(branch: str = "central", day: str | None = None) -> dict:
    """Opening hours for a branch (default 'central'), optionally a single day.
    Use this for any 'what time / when is X open / is it open on …' question."""
    return _logged("get_hours", db.get_hours, branch=branch, day=day)


@mcp.tool(annotations=READ_ONLY)
def list_events(category: str | None = None, upcoming_only: bool = True,
                limit: int = 25) -> list[dict]:
    """List events (story-time, workshop, closure, book-sale, program).
    Use for 'what's coming up / any events' questions."""
    return _logged("list_events", db.list_events, category=category,
                   upcoming_only=upcoming_only, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def get_event(query: str) -> list[dict]:
    """Find specific events by name/category (e.g. 'story time', 'book sale').
    Use for any 'when is <event>' question — dates are facts, never guess them."""
    return _logged("get_event", db.get_event, query=query)


@mcp.tool(annotations=READ_ONLY)
def list_programs(audience: str | None = None) -> list[dict]:
    """List ongoing programs/services, optionally by audience (kids/teens/adults)."""
    return _logged("list_programs", db.list_programs, audience=audience)


@mcp.tool(annotations=READ_ONLY)
def get_program(name_or_id: str) -> dict | None:
    """Look up one program (e.g. 'summer reading', 'tech help') including whether
    registration is required."""
    return _logged("get_program", db.get_program, name_or_id=name_or_id)


@mcp.tool(annotations=READ_ONLY)
def get_fee(fee_type: str | None = None) -> list[dict]:
    """Fees and fines in dollars (printing, replacement card, meeting room,
    overdue). Every dollar amount the assistant states MUST come from here."""
    return _logged("get_fee", db.get_fee, fee_type=fee_type)


@mcp.tool(annotations=READ_ONLY)
def get_contact(department: str | None = None) -> list[dict]:
    """Phone/email by department (reference, youth-services, circulation,
    accessibility). Contact details are facts and must cite a source."""
    return _logged("get_contact", db.get_contact, department=department)


# ----------------------------------------------------------- narrative tools

@mcp.tool(annotations=READ_ONLY)
def search_wiki(query: str, limit: int = 5) -> list[dict]:
    """Search curated wiki pages for how/why/process/policy context. Returns
    source-cited snippets. Do NOT use for exact dates, dollars, hours, or phones."""
    return _logged("search_wiki", search.search_wiki, query=query, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def read_wiki_page(path_or_slug: str, max_chars: int = 10000) -> dict | None:
    """Read one wiki page in full by slug, path, or title (e.g. 'fines-and-fees')."""
    return _logged("read_wiki_page", search.read_wiki_page,
                   path_or_slug=path_or_slug, max_chars=max_chars)


# ----------------------------------------------------------- auth + server

class BearerAuthMiddleware:
    """ASGI middleware enforcing a shared bearer token when GT_MCP_API_KEY is set."""

    def __init__(self, app, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        presented = None
        auth = headers.get(b"authorization")
        if auth and auth.lower().startswith(b"bearer "):
            presented = auth[7:].decode().strip()
        elif headers.get(b"x-ms-apikey"):
            presented = headers[b"x-ms-apikey"].decode().strip()
        if presented != self.api_key:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self.app(scope, receive, send)


def main() -> int:
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
        return 0

    import uvicorn

    host = os.environ.get("GT_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("GT_MCP_PORT", "8765"))
    api_key = os.environ.get("GT_MCP_API_KEY", "").strip()

    app = mcp.streamable_http_app()
    if api_key:
        app = BearerAuthMiddleware(app, api_key)
        print(f"groundtruth-mcp: auth ON, listening on http://{host}:{port}/mcp", file=sys.stderr)
    else:
        print(f"groundtruth-mcp: auth OFF (dev), listening on http://{host}:{port}/mcp", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
