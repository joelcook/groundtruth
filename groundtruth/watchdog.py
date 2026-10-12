"""Outside-in checks: is the server up, answering, and serving fresh data?

    ./gt watchdog                                   # start the server over stdio and test it
    ./gt watchdog --url https://mcp.example.org/mcp # test a deployed server
    ./gt watchdog --url ... --max-age-hours 48      # also fail if the data is stale
    ./gt watchdog --repo [--url ...]                # also check the repo: alerts, refresh, CI, deploy

It talks MCP exactly like a chat client would: initialize, list tools, call a
fact tool and the wiki search. ``--repo`` adds what only the repo knows, so it
still fires when the refresh host is off:

  * ALERTS.md lists no open alert (the refresh host publishes them there);
  * a ``refresh:`` commit landed on main recently (when groundtruth.yaml names a host);
  * CI passed on main's head commit (in GitHub Actions, or with GITHUB_TOKEN and
    GITHUB_REPOSITORY set);
  * the deployed server's code is no more than --code-max-lag-hours behind main
    (when it reports its build commit: GT_BUILD_COMMIT, see docs/deploy.md).

Exit code 0 means healthy. Schedule it (see .github/workflows/watchdog.yml) and
a failed run is your alert.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

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

    def note(self, msg: str) -> None:
        print(f"  · {msg}")


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


def _health(url: str, max_age_hours: float | None, checks: Checks, api_key: str | None = None) -> dict:
    import httpx

    base = url.rstrip("/").removesuffix("/mcp")
    try:
        r = httpx.get(base + "/healthz", timeout=15,
                      headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        checks.fail(f"healthz unreachable: {exc}")
        return {}
    if r.status_code not in (200, 503) or "name" not in body:
        checks.fail(f"healthz: {r.status_code} {body}")
        return {}
    code = f", code {str(body['commit'])[:12]}" if body.get("commit") else ""
    checks.ok(f"healthz: {body.get('name')} data {body.get('data_version')}, built {body.get('built_at')}{code}")
    status = body.get("checks") or {}
    if status and not status.get("ok", True):
        checks.fail(f"the server is serving content that fails ./gt check ({status.get('errors')} problem(s)): "
                    f"{status.get('first')}")
    elif status.get("update_rejected"):
        checks.fail(f"an update was rejected (still serving the last good version): {status.get('first')}")
    update = body.get("data_update") or {}
    if update.get("last_error"):
        checks.fail(f"the server can't install data bundles, so it's serving old data: {update['last_error']}")
    if max_age_hours and body.get("built_at"):
        built = dt.datetime.strptime(body["built_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - built).total_seconds() / 3600
        if age > max_age_hours:
            checks.fail(f"data is {age:.0f}h old (limit {max_age_hours:.0f}h)")
        else:
            checks.ok(f"data age {age:.1f}h (limit {max_age_hours:.0f}h)")
    return body


def chat_question(project: Project | None) -> tuple[str, list[str]]:
    """What to ask the public chat: a golden question with a known answer, or just something to answer."""
    if project is not None:
        from groundtruth import checks as gates
        for item in gates.load_golden(project):
            if isinstance(item, dict) and item.get("category") == "hard_fact" and item.get("must_contain"):
                return str(item["question"]), [str(v) for v in item["must_contain"]]
    return "What can you help me with?", []


def chat_check(base: str, project: Project | None, checks: Checks) -> None:
    """The public /chat answers a real question through the model, with the right value: a server whose
    model credential expired otherwise looks healthy while every visitor gets an error."""
    import httpx
    from groundtruth import evals
    question, expected = chat_question(project)
    why = ""
    for _ in range(2):  # one retry: a model can word a right answer oddly once
        try:
            r = httpx.post(base + "/chat/ask", json={"question": question}, timeout=120)
            reply = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as exc:  # noqa: BLE001
            why = f"unreachable: {exc}"
            continue
        answer = str(reply.get("answer") or "")
        if r.status_code != 200 or not answer.strip():
            why = f"{r.status_code} {reply.get('error') or answer[:120]}"
        elif reply.get("abstained"):
            why = f"it couldn't confirm the answer to {question!r}"
        elif missing := [v for v in expected if not evals.contains(answer, v)]:
            why = f"its answer to {question!r} doesn't state {missing}"
        else:
            checks.ok(f"chat: answered {question!r}" + (" with the right value" if expected else ""))
            return
    checks.fail(f"the public chat is failing: {why}")


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
    if status == 404 or "Session terminated" in str(leaf):
        return f"{status or ''} no MCP endpoint at that URL: the MCP path is usually /mcp".strip()
    if status:
        return f"HTTP {status} from the server"
    return f"{type(leaf).__name__}: {leaf}"


def deployed_commit(url: str) -> str | None:
    """The build commit a deployed server reports in /healthz (None if unreachable or unstamped)."""
    import httpx
    base = url.rstrip("/").removesuffix("/mcp")
    try:
        commit = httpx.get(base + "/healthz", timeout=15).json().get("commit")
    except Exception:  # noqa: BLE001 - the caller treats unknown as "can't tell"
        return None
    return str(commit) if commit else None


# ===================================================================== repo checks


def alerts_result(entries: list[tuple[str, str]]) -> tuple[bool, str]:
    if not entries:
        return True, "ALERTS.md lists no open alerts"
    shown = "; ".join(f"{stamp} {msg[:140]}" for stamp, msg in entries[:3])
    more = f" (+{len(entries) - 3} more)" if len(entries) > 3 else ""
    return False, f"{len(entries)} open alert(s) in ALERTS.md: {shown}{more}. Fix each, then delete its line"


def refresh_result(now: float, commit_ts: int | None, label: str, max_hours: float) -> tuple[bool, str]:
    if commit_ts is None:
        return False, ("no `refresh:` commit on main yet: is the refresh host running? (GitHub Actions: the "
                       "GT_AUTOPILOT variable and the autopilot workflow's runs; a machine: ./gt host check)")
    age = (now - commit_ts) / 3600
    return age <= max_hours, (f"last refresh {label} landed {age:.0f}h ago (limit {max_hours:.0f}h)"
                              + ("" if age <= max_hours else ": is the refresh host running? (GitHub Actions: "
                                 "the autopilot workflow's runs; a machine: ./gt host check)"))


def ci_result(runs: list[dict], sha: str) -> tuple[bool | None, str]:
    """Grade the CI workflow's runs for main's head commit. None: still running."""
    if not runs:
        return False, f"no CI run for main's head {sha[:7]}"
    run = runs[0]
    if run.get("status") != "completed":
        return None, f"CI is {run.get('status')} on {sha[:7]}"
    ok = run.get("conclusion") == "success"
    return ok, f"CI {run.get('conclusion')} on {sha[:7]} {run.get('html_url', '')}".strip()


def code_lag_result(behind: int | None, oldest_hours: float | None, deployed: str,
                    max_hours: float) -> tuple[bool, str]:
    if behind is None:
        return False, f"the server runs {deployed[:7]}, which isn't in main's history: redeploy from main"
    if behind == 0:
        return True, f"the deployed code ({deployed[:7]}) matches main"
    lag = oldest_hours or 0.0
    return lag <= max_hours, (f"the server runs {deployed[:7]}, {behind} code commit(s) behind main; the oldest "
                              f"is {lag:.0f}h old (limit {max_hours:.0f}h). Redeploy (docs/deploy.md)")


def _ci_runs(repo: str, token: str, sha: str, workflow: str) -> list[dict]:
    import httpx
    r = httpx.get(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs",
                  params={"head_sha": sha, "per_page": 5}, timeout=30,
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json().get("workflow_runs", [])


def repo_checks(project: Project, checks: Checks, *, deployed: str | None, url: str | None,
                refresh_max_hours: float, code_max_lag_hours: float) -> None:
    from groundtruth import alerts, host
    ok, msg = alerts_result(alerts.open_alerts(project))
    (checks.ok if ok else checks.fail)(msg)
    try:
        root = host.git_root(project)
    except host.HostError as exc:
        checks.fail(f"{exc}: the repo checks need the repo")
        return
    ref = "origin/main" if host.git(root, "rev-parse", "--verify", "--quiet", "origin/main",
                                    check=False).returncode == 0 else "HEAD"
    head = host.git(root, "rev-parse", ref).stdout.strip()
    now = time.time()

    if host.configured_name(project.config):
        out = host.git(root, "log", ref, "--grep=^refresh:", "-1", "--format=%ct %h %s").stdout.strip()
        ts, _, label = out.partition(" ")
        ok, msg = refresh_result(now, int(ts) if ts else None, label, refresh_max_hours)
        (checks.ok if ok else checks.fail)(msg)
    else:
        checks.note("refresh: no refresh host named in groundtruth.yaml, so no nightly refresh to check")

    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    if repo and token:
        try:
            ok, msg = ci_result(_ci_runs(repo, token, head, os.environ.get("GT_CI_WORKFLOW", "ci.yml")), head)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"couldn't read CI runs: {exc}"
        (checks.note if ok is None else checks.ok if ok else checks.fail)(msg)
    else:
        checks.note("CI on main: not checked here (GitHub Actions sets GITHUB_TOKEN and GITHUB_REPOSITORY)")

    if url and deployed:
        behind = host.code_commits_since(root, deployed, head)
        stamps = host.git(root, "log", "--reverse", "--format=%ct", f"{deployed}..{head}", "--",
                          *host.CODE_PATHS, check=False).stdout.split()
        oldest = (now - int(stamps[0])) / 3600 if stamps else None
        ok, msg = code_lag_result(behind, oldest, deployed, code_max_lag_hours)
        (checks.ok if ok else checks.fail)(msg)
    elif url:
        checks.note("deployed code: the server doesn't report its build commit. Build the image with "
                    "--build-arg GT_BUILD_COMMIT=$(git rev-parse HEAD) to have this checked")


def run(project: Project | None, url: str | None = None, api_key: str | None = None,
        max_age_hours: float | None = None, *, repo: bool = False, refresh_max_hours: float = 30,
        code_max_lag_hours: float = 72) -> int:
    checks = Checks()
    if url and not url.rstrip("/").endswith("/mcp"):
        from urllib.parse import urlparse
        if urlparse(url).path in ("", "/"):
            url = url.rstrip("/") + "/mcp"
            print(f"(no path given; using {url})")
    try:
        if url:
            print(f"watchdog: {url}")
            key = api_key or os.environ.get("GT_MCP_API_KEY") or None
            body = _health(url, max_age_hours, checks, key)
            if repo and project is not None:
                repo_checks(project, checks, deployed=body.get("commit"), url=url,
                            refresh_max_hours=refresh_max_hours, code_max_lag_hours=code_max_lag_hours)
            anyio.run(_http, url, api_key or os.environ.get("GT_MCP_API_KEY") or None, checks, body.get("probe"))
            if body.get("chat"):
                chat_check(url.rstrip("/").removesuffix("/mcp"), project, checks)
        elif repo and project is not None:
            print(f"watchdog: {project.name} (repo checks)")
            repo_checks(project, checks, deployed=None, url=None,
                        refresh_max_hours=refresh_max_hours, code_max_lag_hours=code_max_lag_hours)
        else:
            print(f"watchdog: {project.name} over stdio")
            from groundtruth import checks as gates
            report = gates.run(project)
            if not report.ok:  # the server refuses to start on it, so there's no session to test
                first = next(f"{k}: {e[0]}" for k, e in report.errors.items() if e)
                checks.fail(f"the project fails ./gt check, so the server won't start: {first}")
            else:
                anyio.run(_stdio, project, checks)
    except Exception as exc:  # noqa: BLE001 - surface any transport failure as a failed check
        checks.fail(f"MCP session failed: {_explain(exc)}")
    print("healthy" if not checks.failed else f"UNHEALTHY: {checks.failed} check(s) failed")
    return 0 if not checks.failed else 1
