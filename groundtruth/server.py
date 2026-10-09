"""The MCP server: the only thing the model ever talks to.

Its tools are generated from your project:
  * one tool per entry in each ``facts/*.yaml`` (``get_fee``, ``get_hours``...)
  * ``search_wiki`` and ``read_wiki_page`` for how/why questions

Every tool is read-only. Fact data and wiki pages reload live when you edit
them; adding a *new* tool needs a restart.

    ./gt serve                 # streamable HTTP on http://127.0.0.1:8765/mcp
    ./gt serve --stdio         # for Claude Desktop, Claude Code, Cursor, Codex...

Environment:
    GT_MCP_API_KEY      require ``Authorization: Bearer <key>`` (or ``x-api-key`` /
                        ``x-ms-apikey``) on every HTTP request. Set it before you
                        expose the server anywhere.
    GT_ALLOWED_HOSTS    comma-separated Host headers to accept, e.g.
                        ``mcp.example.org``. Only needed without an API key; with a
                        key set, any host is accepted.
    GT_MCP_HOST/PORT    bind address (default 127.0.0.1:8765)
    GT_DATA_URL         poll a published data bundle and hot-swap it (see bundle.py)
"""
from __future__ import annotations

import hmac
import inspect
import json
import os
import sys
import time
from typing import Annotated, Any

from pydantic import Field

from groundtruth import __version__, facts, observability, search
from groundtruth.project import Project

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


# ===================================================================== dispatch


def call_tool(project: Project, name: str, args: dict[str, Any] | None = None, *, log: bool = True) -> Any:
    """Run any tool by name. The server, the evals, and ``./gt ask`` all use this."""
    args = dict(args or {})
    t0 = time.perf_counter()
    ok, result, error = True, None, None
    try:
        if name == "search_wiki":
            result = search.search_wiki(args.get("query", ""), int(args.get("limit") or 5), project=project)
        elif name == "read_wiki_page":
            result = search.read_wiki_page(args.get("path_or_slug", ""), int(args.get("max_chars") or 10000),
                                           project=project)
            if result is None:
                result = {"found": False, "hint": "No page by that name. Try search_wiki first."}
        else:
            result = facts.store(project).call(name, args)
        return result
    except Exception as exc:
        ok, error = False, f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if log:
            if isinstance(result, dict) and "count" in result:
                count = result["count"]
            elif isinstance(result, list):
                count = len(result)
            elif isinstance(result, dict) and result.get("found") is False:
                count = 0
            else:
                count = None if result is None else 1
            observability.log_tool_call(project, name, args, ok=ok, ms=(time.perf_counter() - t0) * 1000,
                                        result_count=count, error=error)


# ===================================================================== tool definitions


def tool_specs(project: Project) -> list[dict[str, Any]]:
    """Every tool the model sees: name, description, and JSON-schema parameters."""
    store = facts.store(project)
    store.refresh()
    out = []
    for name, spec in store.tools.items():
        desc = spec["description"]
        if "source" not in desc.lower():
            desc += " Each result has a `source` (a URL, or a file path for local documents): cite it."
        out.append({"name": name, "description": desc, "input_schema": store.tool_param_schema(name),
                    "collection": spec["collection"], "kind": spec["kind"]})
    out.append({
        "name": "search_wiki",
        "description": ("Search the wiki for how/why/process/policy explanations. Returns source-cited "
                        "snippets. Never use it for exact dates, dollar amounts, hours, or phone numbers: "
                        "those come from the fact tools."),
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What the person is asking about."},
            "limit": {"type": "integer", "description": "Max pages (default 5)."}},
            "required": ["query"]},
        "collection": None, "kind": "wiki",
    })
    out.append({
        "name": "read_wiki_page",
        "description": "Read one wiki page in full by slug, path, or title (use a path from search_wiki).",
        "input_schema": {"type": "object", "properties": {
            "path_or_slug": {"type": "string", "description": "e.g. 'fines-and-fees' or a path from search_wiki."},
            "max_chars": {"type": "integer", "description": "Truncate long pages (default 10000)."}},
            "required": ["path_or_slug"]},
        "collection": None, "kind": "wiki",
    })
    return out


PLACEHOLDER = "{{FACT_TOOLS}}"


def routing_table(project: Project) -> str:
    """Markdown table: which fact tool answers what. Generated from facts/, so it can't drift."""
    store = facts.store(project)
    store.refresh()
    lines = ["| For | Call |", "|---|---|"]
    for coll in store.collections.values():
        names = ", ".join(f"`{t['name']}`" for t in coll["tools"])
        lines.append(f"| {coll['description'] or coll['name']} | {names} |")
    return "\n".join(lines)


def _generated_instructions(project: Project) -> str:
    about = project.about or f"Grounded knowledge tools for {project.name}."
    return (
        f"{about}\n\n"
        "Rule: never state a date, time, dollar amount, phone number, email, hours, or other exact fact "
        "from memory or from wiki prose. Look it up with a fact tool and cite the result's `source`:\n\n"
        f"{routing_table(project)}\n\n"
        "For how/why/process questions, call search_wiki, then read_wiki_page on the best hit. "
        "If a wiki page and a fact tool disagree, the fact tool is right. If no tool covers the question, "
        "say so instead of guessing."
    )


def instructions(project: Project) -> str:
    """The assistant's instructions. One source of truth for MCP clients, ./gt ask, and evals.

    Uses prompts/system_prompt.md when it exists, with ``{{FACT_TOOLS}}`` replaced by
    the generated routing table. Without a prompt file, generates everything.
    """
    if project.prompt_path.exists():
        text = project.prompt_path.read_text(encoding="utf-8")
        if PLACEHOLDER in text:
            text = text.replace(PLACEHOLDER, routing_table(project))
        return text.strip()
    return _generated_instructions(project)


def _make_fact_tool(project: Project, spec: dict[str, Any]):
    params: list[inspect.Parameter] = []
    kw = inspect.Parameter.KEYWORD_ONLY
    if spec["kind"] == "lookup":
        params.append(inspect.Parameter(
            spec["arg"], kw, default=None,
            annotation=Annotated[str | None, Field(description="What to look up. Leave empty to list everything.")]))
    else:
        for f in spec["filters"]:
            params.append(inspect.Parameter(
                f["param"], kw, default=None,
                annotation=Annotated[str | None, Field(description=f"Only rows where {f['field']} matches.")]))
        if spec.get("upcoming"):
            params.append(inspect.Parameter(
                "upcoming_only", kw, default=True,
                annotation=Annotated[bool, Field(description="Skip dates in the past.")]))
    params.append(inspect.Parameter(
        "limit", kw, default=facts.DEFAULT_LIMIT,
        annotation=Annotated[int, Field(description="Max rows to return.")]))

    def tool(**kwargs):
        return call_tool(project, spec["name"], kwargs)

    tool.__signature__ = inspect.Signature(params, return_annotation=dict)
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    tool.__name__ = spec["name"]
    return tool


def build_server(project: Project):
    """Construct the FastMCP server for a project."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP(
        project.name[:64] or "groundtruth",
        instructions=instructions(project),
        stateless_http=os.environ.get("GT_MCP_STATEFUL", "0") != "1",
        json_response=True,
        transport_security=_transport_security(),
        log_level=os.environ.get("GT_LOG_LEVEL", "warning").upper(),
    )
    annotations = ToolAnnotations(**READ_ONLY)
    store = facts.store(project)
    for spec_out in tool_specs(project):
        name = spec_out["name"]
        if name in ("search_wiki", "read_wiki_page"):
            continue
        fn = _make_fact_tool(project, store.tools[name])
        server.add_tool(fn, name=name, description=spec_out["description"], annotations=annotations)

    def search_wiki(
        query: Annotated[str, Field(description="What the person is asking about.")],
        limit: Annotated[int, Field(description="Max pages.")] = 5,
    ) -> list[dict]:
        return call_tool(project, "search_wiki", {"query": query, "limit": limit})

    def read_wiki_page(
        path_or_slug: Annotated[str, Field(description="e.g. 'fines-and-fees' or a path from search_wiki.")],
        max_chars: Annotated[int, Field(description="Truncate long pages.")] = 10000,
    ) -> dict:
        return call_tool(project, "read_wiki_page", {"path_or_slug": path_or_slug, "max_chars": max_chars})

    wiki_specs = {s["name"]: s for s in tool_specs(project) if s["kind"] == "wiki"}
    server.add_tool(search_wiki, name="search_wiki", description=wiki_specs["search_wiki"]["description"],
                    annotations=annotations)
    server.add_tool(read_wiki_page, name="read_wiki_page", description=wiki_specs["read_wiki_page"]["description"],
                    annotations=annotations)
    return server


# ===================================================================== HTTP plumbing


def _transport_security():
    """Which Host headers to accept on HTTP.

    The MCP SDK rejects unknown hosts by default (DNS-rebinding protection), which
    is right for an open server on your laptop and confusing the first time you
    deploy. So: an explicit GT_ALLOWED_HOSTS wins; with an API key set the key
    does the protecting and any host is fine; otherwise only localhost.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    raw = os.environ.get("GT_ALLOWED_HOSTS", "").strip()
    if raw == "*" or (not raw and os.environ.get("GT_MCP_API_KEY", "").strip()):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for h in (x.strip() for x in raw.split(",") if x.strip()):
        hosts += [h, f"{h}:*"]
        origins += [f"https://{h}", f"http://{h}", f"https://{h}:*"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                     allowed_origins=origins)


_check_cache: dict[str, tuple[float, dict]] = {}


def check_status(project: Project) -> dict[str, Any]:
    """Does the content being served pass ./gt check? Cached for 30 seconds."""
    from groundtruth import checks
    key = str(project.data_root or project.root)
    hit = _check_cache.get(key)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    if project.data_root is not None:
        status = {"ok": True, "errors": 0, "note": "published bundle (checked before publishing)"}
    else:
        report = checks.run(project)
        errors = [f"{k}: {e}" for k, v in report.errors.items() for e in v]
        status = {"ok": not errors, "errors": len(errors), "first": errors[0] if errors else None}
    _check_cache[key] = (time.monotonic(), status)
    return status


def health(project: Project) -> dict[str, Any]:
    store = facts.store(project)
    store.refresh()
    db = project.db_path
    counts = json.loads(store.meta.get("counts", "{}"))
    pages = search.load_pages(project)
    return {
        "ok": True,
        "name": project.name,
        "groundtruth": __version__,
        "data_version": store.meta.get("fingerprint"),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(db.stat().st_mtime)) if db.exists() else None,
        "facts": counts,
        "wiki_pages": len(pages),
        "probe": pages[0].title if pages else None,  # a query ./gt watchdog knows must find something
        "tools": len(store.tools) + 2,
        "checks": check_status(project),
    }


class GatewayMiddleware:
    """Adds /healthz (no auth, for load balancers and ./gt watchdog) and API-key auth."""

    def __init__(self, app, project: Project, api_key: str) -> None:
        self.app = app
        self.project = project
        self.api_key = api_key

    async def _send_json(self, send, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in ("/healthz", "/health"):
            try:
                return await self._send_json(send, 200, health(self.project))
            except Exception as exc:  # noqa: BLE001 - health must report, not crash
                return await self._send_json(send, 503, {"ok": False, "error": str(exc)})
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if self.api_key:
            presented = ""
            auth = headers.get(b"authorization", b"")
            if auth.lower().startswith(b"bearer "):
                presented = auth[7:].decode(errors="ignore").strip()
            else:
                presented = (headers.get(b"x-api-key") or headers.get(b"x-ms-apikey") or b"").decode(
                    errors="ignore").strip()
            if not presented or not hmac.compare_digest(presented, self.api_key):
                return await self._send_json(send, 401, {"error": "unauthorized",
                                                         "hint": "send Authorization: Bearer <GT_MCP_API_KEY>"})
        if headers.get(b"x-groundtruth-probe"):
            token = observability.PROBE.set(True)
            try:
                return await self.app(scope, receive, send)
            finally:
                observability.PROBE.reset(token)
        return await self.app(scope, receive, send)


def http_app(project: Project):
    server = build_server(project)
    app = server.streamable_http_app()
    return GatewayMiddleware(app, project, os.environ.get("GT_MCP_API_KEY", "").strip())


def run(project: Project, *, stdio: bool = False, host: str | None = None, port: int | None = None,
        force: bool = False) -> int:
    facts.ensure_built(project)
    status = check_status(project)
    if not status["ok"]:
        msg = (f"groundtruth: this project fails ./gt check ({status['errors']} problem(s)); first: "
               f"{status['first']}")
        if not stdio and not force:
            print(msg + "\nRefusing to serve content that hasn't passed the checks. Fix it, or run "
                  "./gt serve --force to serve anyway (local testing only).", file=sys.stderr)
            return 1
        print(msg + " Serving anyway" + (" (--force)." if force else " because this is a local stdio session."),
              file=sys.stderr)
    if os.environ.get("GT_DATA_URL", "").strip():
        from groundtruth import bundle
        bundle.start_polling(project)
    if stdio:
        build_server(project).run(transport="stdio")
        return 0
    import uvicorn

    host = host or os.environ.get("GT_MCP_HOST", "127.0.0.1")
    port = int(port or os.environ.get("GT_MCP_PORT", "8765"))
    key = os.environ.get("GT_MCP_API_KEY", "").strip()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"groundtruth: serving {project.name!r} at http://{shown}:{port}/mcp "
          f"(auth {'ON' if key else 'OFF - set GT_MCP_API_KEY before exposing this'})", file=sys.stderr)
    print(f"groundtruth: health check at http://{shown}:{port}/healthz", file=sys.stderr)
    uvicorn.run(http_app(project), host=host, port=port, log_level=os.environ.get("GT_LOG_LEVEL", "warning"))
    return 0
