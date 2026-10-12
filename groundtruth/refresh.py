"""``./gt refresh``: the agent that keeps your knowledge current.

    ingest -> build -> compile -> check -> eval -> pull request, or a push

    ./gt refresh                    # dry run: reports what would change, writes nothing
    ./gt refresh --write            # does the work on a refresh/<date> branch
    ./gt refresh --write --open-pr  # ...and opens a pull request (needs the gh CLI)
    ./gt refresh --write --push     # commits right here for the refresh host to push (./gt host)
    ./gt refresh --write --push --quick   # the same, re-capturing only the sources facts cite

Two ways to run it unattended:
  * Weekly in GitHub Actions (.github/workflows/refresh.yml): changes land on a
    dated branch and a pull request a person reviews and merges.
  * Nightly on a refresh host (./gt host): it commits in a worktree at origin/main
    and the host pushes main when the gates pass. Stale pages are left to the
    rewrite job (./gt rewrite), and a source your facts cite raises an alert, since
    facts wait for a person (./gt review --done).

Why it's safe to leave on a schedule:
  * A quiet run is free: no source changes means no commits, no PR, no API spend.
  * Gates first. If a check or eval fails, it stops and says why; nothing ships.
  * A source outage can't wipe your evidence: a refresh that would flag a large
    share of your sources as removed upstream stops instead.
  * Claude is optional. With ANTHROPIC_API_KEY the branch mode drafts and refreshes
    wiki pages; without one it writes build/compile-packet.md into the PR.
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

from groundtruth import alerts, checks, compile as compile_mod, evals, facts, ingest, llm
from groundtruth.project import Project

REMOVED_SHARE_LIMIT = 0.2  # more than this share of live sources gone at once looks like an outage, not edits
REMOVED_COUNT_FLOOR = 3


def _git(project: Project, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(project.root), *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def _commit(project: Project, paths: list[Path], message: str) -> bool:
    rels = [str(p) for p in paths if p.exists()]
    if not rels or not _git(project, "status", "--porcelain", "--", *rels):
        return False
    _git(project, "add", "-A", "--", *rels)
    _git(project, "commit", "-q", "-m", message)
    return True


def fact_sources(project: Project) -> list[str]:
    """The sources a facts file cites: the ones a quick refresh re-captures."""
    from groundtruth import review
    return sorted(src for src, deps in review.dependents(project).items()
                  if deps["facts"] and (project.root / src).exists())


def _outage(project: Project, result: ingest.IngestResult) -> str | None:
    """Why this ingest looks like the site being down rather than real removals (None if it doesn't)."""
    live = sum(1 for s in compile_mod.sources(project) if not ingest.page_meta(s.path).get("removed_upstream"))
    gone = len(result.removed)
    if gone >= REMOVED_COUNT_FLOOR and gone > REMOVED_SHARE_LIMIT * (live + gone):
        return (f"{gone} of {live + gone} sources look removed upstream at once, which looks like an outage. "
                f"Nothing was committed; check the site, then run ./gt ingest by hand if they really are gone")
    return None


def run(project: Project, *, write: bool = False, open_pr: bool = False, live: bool = False,
        push: bool = False, quick: bool = False) -> int:
    today = dt.date.today().isoformat()
    mode = "push" if push else "write" if write else "dry run"
    print(f"refresh: {project.name} ({mode}{', quick' if quick else ''}, {today})")
    lines: list[str] = []
    write = write or push

    def fail(msg: str) -> int:
        print(f"  ✗ {msg}")
        if push:  # on the refresh host nobody reads the output: say it where someone will see it
            alerts.notify(project, "the refresh failed", msg)
        return finish(1)

    branch = original = None
    if write:
        if _git(project, "status", "--porcelain"):
            print("  ✗ the worktree has uncommitted changes; commit or stash them first")
            return 1
        if not push:
            original = _git(project, "branch", "--show-current")
            branch = f"refresh/{today}"
            if _git(project, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
                _git(project, "checkout", "-q", branch)
            else:
                _git(project, "checkout", "-q", "-b", branch)
    start = _git(project, "rev-parse", "HEAD", check=False) if push else ""

    def finish(code: int) -> int:
        if push and code != 0 and start:
            _git(project, "reset", "-q", "--hard", start, check=False)  # nothing half-done is left to push
        if write and original and branch:
            if code == 0 and not _git(project, "log", f"{original}..{branch}", "--oneline", check=False):
                _git(project, "checkout", "-q", original)
                _git(project, "branch", "-q", "-D", branch, check=False)
            elif code == 0:
                _git(project, "checkout", "-q", original)
            else:
                print(f"  stopped on branch {branch} so you can inspect it")
        return code

    try:
        # 1. evidence
        if quick:
            targets = fact_sources(project)
            result = ingest.recapture(project, targets, dry_run=not write)
            print(f"  ✓ ingest (quick, {len(targets)} source(s) facts cite): {result.summary()}")
        elif project.ingest_sources:
            result = ingest.run(project, [], dry_run=not write, save=False)
            print(f"  ✓ ingest: {result.summary()}")
        else:
            result = None
            print("  · ingest: no ingest.sources configured, using sources/ as-is")
        if result is not None:
            for s in result.skipped[:10]:
                print(f"    ! skipped {s}")
            outage = _outage(project, result)
            if outage and write:
                return fail(outage)
            lines.append(f"- Sources: {result.summary()}")
            stamp = f"{today} {dt.datetime.now():%H:%M}" if quick else today
            message = f"refresh: {'quick ' if quick else ''}sources ({stamp})"
            if write and _commit(project, [project.sources_dir], message):
                lines.append("  - committed source changes")

        # 2. facts
        try:
            counts = facts.build(project)
        except facts.FactsError as exc:
            return fail(f"facts don't build: {exc.problems[0]}")
        print(f"  ✓ build: {sum(counts.values())} fact rows in {len(counts)} collection(s)")

        # 3. wiki
        status = compile_mod.status(project)
        todo = len(status["uncovered"]) + len(status["stale"])
        if push:  # the rewrite job (./gt rewrite) handles stale pages; new sources wait for ./gt compile
            print(f"  · wiki: {len(status['stale'])} stale page(s) for ./gt rewrite, "
                  f"{len(status['uncovered'])} new source(s) nothing covers yet (./gt compile)")
        elif todo and write and llm.available():
            written, problems = compile_mod.compile_with_claude(project)
            print(f"  ✓ compile: {len(written)} page(s) drafted or refreshed with {llm.model()}")
            lines.append(f"- Wiki: Claude drafted/refreshed {len(written)} page(s): review them")
            lines += [f"  - `{w}`" for w in written]
            for p in problems:
                print(f"    ! {p}")
            _commit(project, [project.wiki_dir], f"refresh: wiki ({today})")
        elif todo:
            packet = compile_mod.packet(project)
            print(f"  · compile: {len(status['uncovered'])} new source(s), {len(status['stale'])} stale page(s); "
                  f"work order in {project.rel(packet)}")
            lines.append(f"- Wiki: {len(status['uncovered'])} new source(s) and {len(status['stale'])} stale "
                         f"page(s) need a page. Hand `{project.rel(packet)}` to a coding agent.")
        else:
            print("  ✓ compile: nothing new or stale")

        # 4. gates. Changed sources aren't fatal here: they go in the PR (or an alert) as a review
        # list, and ./gt check stays red until a person reconciles them and runs ./gt review --done.
        from groundtruth import review
        report = checks.run(project)
        fatal = {k: v for k, v in report.errors.items() if k != "sources" and v}
        if fatal:
            for section, errs in fatal.items():
                for e in errs:
                    print(f"  ✗ {section}: {e}")
            first = next(f"{k}: {e[0]}" for k, e in fatal.items())
            return fail(f"./gt check fails after the refresh ({first})")
        pending = review.changes(project)
        if pending:
            print(f"  ! {len(pending)} changed source(s) need a review (listed in the PR)" if not push else
                  f"  ! {len(pending)} changed source(s) need a review")
            lines.append("- **Needs your review** (facts and pages written against an older source; "
                         "`./gt check` stays red until you run `./gt review --done`):")
            for ch in pending:
                lines.append(f"  - `{ch.source}` ({ch.reason}): " + ", ".join(f"`{x}`" for x in ch.facts + ch.wiki))
            cited = [ch for ch in pending if ch.facts]
            from groundtruth import reconcile
            if push and cited and not reconcile.enabled(project):  # otherwise ./gt rewrite updates them
                alerts.notify(project, "facts cite sources that changed",
                              f"{len(cited)} source(s) your facts cite changed: "
                              + "; ".join(f"{ch.source} ({', '.join(ch.facts)})" for ch in cited[:5])
                              + ". Run ./gt review --diff, fix the facts, then ./gt review --done.")
        print("  ✓ check" + (" (except the source review above)" if pending else ""))
        if evals.run(project, "tools", quiet=True) != 0:
            return fail("./gt eval (tools tier) fails after the refresh: run ./gt eval to see why")
        print("  ✓ eval (tools tier)")
        if live:
            if evals.run(project, "live", quiet=False) != 0:
                return finish(1)
            print("  ✓ eval (live)")

        # 5. publish
        if push:
            made = _git(project, "log", f"{start}..HEAD", "--oneline", check=False) if start else ""
            if made:
                print("done: committed; the refresh host pushes it")
            elif not quick:  # a quiet night still says it ran, or the watchdog can't tell it from a dead host
                _git(project, "commit", "-q", "--allow-empty", "-m", f"refresh: no source changes ({today})",
                     "-m", "\n".join(lines) or "No ingest sources are configured.")
                print("quiet run: no source changed; recorded the run with an empty commit for the watchdog")
            else:
                print("quiet run: nothing changed")
            return finish(0)
        if write and branch and original:
            changed = _git(project, "log", f"{original}..{branch}", "--oneline", check=False)
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
                    _git(project, "push", "-q", "-u", "origin", branch)
                    subprocess.run(["gh", "pr", "create", "--title", f"Knowledge refresh {today}",
                                    "--body", body, "--head", branch], cwd=project.root, check=False)
            print(f"done: changes are on branch {branch}")
        else:
            print("dry run complete. Re-run with --write to commit changes on a branch.")
        return finish(0)
    except Exception as exc:  # noqa: BLE001
        return fail(f"{type(exc).__name__}: {exc}")
