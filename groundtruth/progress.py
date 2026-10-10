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


def next_action(project: Project, todo: list[Step], gt: str = "./gt") -> str | None:
    """The one thing to do now. Broken files come first, then pending review, then the road in order."""
    from groundtruth import checks
    try:
        report = checks.run(project, production=True)
    except Exception as exc:  # noqa: BLE001 - status must guide, not crash
        return f"{gt} check  (it can't even run: {exc})"
    def is_review(kind: str, msg: str) -> bool:  # handled by the review step, not "broken"
        return kind == "sources" and "review" in msg and "same document" not in msg
    broken = [f"{k}: {e}" for k, v in report.errors.items() if k != "examples" for e in v if not is_review(k, e)]
    if broken:
        return f"{gt} check, and fix: {broken[0]}"
    review = next((s for s in todo if s.title == REVIEW and not s.done), None)
    if review:
        return review.next
    first = next((s for s in todo if not s.done), None)
    return first.next if first else None


REVIEW = "Review changed sources"


def steps(project: Project, gt: str = "./gt", where: str = "") -> list[Step]:
    """The road, in order. ``gt`` and ``where`` make the suggested commands copy-pasteable."""
    from groundtruth import checks, compile as comp, evals, examples, facts, observability, review

    ex = examples.find(project)
    fictional_sources = {p.resolve() for p in ex.sources}
    real_sources = [s for s in comp.sources(project) if s.path.resolve() not in fictional_sources]
    uncovered = comp.uncovered(project)
    uncovered = [s for s in uncovered if s.path.resolve() not in fictional_sources]
    drafts = [p for p in checks.wiki_pages(project) if checks.is_draft(p)]
    try:
        golden = checks.load_golden(project)
    except Exception:  # noqa: BLE001 - broken YAML: ./gt check explains it, next_action points there
        golden = []
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
    try:
        report = checks.run(project, production=True)
    except Exception:  # noqa: BLE001
        report = checks.Report()
        report.error("check", "./gt check can't run")
    results = evals.run_tools_tier(project, golden) if report.ok and golden else []
    failing = [r["id"] for r in results if not r.get("skipped") and not r["passed"]]
    calls = [r for r in observability.read_log(project) if not r.get("probe")]  # watchdog probes aren't use

    proposed = comp.proposals(project)
    work = []
    if proposed:
        work.append(f"{len(proposed)} proposed facts file(s) to review")
    if uncovered:
        work.append(f"{len(uncovered)} source(s) not covered")
    if drafts:
        work.append(f"{len(drafts)} draft page(s) unfinished")
    q_detail = f"{len(real_questions)} question(s) of your own"
    if untested:
        q_detail += f"; no question tests {', '.join(untested[:3])}{'...' if len(untested) > 3 else ''}"
    problems = [f"{k}: {e}" for k, v in report.errors.items() for e in v]
    first = problems[0] if problems else ""
    gate_detail = ("check --production and eval pass" if report.ok and not failing else
                   f"{len(problems)} problem(s), first: {first[:70]}{'...' if len(first) > 70 else ''}"
                   if not report.ok else f"eval fails: {', '.join(failing[:3])}")
    packet = f"{where}/build/compile-packet.md" if where else "build/compile-packet.md"
    if proposed or drafts:
        parts = []
        if proposed:
            parts.append(f"check {project.rel(proposed[0])}{' (and the others)' if len(proposed) > 1 else ''} "
                         f"against its source and rename it without the _")
        if drafts:
            parts.append(f"finish the draft pages in {project.rel(project.wiki_dir / 'drafts')}/ and set "
                         f"status: published")
        compile_next = "; ".join(parts) + f". Or tell a coding agent: \"do the work in {packet}\""
    else:
        compile_next = f"{gt} compile, then tell a coding agent: \"do the work in {packet}\""
    return [
        Step("Capture your evidence", bool(real_sources), f"{len(real_sources)} source(s) of your own",
             f"{gt} ingest URL_OR_FOLDER"),
        Step("Turn it into facts and pages", bool(real_sources) and not work,
             ", ".join(work) or ("every source is covered" if real_sources else "nothing to work from yet"),
             compile_next),
        Step("Replace the template examples", not ex.total,
             f"{ex.total} fictional item(s) left" if ex.total else "none left",
             f"{gt} examples --remove (once your own facts and pages replace them)"),
        Step("Write eval questions", bool(real_questions) and not untested, q_detail,
             "add questions to evals/golden.yaml (the compile packet has starters); see docs/evals.md"),
        Step(REVIEW, has_lock and not pending,
             f"{len(pending)} source(s) waiting for review" if pending else
             "all reviewed" if has_lock else "never reviewed",
             f"{gt} review --diff, fix what's stale, then {gt} review --done"),
        Step("Pass the gates", report.ok and not failing, gate_detail,
             f"{gt} check --production && {gt} eval"),
        Step("Try it in a client", bool(calls), f"{len(calls)} real tool call(s) logged",
             f"{gt} connect claude-desktop --install, restart it, and ask a real question"),
    ]
