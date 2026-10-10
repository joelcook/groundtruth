"""Where a project is on the road from template to production, and the one next step.

``./gt status`` prints this. Each step is something you can check from the files
alone, so the list is always honest about what's left: a fresh template is not
"ready" just because its fictional examples pass the checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from groundtruth.project import Project


@dataclass
class Step:
    title: str
    done: bool
    detail: str
    next: str  # the command (or action) that moves this step forward


def steps(project: Project, gt: str = "./gt", where: str = "") -> list[Step]:
    """The road, in order. ``gt`` and ``where`` make the suggested commands copy-pasteable."""
    from groundtruth import checks, compile as comp, evals, examples, facts, observability, review

    ex = examples.find(project)
    fictional_sources = {p.resolve() for p in ex.sources}
    real_sources = [s for s in comp.sources(project) if s.path.resolve() not in fictional_sources]
    uncovered = comp.uncovered(project)
    uncovered = [s for s in uncovered if s.path.resolve() not in fictional_sources]
    drafts = [p for p in checks.wiki_pages(project) if checks.is_draft(p)]
    stale = comp.stale_pages(project)
    golden = checks.load_golden(project)
    example_ids = set(ex.questions)
    real_questions = [q for q in golden if q.get("id") not in example_ids]
    try:
        collections = facts.load(project)
    except facts.FactsError:
        collections = []
    tested = {q.get("must_call_tool") for q in real_questions}
    fictional_facts = {p.resolve() for p in ex.facts}
    untested = [t.name for c in collections if c.file and Path(c.file).resolve() not in fictional_facts
                for t in c.tools if t.name not in tested]
    pending = review.changes(project)
    has_lock = review.load_lock(project) is not None
    report = checks.run(project, production=True)
    results = evals.run_tools_tier(project, golden) if report.ok and golden else []
    failing = [r["id"] for r in results if not r.get("skipped") and not r["passed"]]
    calls = observability.read_log(project)

    work = []
    if uncovered:
        work.append(f"{len(uncovered)} source(s) not covered")
    if drafts:
        work.append(f"{len(drafts)} draft page(s) unfinished")
    if stale:
        work.append(f"{len(stale)} page(s) whose sources changed")
    q_detail = f"{len(real_questions)} question(s) of your own"
    if untested:
        q_detail += f"; no question tests {', '.join(untested[:3])}{'...' if len(untested) > 3 else ''}"
    problems = [f"{k}: {e}" for k, v in report.errors.items() for e in v]
    first = problems[0] if problems else ""
    gate_detail = ("check --production and eval pass" if report.ok and not failing else
                   f"{len(problems)} problem(s), first: {first[:70]}{'...' if len(first) > 70 else ''}"
                   if not report.ok else f"eval fails: {', '.join(failing[:3])}")
    packet = f"{where}/build/compile-packet.md" if where else "build/compile-packet.md"
    return [
        Step("Capture your evidence", bool(real_sources), f"{len(real_sources)} source(s) of your own",
             f"{gt} ingest URL_OR_FOLDER"),
        Step("Turn it into facts and pages", bool(real_sources) and not work,
             ", ".join(work) or ("every source is covered" if real_sources else "nothing to work from yet"),
             f"{gt} compile, then tell a coding agent: \"do the work in {packet}\""),
        Step("Replace the template examples", not ex.total,
             f"{ex.total} fictional item(s) left" if ex.total else "none left",
             f"{gt} examples --remove (once your own facts and pages replace them)"),
        Step("Write eval questions", bool(real_questions) and not untested, q_detail,
             "add questions to evals/golden.yaml (the compile packet has starters); see docs/evals.md"),
        Step("Review changed sources", has_lock and not pending,
             f"{len(pending)} source(s) waiting for review" if pending else
             "all reviewed" if has_lock else "never reviewed",
             f"{gt} review --diff, fix what's stale, then {gt} review --done"),
        Step("Pass the gates", report.ok and not failing, gate_detail,
             f"{gt} check --production && {gt} eval"),
        Step("Try it in a client", bool(calls), f"{len(calls)} tool call(s) logged",
             f"{gt} connect claude-desktop --install, restart it, and ask a real question"),
    ]
