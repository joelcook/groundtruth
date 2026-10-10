"""The ``./gt`` command. Run ``./gt`` with no arguments for the menu."""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

from groundtruth import __version__
from groundtruth.project import DEMO_PROJECT, REPO_ROOT, Project, ProjectError, pointer_target, resolve

MENU = """\
groundtruth {version}: give your LLM a single source of truth.

Start here
  demo                 build and test the bundled library example (30 seconds, no API key)
  ask "question"       see what the assistant would answer, and from which tools
  init NAME            start your own assistant (--template saas|college|city|clinic|blank)

Build your knowledge
  ingest URL|FOLDER    pull a website, sitemap, or folder into sources/
  compile              work order for Claude Code/Codex to write wiki pages + facts
                       (--write: let Claude draft the pages itself)
  build                rebuild the facts database (automatic, but handy)
  review               sources that changed since facts/wiki were last checked against them
  check                facts, wiki, no-facts-in-prose, eval wiring
  eval                 free offline eval (--tier live: ask Claude, needs a key)

Run it
  serve                MCP server on http://127.0.0.1:8765/mcp (--stdio for local clients)
  connect [CLIENT]     setup for Claude Desktop/Code, Codex, Cursor, VS Code, Copilot Studio...
  tools                the tools your assistant gets

Keep it honest
  report               what people ask, what's missing, error rates
  watchdog             outside-in health check (--url for a deployed server)
  refresh              the weekly agent: ingest, build, compile, check, eval, PR
  bundle               package data so deployed servers update without a redeploy

Housekeeping
  status · use PATH · examples [--remove] · prompt · doctor · setup

Project: {project}
Add -p PATH before the command to use a different project. Docs: README.md, docs/
"""


def _p(args) -> Project:
    return resolve(getattr(args, "project", None))


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _bad(msg: str) -> None:
    print(f"  ✗ {msg}")


def _where(project: Project) -> str:
    try:
        return str(project.root.relative_to(REPO_ROOT))
    except ValueError:
        return str(project.root)


# ===================================================================== commands


def cmd_build(args) -> int:
    from groundtruth import facts, search
    project = _p(args)
    try:
        counts = facts.build(project)
    except facts.FactsError as exc:
        print("facts have problems:")
        for p in exc.problems:
            _bad(p)
        return 1
    _ok(f"{sum(counts.values())} rows in {len(counts)} collection(s) -> {project.rel(project.db_path)}")
    if args.index:
        n = search.build_search_index(project)
        _ok(f"semantic index: {n} page(s) -> {project.rel(project.index_path)}")
    from groundtruth import checks
    report = checks.run(project)
    if not report.ok:
        n = sum(len(v) for v in report.errors.values())
        _bad(f"built, but ./gt check finds {n} problem(s), and ./gt serve won't serve it until they're fixed")
        return 1
    return 0


def cmd_examples(args) -> int:
    from groundtruth import examples
    project = _p(args)
    found = examples.find(project)
    if not found.total:
        _ok("no template examples left: everything here is yours")
        return 0
    if not args.remove:
        print(f"{found.total} template example item(s) still live (fictional data):")
        for p in [*found.facts, *found.wiki, *found.sources]:
            print(f"  {project.rel(p)}")
        for q in found.questions:
            print(f"  evals/golden.yaml: {q}")
        print("\nRemove them all with: ./gt examples --remove")
        return 0
    removed = examples.remove(project)
    _ok(f"removed {len(removed.facts)} facts file(s), {len(removed.wiki)} page(s), "
        f"{len(removed.sources)} source(s), {len(removed.questions)} question(s)")
    print("next: ./gt compile   (a work order for adding your own facts, pages, and questions)")
    print("      if prompts/system_prompt.md names tools you just removed, ./gt check will point them out")
    return 0


def cmd_review(args) -> int:
    from groundtruth import review
    project = _p(args)
    if args.done:
        recorded = review.mark_reviewed(project, args.paths or None)
        _ok(f"recorded {len(recorded)} source version(s) in {project.rel(review.lock_path(project))}")
        return 0
    if review.load_lock(project) is None:
        print("No sources.lock.yaml yet. When your facts and wiki match your sources, run ./gt review --done")
        return 0
    pending = review.changes(project)
    if not pending:
        _ok("every cited source matches its last review")
        return 0
    print(f"{len(pending)} source(s) changed since the last review:\n")
    for ch in pending:
        print(f"  {ch.source} ({ch.reason})")
        for f in ch.facts:
            print(f"      facts: {f}")
        for w in ch.wiki:
            print(f"      wiki:  {w}")
    if args.diff:
        for ch in pending:
            d = review.diff(project, ch.source)
            print(f"\n── {ch.source} " + "─" * max(0, 56 - len(ch.source)))
            print(d if d else "(no saved copy of the reviewed version: compare by hand)")
    else:
        print("\nSee exactly what changed: ./gt review --diff")
    print("Re-check each listed file against its source, update what's out of date, then run ./gt review --done.")
    return 1


def cmd_check(args) -> int:
    from groundtruth import checks
    project = _p(args)
    report = checks.run(project, production=args.production)
    print(f"check{' --production' if args.production else ''}: {project.name} ({_where(project)})")
    for section in ("facts", "sources", "wiki", "prose", "evals", "prompt", "examples"):
        errs = report.errors.get(section, [])
        warns = report.warnings.get(section, [])
        note = report.notes.get(section, "")
        if errs:
            _bad(f"{section}: {len(errs)} problem(s)")
            for e in errs:
                print(f"      {e}")
        elif section in report.notes:
            _ok(f"{section}: {note}")
        for w in warns:
            print(f"    ! {w}")
    print("all good" if report.ok else "fix the problems above, then run ./gt check again")
    return 0 if report.ok else 1


def cmd_eval(args) -> int:
    from groundtruth import evals
    return evals.run(_p(args), args.tier, only=args.only, judge=not args.no_judge)


def cmd_serve(args) -> int:
    from groundtruth import server
    return server.run(_p(args), stdio=args.stdio, host=args.host, port=args.port, force=args.force)


def cmd_tools(args) -> int:
    from groundtruth import server
    specs = server.tool_specs(_p(args))
    if args.json:
        print(json.dumps([{k: s[k] for k in ("name", "description", "input_schema")} for s in specs], indent=2))
        return 0
    for s in specs:
        params = ", ".join(f"{k}" for k in s["input_schema"].get("properties", {}) if k != "limit")
        print(f"{s['name']}({params})")
        print(textwrap.indent(textwrap.fill(s["description"], 88), "    "))
    return 0


def cmd_prompt(args) -> int:
    from groundtruth import server
    print(server.instructions(_p(args)))
    return 0


def cmd_ask(args) -> int:
    from groundtruth import evals, llm, server
    project = _p(args)
    question = " ".join(args.question).strip()
    if not question:
        print('usage: ./gt ask "your question"')
        return 2
    if llm.available() and not args.offline:
        print(f"asking {llm.model()} with {project.name}'s tools...\n")
        try:
            out = llm.agent(question, evals.system_prompt(project), server.tool_specs(project),
                            lambda name, a: server.call_tool(project, name, a))
        except llm.LLMUnavailable as exc:
            print(f"  ✗ {exc}")
            return 1
        print(out["response"])
        print("\ntools used:")
        for c in out["tool_calls"] or [{"name": "(none)", "input": {}}]:
            print(f"  - {c['name']}({', '.join(f'{k}={v!r}' for k, v in c['input'].items())})")
        return 0
    from groundtruth import facts
    store = facts.store(project)
    if args.json:
        out = {"question": question, "facts": [], "wiki": []}
        for guess in store.suggest(question):
            out["facts"].append({"tool": guess["tool"], "args": guess["args"], "score": guess["score"],
                                 "result": server.call_tool(project, guess["tool"], guess["args"], log=False)})
        out["wiki"] = server.call_tool(project, "search_wiki", {"query": question, "limit": 3}, log=False)
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"No API key, so this is a heuristic preview of what the model might look up for {question!r}.")
    print("(A real model chooses its own tools; set ANTHROPIC_API_KEY to see that.)\n")
    found = False
    for guess in store.suggest(question):
        res = server.call_tool(project, guess["tool"], {**guess["args"], "limit": 3}, log=False)
        if not res.get("count"):
            continue
        found = True
        key = store.collections[res["collection"]]["key"]
        args_txt = ", ".join(f"{k}={v!r}" for k, v in guess["args"].items())
        conf = "high" if guess["score"] >= 60 else "medium" if guess["score"] >= 40 else "low"
        print(f"{guess['tool']}({args_txt}) -> {res['count']} row(s)   [match confidence: {conf}]")
        for row in res["results"][:3]:
            fields = "; ".join(f"{k}={str(v)[:80]}" for k, v in row.items()
                               if k not in (key, "source", "source_file") and v not in (None, ""))
            print(f"  • {row.get(key)}: {fields}")
            print(f"    source: {row.get('source')}")
    hits = server.call_tool(project, "search_wiki", {"query": question, "limit": 3}, log=False)
    if hits:
        found = True
        print("search_wiki ->")
        for h in hits:
            print(f"    {h['path']}: {h['snippet'][:150]}")
    if not found:
        print("No fact or page matched. If the assistant should know this, add a fact or a page.\n"
              "If it's out of scope (personal or account data), that's expected: make sure the prompt\n"
              "tells the assistant how to redirect, and add an out_of_scope question to evals/golden.yaml.")
    print("\nSet ANTHROPIC_API_KEY to get a written answer from Claude using these tools.")
    return 0


def cmd_ingest(args) -> int:
    from groundtruth import ingest
    project = _p(args)
    try:
        result = ingest.run(project, args.targets, max_pages=args.max_pages, dry_run=args.dry_run)
    except ValueError as exc:
        _bad(str(exc))
        return 2
    _ok(f"ingest{' (dry run)' if args.dry_run else ''}: {result.summary()}")
    for path in result.new[:15]:
        print(f"      + {path}")
    if len(result.new) > 15:
        print(f"      ... and {len(result.new) - 15} more")
    for path in result.updated[:15]:
        print(f"      ~ {path}")
    for path in result.removed[:15]:
        print(f"      - {path} (gone upstream; kept and flagged)")
    for s in result.skipped[:10]:
        print(f"    ! skipped {s}")
    if result.changed and not args.dry_run:
        from groundtruth import review
        pending = review.changes(project)
        if pending:
            print(f"    ! {len(pending)} changed source(s) are cited by your facts or wiki: run ./gt review")
        print("next: ./gt compile   (a work order for turning the evidence into facts and pages)")
    return 0


def cmd_compile(args) -> int:
    from groundtruth import compile as comp, llm
    project = _p(args)
    if args.write:
        if not llm.available():
            _bad("--write needs Claude: set ANTHROPIC_API_KEY or run `ant auth login`. "
                 "Without a key, run ./gt compile and hand the packet to a coding agent.")
            return 2
        written, problems = comp.compile_with_claude(project, limit=args.limit)
        _ok(f"wrote {len(written)} page(s)")
        for w in written:
            print(f"      {w}")
        for p in problems:
            print(f"    ! fact in prose: {p}")
        print("next: review with git diff, then ./gt check && ./gt eval")
        return 0
    drafts = comp.write_drafts(project)
    status = comp.status(project)
    path = comp.packet(project)
    _ok(f"{len(status['uncovered'])} new source(s), {len(status['stale'])} stale page(s)")
    if drafts:
        _ok(f"{len(drafts)} draft page(s) in {project.rel(project.wiki_dir / 'drafts')}/ (not served until "
            f"you set status: published)")
    print(f"\nWork order written to {project.rel(path)}.")
    print("Open this repo in Claude Code (or Codex, Cursor) and say:\n")
    print(f'    "Do the work in {_where(project)}/build/compile-packet.md"\n')
    print("Or let Claude draft the pages directly: ./gt compile --write (needs ANTHROPIC_API_KEY)")
    return 0


def cmd_connect(args) -> int:
    from groundtruth import connect
    project = _p(args)
    clients = [args.client] if args.client else connect.CLIENTS
    if args.install:
        if not args.client:
            _bad("say which client: ./gt connect claude-desktop --install")
            return 2
        try:
            msg = connect.install(args.client, project)
        except ValueError as exc:
            _bad(str(exc))
            return 2
        _ok(msg)
        return 0
    for c in clients:
        try:
            text = connect.snippet(c, project, url=args.url)
        except ValueError as exc:
            _bad(str(exc))
            return 2
        print(f"── {c} " + "─" * max(0, 60 - len(c)))
        print(text)
        print()
    return 0


def cmd_report(args) -> int:
    from groundtruth import report
    project = _p(args)
    path, stats = report.run(project, days=args.days)
    if not stats["calls"]:
        print("No tool calls logged yet. Serve it, ask some questions, then run this again.")
    else:
        _ok(f"{stats['calls']} tool calls, {stats['errors']} errors, {stats['gaps']} lookups found nothing")
    print(f"report: {project.rel(path)}")
    return 0


def cmd_watchdog(args) -> int:
    from groundtruth import watchdog
    return watchdog.run(_p(args), url=args.url, api_key=args.api_key, max_age_hours=args.max_age_hours)


def cmd_refresh(args) -> int:
    from groundtruth import refresh
    return refresh.run(_p(args), write=args.write, open_pr=args.open_pr, live=args.live)


def cmd_bundle(args) -> int:
    from groundtruth import bundle
    project = _p(args)
    path = bundle.build(project)
    _ok(f"{project.rel(path)} ({path.stat().st_size // 1024} KB)")
    return 0


def cmd_init(args) -> int:
    from groundtruth import checks, evals, facts, scaffold
    if args.list or not args.name:
        print("templates:")
        for name, desc in scaffold.templates().items():
            print(f"  {name:10} {desc}")
        if not args.name:
            print("\nusage: ./gt init NAME [--template blank]")
        return 0
    try:
        target = scaffold.init(args.name, args.template, args.title)
    except ValueError as exc:
        _bad(str(exc))
        return 2
    from groundtruth.project import load
    project = load(target)
    facts.build(project)
    report = checks.run(project)
    rc = evals.run(project, "tools", quiet=True)
    _ok(f"created {_where(project)}/ from the {args.template!r} template")
    _ok("./gt now points at it (switch any time with ./gt use PATH)")
    _ok("check passes" if report.ok else "check has problems (run ./gt check)")
    _ok("free eval passes" if rc == 0 else "free eval has failures (run ./gt eval)")
    from groundtruth import examples
    ex = examples.find(project).total
    note = (f"\n        The template's {ex} example items are fictional placeholders so it works on day one.\n"
            f"        The compile packet walks you through replacing them (or: ./gt examples --remove).\n"
            if ex else "")
    print(textwrap.dedent(f"""
        Make it yours:
          1. ./gt ingest https://your-site.example     pull your pages into sources/
          2. ./gt compile                               work order for Claude Code/Codex
             (edit facts/*.yaml and wiki/ by hand any time; ./gt check keeps you honest)
          3. ./gt ask "a real question"                 see what the assistant finds
          4. ./gt connect claude-desktop --install      talk to it in Claude
        {note}
        Everything lives in {_where(project)}/. Start with its README.md.
    """).rstrip())
    return 0


def cmd_use(args) -> int:
    from groundtruth import scaffold
    try:
        target = scaffold.use(args.path)
    except ProjectError as exc:
        _bad(str(exc))
        return 2
    _ok(f"./gt now uses {target}")
    return 0


def cmd_status(args) -> int:
    from groundtruth import checks, compile as comp, facts, observability, search
    project = _p(args)
    print(f"{project.name}  ({_where(project)})")
    try:
        colls = facts.load(project)
        print(f"  facts    {len(colls)} collection(s), {sum(len(c.rows) for c in colls)} rows, "
              f"{sum(len(c.tools) for c in colls)} tools")
    except facts.FactsError as exc:
        print(f"  facts    {len(exc.problems)} problem(s): run ./gt check")
    print(f"  wiki     {len(search.load_pages(project))} public page(s)")
    srcs = comp.sources(project)
    st = comp.status(project)
    from groundtruth import review
    pending = review.changes(project)
    print(f"  sources  {len(srcs)} page(s); {len(st['uncovered'])} not covered yet, {len(pending)} changed since review")
    print(f"  evals    {len(checks.load_golden(project))} golden question(s)")
    from groundtruth import examples
    ex = examples.find(project).total
    if ex:
        print(f"  examples {ex} fictional template item(s) still live (./gt examples)")
    calls = observability.read_log(project)
    print(f"  logs     {len(calls)} tool call(s) recorded")
    nxt = ("./gt review" if pending else "./gt compile" if st["uncovered"] or st["stale"] else
           "./gt connect" if not calls else "./gt report")
    print(f"next: {nxt}")
    return 0


def cmd_doctor(args) -> int:
    import importlib
    import shutil
    print("doctor:")
    bad = 0
    v = sys.version_info
    (_ok if v >= (3, 10) else _bad)(f"python {v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 10) else " (need 3.10+)"))
    bad += v < (3, 10)
    for mod, why in (("mcp", "MCP server"), ("yaml", "config"), ("httpx", "ingest/watchdog"),
                     ("bs4", "ingest"), ("markdownify", "ingest"), ("anthropic", "live evals/compile")):
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "")
            if mod == "mcp":
                from importlib.metadata import version
                ver = version("mcp")
                if not ver.startswith("1."):
                    _bad(f"mcp {ver}: groundtruth needs mcp 1.x. Run ./gt setup")
                    bad += 1
                    continue
            _ok(f"{mod} {ver}".strip() + f"  ({why})")
        except ImportError:
            _bad(f"{mod} missing ({why}). Run ./gt setup")
            bad += 1
    try:
        project = _p(args)
        _ok(f"project: {project.name} ({_where(project)})")
        from groundtruth import checks
        report = checks.run(project)
        (_ok if report.ok else _bad)("check " + ("passes" if report.ok else "fails: run ./gt check"))
        bad += not report.ok
    except ProjectError as exc:
        _bad(str(exc))
        bad += 1
    from groundtruth import llm
    print(f"  {'✓' if llm.available() else '·'} Claude credentials "
          + ("found (live evals, ./gt ask answers, compile --write)" if llm.available()
             else "not set (optional: ANTHROPIC_API_KEY)"))
    print(f"  {'✓' if shutil.which('gh') else '·'} gh CLI " + ("found" if shutil.which("gh") else "not found (optional: refresh --open-pr)"))
    print(f"  {'✓' if shutil.which('docker') else '·'} docker " + ("found" if shutil.which("docker") else "not found (optional: deploy)"))
    print("healthy" if not bad else f"{bad} problem(s) above")
    return 0 if not bad else 1


def cmd_setup(args) -> int:
    import subprocess
    extras = [e for e, on in (("pdf", args.pdf), ("voyage", args.voyage)) if on]
    spec = f".[{','.join(extras)}]" if extras else "."
    cmd = [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "-e", spec]
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=REPO_ROOT)


def cmd_demo(args) -> int:
    from groundtruth import checks, evals, facts, watchdog
    from groundtruth.project import load
    project = load(args.project or DEMO_PROJECT)
    os.environ.setdefault("GT_LOG", "0")
    print(f"groundtruth demo: {project.name} ({_where(project)})\n")
    counts = facts.build(project)
    store = facts.store(project)
    store.refresh(force=True)
    n_tools = len(store.tools)
    _ok(f"facts: {len(counts)} collections, {sum(counts.values())} rows, {n_tools} tools generated from YAML")
    report = checks.run(project)
    if not report.ok:
        _bad("check failed: run ./gt -p examples/library check")
        return 1
    _ok(f"check: {report.notes.get('wiki')}, no facts in prose, {report.notes.get('evals')}")
    rc = evals.run(project, "tools", quiet=True)
    import glob
    latest = sorted(glob.glob(str(project.runs_dir / "*-tools.json")))[-1]
    data = json.loads(Path(latest).read_text())
    if rc != 0:
        _bad("free eval failed: run ./gt -p examples/library eval")
        return 1
    _ok(f"eval (free): {data['passed']}/{data['total']} passed, {data['skipped']} need a model")
    print("  ... starting the MCP server and calling it like a chat client would")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = watchdog.run(project)
    if rc != 0:
        print(buf.getvalue())
        _bad("server self-test failed")
        return 1
    _ok("server: answered over MCP (initialize, tools/list, a fact tool, search_wiki)")
    here = "" if pointer_target() in (None, DEMO_PROJECT) else "-p examples/library "
    print(textwrap.dedent(f"""
        It works. Try it:
          ./gt {here}ask "what time does the central library open on saturday?"
          ./gt {here}ask "how do I get a library card?"
          ./gt {here}connect claude-desktop --install    then ask Claude about the library

        Make your own:
          ./gt init my-assistant --template saas        (or college, city, clinic, blank)
    """).rstrip())
    return 0


# ===================================================================== parser


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gt", add_help=True, description="groundtruth")
    ap.add_argument("-p", "--project", help="project folder (default: see ./gt status)")
    ap.add_argument("--version", action="version", version=f"groundtruth {__version__}")
    sub = ap.add_subparsers(dest="command")

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("demo", cmd_demo, "build and test the bundled library example")
    sp = add("ask", cmd_ask, "ask a question (written answer with an API key, retrieval preview without)")
    sp.add_argument("question", nargs="*")
    sp.add_argument("--offline", action="store_true", help="show the retrieval preview even with a key")
    sp.add_argument("--json", action="store_true", help="print the full preview as JSON")
    sp = add("init", cmd_init, "start your own assistant from a template")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--template", "-t", default="blank")
    sp.add_argument("--title", help="display name (default: from the folder name)")
    sp.add_argument("--list", action="store_true", help="list templates")
    sp = add("use", cmd_use, "point ./gt at a project folder")
    sp.add_argument("path")
    sp = add("ingest", cmd_ingest, "pull a website, sitemap, or folder into sources/")
    sp.add_argument("targets", nargs="*")
    sp.add_argument("--max-pages", type=int)
    sp.add_argument("--dry-run", action="store_true")
    sp = add("compile", cmd_compile, "work order for a coding agent, or --write to draft pages with Claude")
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--limit", type=int, default=20)
    sp = add("build", cmd_build, "rebuild the facts database")
    sp.add_argument("--index", action="store_true", help="also build the semantic search index")
    sp = add("check", cmd_check, "facts, wiki, no-facts-in-prose, eval wiring")
    sp.add_argument("--production", action="store_true", help="also fail on template examples (what serve and Docker use)")
    sp = add("examples", cmd_examples, "list (or --remove) the template's fictional example content")
    sp.add_argument("--remove", action="store_true")
    sp = add("review", cmd_review, "what changed in your sources since facts and wiki were last reviewed")
    sp.add_argument("--done", action="store_true", help="record the current source versions as reviewed")
    sp.add_argument("--diff", action="store_true", help="show what changed in each source")
    sp.add_argument("paths", nargs="*", help="only these sources (default: all)")
    sp = add("eval", cmd_eval, "run the golden set (tools tier is free)")
    sp.add_argument("--tier", default="tools", choices=["tools", "replay", "fast", "live", "release"])
    sp.add_argument("--only")
    sp.add_argument("--no-judge", action="store_true", help="live: skip the LLM judge for narrative answers")
    sp = add("serve", cmd_serve, "run the MCP server")
    sp.add_argument("--stdio", action="store_true")
    sp.add_argument("--force", action="store_true", help="serve even if ./gt check fails (local testing only)")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp = add("connect", cmd_connect, "setup snippets for MCP clients")
    sp.add_argument("client", nargs="?")
    sp.add_argument("--install", action="store_true")
    sp.add_argument("--url", help="your deployed /mcp URL, for hosted clients")
    add("prompt", cmd_prompt, "print the assistant's full instructions (what MCP clients receive)")
    sp = add("tools", cmd_tools, "list the tools your assistant gets")
    sp.add_argument("--json", action="store_true")
    sp = add("report", cmd_report, "observability report from the tool-call log")
    sp.add_argument("--days", type=int, default=30)
    sp = add("watchdog", cmd_watchdog, "outside-in health check")
    sp.add_argument("--url")
    sp.add_argument("--api-key")
    sp.add_argument("--max-age-hours", type=float)
    sp = add("refresh", cmd_refresh, "the weekly agent")
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--open-pr", action="store_true")
    sp.add_argument("--live", action="store_true", help="also run live evals (needs a key)")
    add("bundle", cmd_bundle, "package data for live updates")
    add("status", cmd_status, "where things stand and what to do next")
    add("doctor", cmd_doctor, "check your environment")
    sp = add("setup", cmd_setup, "install or upgrade dependencies")
    sp.add_argument("--pdf", action="store_true", help="also install PDF support")
    sp.add_argument("--voyage", action="store_true", help="also install the Voyage embeddings client")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        try:
            where = _where(resolve(args.project))
        except ProjectError as exc:
            where = f"(none: {exc})"
        print(MENU.format(version=__version__, project=where))
        return 0
    try:
        return int(args.fn(args) or 0)
    except ProjectError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
