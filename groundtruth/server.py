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
import threading
import time
from typing import Annotated, Any

from pydantic import Field

from groundtruth import __version__, facts, observability, search
from groundtruth.project import Project

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


# ===================================================================== dispatch


class Gate:
    """What the server actually serves: the last version of the project that passed ./gt check.

    Every second or so it looks for changes on disk (facts, wiki, sources, prompt,
    evals, the review lock). A changed project is checked first; only a passing one
    replaces what's being served. A failing edit is rejected and reported through
    /healthz and ./gt watchdog, while the last good version keeps serving.
    """

    def __init__(self, project: Project, *, production: bool = False, allow_unvalidated: bool = False):
        self.project = project
        self.production = production
        self.allow_unvalidated = allow_unvalidated
        self.pages: list | None = None
        self.validated = False
        self.serving_sig = None
        self.rejected_sig = None
        self.rejected: str | None = None
        self.errors = 0
        self.listeners: list = []  # called after each validated reload (the server syncs prompt + tools)
        self._checked = 0.0
        self._lock = threading.Lock()
        facts.store(project).auto_reload = False

    def _signature(self):
        p = self.project
        parts: list = [str(p.data_root)]
        for d in (p.facts_dir, p.wiki_dir, p.pages_dir):
            if d.exists():
                parts += [(str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in sorted(d.rglob("*")) if f.is_file()]
        for f in (p.prompt_path, p.golden_path, p.root / "sources.lock.yaml", p.root / "groundtruth.yaml"):
            if f.exists():
                parts.append((str(f), f.stat().st_mtime_ns, f.stat().st_size))
        return hash(tuple(parts))

    def _load(self) -> None:
        facts.ensure_built(self.project)
        facts.store(self.project).refresh(force=True)
        self.pages = search._parse_pages(self.project)
        for listener in self.listeners:
            try:
                listener()
            except Exception as exc:  # noqa: BLE001 - a failed sync must not take serving down
                print(f"groundtruth: couldn't apply the update to the MCP server: {exc}", file=sys.stderr)

    def refresh(self, force: bool = False) -> None:
        from groundtruth import checks
        now = time.monotonic()
        if not force and now - self._checked < 1.0:
            return
        with self._lock:
            self._checked = now
            sig = self._signature()
            if not force and sig == self.serving_sig:
                if self.validated:  # back to the version being served (say, the bad file was deleted)
                    self.rejected, self.rejected_sig, self.errors = None, None, 0
                return
            if not force and sig == self.rejected_sig:
                return
            if self.project.data_root is not None:  # a published bundle was checked before publishing
                self._load()
                self.validated, self.serving_sig, self.rejected, self.rejected_sig = True, sig, None, None
                return
            from groundtruth import project as project_mod
            try:  # check the config on disk too, not just the one loaded at startup
                candidate = project_mod.load(self.project.root)
            except project_mod.ProjectError as exc:
                if self.pages is None:
                    raise
                self.rejected, self.rejected_sig, self.errors = f"config: {exc}", sig, 1
                print(f"groundtruth: rejected an update, still serving the last good version: {exc}", file=sys.stderr)
                return
            report = checks.run(candidate, production=self.production)
            errors = [f"{k}: {e}" for k, v in report.errors.items() for e in v]
            if report.ok or (self.pages is None and self.allow_unvalidated):
                self.project.config = candidate.config
                self._load()
                self.validated, self.serving_sig = report.ok, sig
                self.rejected, self.rejected_sig, self.errors = (None, None, 0) if report.ok else \
                    (errors[0], None, len(errors))
                return
            if self.pages is None:
                raise RuntimeError(errors[0] if errors else "the project fails ./gt check")
            self.rejected, self.rejected_sig, self.errors = errors[0], sig, len(errors)
            print(f"groundtruth: rejected an update that fails ./gt check, still serving the last good version: "
                  f"{errors[0]}", file=sys.stderr)

    def status(self) -> dict[str, Any]:
        self.refresh()
        return {"ok": self.validated, "update_rejected": self.rejected is not None and self.validated,
                "errors": self.errors, "first": self.rejected}


def call_tool(project: Project, name: str, args: dict[str, Any] | None = None, *, log: bool = True,
              gate: Gate | None = None) -> Any:
    """Run any tool by name. The server, the evals, and ``./gt ask`` all use this.

    With a ``gate`` (the server), answers come from the last validated version.
    """
    args = dict(args or {})
    t0 = time.perf_counter()
    ok, result, error = True, None, None
    pages = None
    if gate is not None:
        gate.refresh()
        pages = gate.pages
    try:
        if name == "search_wiki":
            result = search.search_wiki(args.get("query", ""), int(args.get("limit") or 5), project=project,
                                        pages=pages)
        elif name == "read_wiki_page":
            result = search.read_wiki_page(args.get("path_or_slug", ""), int(args.get("max_chars") or 10000),
                                           project=project, pages=pages)
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


def _make_fact_tool(project: Project, spec: dict[str, Any], gate: Gate | None = None):
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
        return call_tool(project, spec["name"], kwargs, gate=gate)

    tool.__signature__ = inspect.Signature(params, return_annotation=dict)
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    tool.__name__ = spec["name"]
    return tool


def build_server(project: Project, gate: Gate | None = None):
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
    registered: dict[str, str] = {}  # fact tool name -> its spec, to spot changes

    def sync() -> None:
        """Make the server match the project: instructions and fact tools. Runs on every validated reload,
        so an edited prompt reaches new sessions and a new facts file's tools appear without a restart."""
        server._mcp_server.instructions = instructions(project)
        specs = {s["name"]: s for s in tool_specs(project) if s["kind"] != "wiki"}
        for name in list(registered):
            if name not in specs or registered[name] != json.dumps(specs[name], sort_keys=True, default=str):
                if hasattr(server, "remove_tool"):
                    server.remove_tool(name)
                else:  # older mcp 1.x
                    server._tool_manager._tools.pop(name, None)
                del registered[name]
        for name, spec_out in specs.items():
            if name not in registered:
                fn = _make_fact_tool(project, store.tools[name], gate)
                server.add_tool(fn, name=name, description=spec_out["description"], annotations=annotations)
                registered[name] = json.dumps(spec_out, sort_keys=True, default=str)

    sync()
    if gate is not None:
        gate.listeners.append(sync)

    def search_wiki(
        query: Annotated[str, Field(description="What the person is asking about.")],
        limit: Annotated[int, Field(description="Max pages.")] = 5,
    ) -> list[dict]:
        return call_tool(project, "search_wiki", {"query": query, "limit": limit}, gate=gate)

    def read_wiki_page(
        path_or_slug: Annotated[str, Field(description="e.g. 'fines-and-fees' or a path from search_wiki.")],
        max_chars: Annotated[int, Field(description="Truncate long pages.")] = 10000,
    ) -> dict:
        return call_tool(project, "read_wiki_page", {"path_or_slug": path_or_slug, "max_chars": max_chars},
                         gate=gate)

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


def health(project: Project, gate: Gate | None = None) -> dict[str, Any]:
    """What /healthz returns. ``ok`` means: serving a version that passed ./gt check."""
    if gate is not None:
        status = gate.status()
        pages = gate.pages or []
    else:  # outside a server: just check what's on disk
        from groundtruth import checks
        report = checks.run(project)
        errors = [f"{k}: {e}" for k, v in report.errors.items() for e in v]
        status = {"ok": report.ok, "update_rejected": False, "errors": len(errors),
                  "first": errors[0] if errors else None}
        pages = search.load_pages(project)
    store = facts.store(project)
    store.refresh()
    db = project.db_path
    return {
        "ok": status["ok"],
        "name": project.name,
        "groundtruth": __version__,
        "data_version": store.meta.get("fingerprint"),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(db.stat().st_mtime)) if db.exists() else None,
        "facts": json.loads(store.meta.get("counts", "{}")),
        "wiki_pages": len(pages),
        "probe": pages[0].title if pages else None,  # a query ./gt watchdog knows must find something
        "tools": len(store.tools) + 2,
        "checks": status,
    }


class GatewayMiddleware:
    """Adds /healthz (no auth, for load balancers and ./gt watchdog) and API-key auth."""

    def __init__(self, app, project: Project, api_key: str, gate: Gate | None = None) -> None:
        self.app = app
        self.project = project
        self.api_key = api_key
        self.gate = gate

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
                body = health(self.project, self.gate)
                return await self._send_json(send, 200 if body["ok"] else 503, body)
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


def http_app(project: Project, gate: Gate | None = None):
    if gate is None:
        gate = Gate(project, production=True, allow_unvalidated=True)
        gate.refresh(force=True)
    server = build_server(project, gate)
    app = server.streamable_http_app()
    return GatewayMiddleware(app, project, os.environ.get("GT_MCP_API_KEY", "").strip(), gate)


def run(project: Project, *, stdio: bool = False, host: str | None = None, port: int | None = None,
        force: bool = False) -> int:
    from groundtruth import checks
    facts.ensure_built(project)
    production = not stdio  # local stdio sessions may still use template examples while you explore
    report = checks.run(project, production=production)
    if not report.ok:
        errors = [f"{k}: {e}" for k, v in report.errors.items() for e in v]
        cmd = "./gt check" + (" --production" if production else "")
        msg = f"groundtruth: this project fails {cmd} ({len(errors)} problem(s)); first: {errors[0]}"
        if not force:
            print(msg + f"\nRefusing to serve content that hasn't passed the checks. Run {cmd} and fix "
                  f"what it reports (or add --force to serve anyway, for local testing only).", file=sys.stderr)
            return 1
        print(msg + " Serving anyway (--force).", file=sys.stderr)
    gate = Gate(project, production=production, allow_unvalidated=True)
    gate.refresh(force=True)
    if os.environ.get("GT_DATA_URL", "").strip():
        from groundtruth import bundle
        bundle.start_polling(project)
    if stdio:
        build_server(project, gate).run(transport="stdio")
        return 0
    import uvicorn

    host = host or os.environ.get("GT_MCP_HOST", "127.0.0.1")
    port = int(port or os.environ.get("GT_MCP_PORT", "8765"))
    key = os.environ.get("GT_MCP_API_KEY", "").strip()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"groundtruth: serving {project.name!r} at http://{shown}:{port}/mcp "
          f"(auth {'ON' if key else 'OFF - set GT_MCP_API_KEY before exposing this'})", file=sys.stderr)
    print(f"groundtruth: health check at http://{shown}:{port}/healthz", file=sys.stderr)
    uvicorn.run(http_app(project, gate), host=host, port=port, log_level=os.environ.get("GT_LOG_LEVEL", "warning"))
    return 0
