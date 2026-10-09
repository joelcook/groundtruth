"""Outside-in checks: is the server up, answering, and serving fresh data?

    ./gt watchdog                                   # start the server over stdio and test it
    ./gt watchdog --url https://mcp.example.org/mcp # test a deployed server
    ./gt watchdog --url ... --max-age-hours 48      # also fail if the data is stale

It talks MCP exactly like a chat client would: initialize, list tools, call a
fact tool and the wiki search. Exit code 0 means healthy. Schedule it (see
.github/workflows/watchdog.yml) and a failed run is your alert.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import anyio

from groundtruth.project import Project


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, msg: str) -> None:
        print(f"  ✓ {msg}")

    def fail(self, msg: str) -> None:
        self.failed += 1
        print(f"  ✗ {msg}")


def _payload(result) -> object:
    if getattr(result, "structuredContent", None) is not None:
        data = result.structuredContent
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    import json
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


async def _exercise(session, checks: Checks, probe: str | None = None) -> None:
    init = await session.initialize()
    checks.ok(f"initialize: {init.serverInfo.name}")
    listed = await session.list_tools()
    names = [t.name for t in listed.tools]
    if {"search_wiki", "read_wiki_page"} <= set(names):
        checks.ok(f"tools/list: {len(names)} tools")
    else:
        checks.fail(f"tools/list: wiki tools missing (got {names})")
    fact_tools = [n for n in names if n not in ("search_wiki", "read_wiki_page")]
    if fact_tools:
        res = await session.call_tool(fact_tools[0], {})
        data = _payload(res)
        count = data.get("count") if isinstance(data, dict) else None
        if res.isError or not count:
            checks.fail(f"{fact_tools[0]}: returned no rows ({str(data)[:160]})")
        else:
            checks.ok(f"{fact_tools[0]}: {count} row(s)")
    else:
        checks.fail("no fact tools advertised (is facts/ empty?)")
    if not probe:
        checks.fail("the wiki has no public pages, so how/why questions can't be answered")
        return
    res = await session.call_tool("search_wiki", {"query": probe, "limit": 1})
    hits = _payload(res)
    if res.isError:
        checks.fail(f"search_wiki errored: {str(hits)[:160]}")
    elif not hits:
        checks.fail(f"search_wiki found nothing for a page title ({probe!r}): retrieval is broken")
    else:
        checks.ok(f"search_wiki: found {probe!r}")


async def _stdio(project: Project, checks: Checks) -> None:
    from groundtruth import search
    pages = search.load_pages(project)
    probe = pages[0].title if pages else None
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "groundtruth", "-p", str(project.root), "serve", "--stdio"],
        env={**os.environ, "GT_LOG": "0"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await _exercise(session, checks, probe)


async def _http(url: str, api_key: str | None, checks: Checks, probe: str | None) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"X-Groundtruth-Probe": "1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await _exercise(session, checks, probe)


def _health(url: str, max_age_hours: float | None, checks: Checks) -> dict:
    import httpx

    base = url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    try:
        r = httpx.get(base + "/healthz", timeout=15)
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        checks.fail(f"healthz unreachable: {exc}")
        return {}
    if r.status_code != 200 or not body.get("ok"):
        checks.fail(f"healthz: {r.status_code} {body}")
        return {}
    checks.ok(f"healthz: {body.get('name')} data {body.get('data_version')}, built {body.get('built_at')}")
    if max_age_hours and body.get("built_at"):
        built = dt.datetime.strptime(body["built_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - built).total_seconds() / 3600
        if age > max_age_hours:
            checks.fail(f"data is {age:.0f}h old (limit {max_age_hours:.0f}h)")
        else:
            checks.ok(f"data age {age:.1f}h (limit {max_age_hours:.0f}h)")
    return body


def _leaf(exc: BaseException) -> BaseException:
    while getattr(exc, "exceptions", None):  # ExceptionGroup (3.11+) or the 3.10 backport
        exc = exc.exceptions[0]
    return exc


def _explain(exc: BaseException) -> str:
    leaf = _leaf(exc)
    response = getattr(leaf, "response", None)
    status = getattr(response, "status_code", None)
    if status == 401:
        return "401 unauthorized: pass the server's key with --api-key or GT_MCP_API_KEY"
    if status == 421 or "Invalid Host" in str(leaf):
        return f"{status or ''} host rejected: set GT_ALLOWED_HOSTS on the server (or an API key)".strip()
    if status:
        return f"HTTP {status} from the server"
    return f"{type(leaf).__name__}: {leaf}"


def run(project: Project, url: str | None = None, api_key: str | None = None,
        max_age_hours: float | None = None) -> int:
    checks = Checks()
    try:
        if url:
            print(f"watchdog: {url}")
            body = _health(url, max_age_hours, checks)
            anyio.run(_http, url, api_key or os.environ.get("GT_MCP_API_KEY") or None, checks, body.get("probe"))
        else:
            print(f"watchdog: {project.name} over stdio")
            anyio.run(_stdio, project, checks)
    except Exception as exc:  # noqa: BLE001 - surface any transport failure as a failed check
        checks.fail(f"MCP session failed: {_explain(exc)}")
    print("healthy" if not checks.failed else f"UNHEALTHY: {checks.failed} check(s) failed")
    return 0 if not checks.failed else 1
