"""``./gt refresh``: the weekly agent that keeps your knowledge current.

    ingest -> build -> compile -> check -> eval -> pull request

    ./gt refresh                    # dry run: reports what would change, writes nothing
    ./gt refresh --write            # does the work on a refresh/<date> branch
    ./gt refresh --write --open-pr  # ...and opens a pull request (needs the gh CLI)

Why it's safe to leave on a schedule (.github/workflows/refresh.yml runs it weekly):
  * It never touches main. Changes land on a dated branch a human reviews.
  * A quiet week is free: no source changes means no commits, no PR, no API spend.
  * Gates first. If a check or eval fails, it stops and says why; nothing ships.
  * Claude is optional. With ANTHROPIC_API_KEY it drafts and refreshes wiki pages;
    without one it writes build/compile-packet.md into the PR for a person or agent.
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

from groundtruth import checks, compile as compile_mod, evals, facts, ingest, llm
from groundtruth.project import REPO_ROOT, Project


def _git(*args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def _commit(paths: list[Path], message: str) -> bool:
    rels = [str(p) for p in paths if p.exists()]
    if not rels or not _git("status", "--porcelain", "--", *rels):
        return False
    _git("add", "--", *rels)
    _git("commit", "-q", "-m", message)
    return True


def run(project: Project, *, write: bool = False, open_pr: bool = False, live: bool = False) -> int:
    today = dt.date.today().isoformat()
    print(f"refresh: {project.name} ({'write' if write else 'dry run'}, {today})")
    lines: list[str] = []

    branch = original = None
    if write:
        if _git("status", "--porcelain"):
            print("  ✗ the worktree has uncommitted changes; commit or stash them first")
            return 1
        original = _git("branch", "--show-current")
        branch = f"refresh/{today}"
        if _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
            _git("checkout", "-q", branch)
        else:
            _git("checkout", "-q", "-b", branch)

    def finish(code: int) -> int:
        if write and original and branch:
            if code == 0 and not _git("log", f"{original}..{branch}", "--oneline", check=False):
                _git("checkout", "-q", original)
                _git("branch", "-q", "-D", branch, check=False)
            elif code == 0:
                _git("checkout", "-q", original)
            else:
                print(f"  stopped on branch {branch} so you can inspect it")
        return code

    try:
        # 1. evidence
        if project.ingest_sources:
            result = ingest.run(project, [], dry_run=not write, save=False)
            print(f"  ✓ ingest: {result.summary()}")
            lines.append(f"- Sources: {result.summary()}")
            if write and _commit([project.sources_dir], f"refresh: sources ({today})"):
                lines.append("  - committed source changes")
        else:
            print("  · ingest: no ingest.sources configured, using sources/ as-is")

        # 2. facts
        counts = facts.build(project)
        print(f"  ✓ build: {sum(counts.values())} fact rows in {len(counts)} collection(s)")

        # 3. wiki
        status = compile_mod.status(project)
        todo = len(status["uncovered"]) + len(status["stale"])
        if todo and write and llm.available():
            written, problems = compile_mod.compile_with_claude(project)
            print(f"  ✓ compile: {len(written)} page(s) drafted or refreshed with {llm.model()}")
            lines.append(f"- Wiki: Claude drafted/refreshed {len(written)} page(s): review them")
            lines += [f"  - `{w}`" for w in written]
            for p in problems:
                print(f"    ! {p}")
            _commit([project.wiki_dir], f"refresh: wiki ({today})")
        elif todo:
            packet = compile_mod.packet(project)
            print(f"  · compile: {len(status['uncovered'])} new source(s), {len(status['stale'])} stale page(s); "
                  f"work order in {project.rel(packet)}")
            lines.append(f"- Wiki: {len(status['uncovered'])} new source(s) and {len(status['stale'])} stale "
                         f"page(s) need a page. Hand `{project.rel(packet)}` to a coding agent.")
        else:
            print("  ✓ compile: nothing new or stale")

        # 4. gates. Changed sources aren't fatal here: they go in the PR as a review list,
        # and CI keeps the PR red until a human reconciles them and runs ./gt review --done.
        from groundtruth import review
        report = checks.run(project)
        fatal = {k: v for k, v in report.errors.items() if k != "sources" and v}
        if fatal:
            for section, errs in fatal.items():
                for e in errs:
                    print(f"  ✗ {section}: {e}")
            return finish(1)
        pending = review.changes(project)
        if pending:
            print(f"  ! {len(pending)} changed source(s) need a human review (listed in the PR)")
            lines.append("- **Needs your review** (facts and pages written against an older source; "
                         "`./gt check` stays red until you run `./gt review --done`):")
            for ch in pending:
                lines.append(f"  - `{ch.source}` ({ch.reason}): " + ", ".join(f"`{x}`" for x in ch.facts + ch.wiki))
        print("  ✓ check" + (" (except the source review above)" if pending else ""))
        if evals.run(project, "tools", quiet=True) != 0:
            print("  ✗ eval (tools tier) failed: run ./gt eval to see why")
            return finish(1)
        print("  ✓ eval (tools tier)")
        if live:
            if evals.run(project, "live", quiet=False) != 0:
                return finish(1)
            print("  ✓ eval (live)")

        # 5. pull request
        if write and branch and original:
            changed = _git("log", f"{original}..{branch}", "--oneline", check=False)
            if not changed:
                print("quiet run: nothing changed, nothing to review, $0 spent")
                return finish(0)
            if open_pr:
                if not shutil.which("gh"):
                    print("  ! gh CLI not found: push the branch and open the PR yourself")
                else:
                    gates = ("`./gt eval` passed; `./gt check` passes once the review above is done."
                             if pending else "Gates passed: `./gt check`, `./gt eval`.")
                    body = "\n".join([f"Automated refresh for **{project.name}** ({today}).", "", *lines, "",
                                      gates, "", "Review the diff, then merge."])
                    _git("push", "-q", "-u", "origin", branch)
                    subprocess.run(["gh", "pr", "create", "--title", f"Knowledge refresh {today}",
                                    "--body", body, "--head", branch], cwd=REPO_ROOT, check=False)
            print(f"done: changes are on branch {branch}")
        else:
            print("dry run complete. Re-run with --write to commit changes on a branch.")
        return finish(0)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {type(exc).__name__}: {exc}")
        return finish(1)
