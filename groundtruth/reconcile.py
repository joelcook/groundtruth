"""Unattended fact updates: when a source behind your facts changes, bring the facts up to date.

``./gt rewrite`` runs this before it rewrites pages. For each source that changed
(or vanished upstream) and that facts files cite, headless Claude Code edits
those facts files to match the source now, plus the expected values of the
golden questions that check them. The change ships only if:

  1. it touched nothing else. In evals/golden.yaml, only the expected values of
     questions that call those files' tools may change, and in the facts files
     only the rows (and, for a vanished source, which source they cite);
  2. every exact value in every row it changed or added (numbers, money, dates,
     times, phone numbers, emails, URLs) appears in that row's cited source
     (grounding.py). It can't introduce a number the source doesn't say;
  3. a change to more than GT_FACTS_MAX_CHANGE_SHARE (0.3) of the rows (a source
     redesigned, or broken) needs a second, stronger checking model to agree too;
  4. ./gt check finds nothing new and ./gt eval passes;
  5. a second model, reading each changed row beside its source, agrees.

It's committed as ``facts: update <files> from <source>``, with every changed row
in the message, and those files count as checked against that version of the
source. Set ``facts_updates: human`` in groundtruth.yaml to keep a person in the
loop instead: changed facts then wait for ./gt review --done.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from groundtruth import alerts, facts, grounding, host, review, rewrite
from groundtruth.project import Project

COMMIT_PREFIX = "facts: update"
EXPECTED_FIELDS = {"must_contain", "must_match", "must_not_contain"}
log = host.log


def enabled(project: Project) -> bool:
    return str(project.config.get("facts_updates") or "llm").strip().lower() != "human"


def max_change_share() -> float:
    return float(os.environ.get("GT_FACTS_MAX_CHANGE_SHARE") or 0.3)


def todo(project: Project, state: dict) -> dict[str, list[str]]:
    """Sources facts cite that changed or vanished (source -> facts files) and aren't reconciled yet."""
    if not enabled(project):
        return {}
    pending = {c.source for c in review.changes(project)} | set(review.removed_upstream(project))
    hashes = review.current(project)
    out = {}
    for src, deps in sorted(review.dependents(project).items()):
        files = deps.get("facts") or []
        if src in pending and files and any(
                rewrite.checked_against(project, state, "facts_verified", f).get(src) != hashes.get(src) for f in files):
            out[src] = files
    return out


# ===================================================================== prompts


def _tools_in(project: Project, files: list[str]) -> list[str]:
    try:
        wanted = {(project.root / f).resolve() for f in files}
        return [t.name for c in facts.load(project) if Path(c.file).resolve() in wanted for t in c.tools]
    except facts.FactsError:
        return []


def build_prompt(project: Project, src: str, files: list[str], errors: list[str]) -> str:
    pre = rewrite._prefix(project)
    tools = _tools_in(project, files)
    gone = src in review.removed_upstream(project)
    parts = [
        f"You maintain this repo's facts without a human in the loop. A source {'was removed upstream' if gone else 'changed'}: "
        f"`{pre}{src}`. Update the facts that cite it so they match what the sources say now:",
        *[f"- `{pre}{f}`" for f in files],
        "",
        "First read CLAUDE.md and docs/facts.md. Then read the source and each facts file.",
        "",
        "Rules:",
        f"- Edit only those facts files and `{pre}evals/golden.yaml`. In golden.yaml change only the",
        "  must_contain, must_match, and must_not_contain values of questions that call "
        + (", ".join(f"`{t}`" for t in tools) or "these files' tools") + ".",
        f"  Change nothing else anywhere. Never edit `{pre}sources/`.",
        "- Change a row only where the source now says something different. Every exact value you",
        "  write (numbers, money, dates, times, phone numbers, emails, URLs) must appear in the row's",
        "  cited source, so the same number, date, or time is plainly there. A script checks this.",
        "- Add a row only for something new the source states. Remove a row only when no current",
        "  source supports it any more.",
        "- Keep each file's key, tools, descriptions, and other settings as they are. Keep row ids",
        "  unless the source renamed the thing.",
        "- When a fact a golden question checks changes, change that question's expected value (and",
        "  tool_args, if the thing was renamed) to match. If the source dropped a fact entirely, remove",
        "  its row, and remove the questions that only checked it. A facts file left with no rows:",
        "  empty it, and the script removes it. Don't add questions.",
        rewrite.UNTRUSTED,
    ]
    if gone:
        parts += [
            "- This source is gone. For each row that cites it, find a current source under",
            f"  `{pre}sources/pages/` that states the same values (use Grep) and cite that one instead,",
            "  either the file's `source:` or a row-level `source: {path: ...}`. Remove rows that no",
            "  current source supports.",
        ]
    parts += [
        "",
        "Afterwards a script checks every value you changed against its source, runs ./gt check and",
        "./gt eval, and a second model checks each changed row against the source. You can't run them.",
        "When you finish, reply with one sentence saying what changed.",
    ]
    if errors:
        parts += ["", "A previous attempt failed these checks. Fix them:", *[f"- {e}" for e in errors[:20]]]
    d = review.diff(project, src)
    if d:
        cut = d[: rewrite.DIFF_CHARS] + (" [...]" if len(d) > rewrite.DIFF_CHARS else "")
        parts += ["", "What changed in the source since the facts were last checked:", "", f"```diff\n{cut}\n```"]
    return "\n".join(parts)


def build_check_prompt(project: Project, src: str, files: list[str], changes: list[str]) -> str:
    pre = rewrite._prefix(project)
    return "\n".join([
        "Check facts against the source they cite, after an automated update. Only read; edit nothing.",
        "",
        f"Read `{pre}{src}`, then these facts files: {', '.join(f'`{pre}{f}`' for f in files)}.",
        "Every row in them that cites that source (a row's own `source`, or else the file's) must be",
        "supported by it: every value, including names, days, descriptions, and `notes` (the assistant",
        "reads them as part of the fact). Every alias must be a name the source uses for that thing.",
        "A row that no current source supports any more should have been removed.",
        "",
        "These rows just changed (before and after):" if changes else "No rows changed: the update decided the "
        "facts already match the changed source. Check that this is true.",
        "",
        *changes,
        "",
        "Reply with only this JSON and no other text:",
        ('{"verdict": "pass" or "fail", "unsupported": [{"claim": "file, row, and the value in question", '
         '"why": "what the source says instead, or that it says nothing"}]}'),
        'Use "pass" with an empty list when every change holds up.',
    ])


# ===================================================================== validating a candidate


@dataclass
class Diff:
    changed: list[str] = field(default_factory=list)  # human-readable, for the checker and the commit
    problems: list[str] = field(default_factory=list)
    rows_before: int = 0
    touched: int = 0


def _yaml(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _row_yaml(row: dict) -> str:
    return yaml.safe_dump(row, sort_keys=False, allow_unicode=True, width=10_000, default_flow_style=True).strip()


def diff_facts(project: Project, rel: str, before: str, after: str, src: str) -> Diff:
    """What changed in one facts file, and whether every new exact value is grounded."""
    out = Diff()
    old, new = _yaml(before) or {}, _yaml(after)
    if not after.strip() and before.strip():
        new = {**old, "rows": []}  # the file was deleted: its last fact is gone, so is its tool
    if not isinstance(new, dict):
        out.problems.append(f"{rel} isn't a valid facts file any more")
        return out
    gone = src in review.removed_upstream(project)
    for k in sorted((set(old) | set(new)) - {"rows"}):
        if old.get(k) != new.get(k) and not (k == "source" and gone):
            out.problems.append(f"{rel}: `{k}` changed; only rows may change (and, for a vanished source, `source`)")
    key = str(new.get("key") or "id")
    default = (new.get("source") or {}).get("path") if isinstance(new.get("source"), dict) else None
    old_rows = {str(r.get(key)): r for r in old.get("rows") or [] if isinstance(r, dict)}
    new_rows = {str(r.get(key)): r for r in new.get("rows") or [] if isinstance(r, dict)}
    out.rows_before = len(old_rows)
    cache: dict = {}
    for rid, row in new_rows.items():
        prev = old_rows.get(rid)
        if prev == row:
            continue
        out.touched += 1
        sources = grounding.row_sources(row, default)
        moved = prev is None or grounding.row_sources(prev, (old.get("source") or {}).get("path")
                                                      if isinstance(old.get("source"), dict) else None) != sources
        fields = None if moved else {f for f in set(row) | set(prev) if row.get(f) != prev.get(f)}
        out.problems += grounding.row_problems(project, row, key, sources, rel, cache, fields)
        old_aliases = {str(a).lower() for a in (prev or {}).get("aliases") or []}
        for alias in [str(a) for a in row.get("aliases") or [] if str(a).lower() not in old_aliases]:
            if not any((idx := grounding._load(project, s, cache)) and alias.lower() in idx.text for s in sources):
                out.problems.append(f"{rel}: row {rid} gained the alias {alias!r}, a word its source doesn't use "
                                    f"(an alias decides which row a question finds)")
        for s in sources:
            if s in review.removed_upstream(project):
                out.problems.append(f"{rel}: row {rid} still cites {s}, which was removed upstream")
            elif not (project.root / s).is_file():
                out.problems.append(f"{rel}: row {rid} cites {s}, which doesn't exist")
        out.changed.append(f"{rel} row {rid}:\n  before: {_row_yaml(prev) if prev else '(new row)'}\n"
                           f"  after:  {_row_yaml(row)}")
    for rid in old_rows.keys() - new_rows.keys():
        out.touched += 1
        out.changed.append(f"{rel} row {rid}:\n  before: {_row_yaml(old_rows[rid])}\n  after:  (removed)")
    return out


def diff_golden(project: Project, before: str, after: str, tools: list[str], src: str) -> tuple[list[str], list[str]]:
    """(changes, problems) in evals/golden.yaml: only the expected values of these tools' questions may move."""
    old, new = _yaml(before) or [], _yaml(after)
    if not isinstance(new, list):
        return [], ["evals/golden.yaml isn't a list of questions any more"]
    olds = {str(i.get("id")): i for i in old if isinstance(i, dict)}
    news = {str(i.get("id")): i for i in new if isinstance(i, dict)}
    index = grounding._load(project, src, {})
    changes, problems = [], []
    if news.keys() - olds.keys():
        problems.append("evals/golden.yaml: questions were added; a fact update may only change or retire them")
    for qid in olds.keys() - news.keys():
        prev = olds[qid]
        if prev.get("must_call_tool") not in tools or not _fact_gone(project, prev):
            problems.append(f"evals/golden.yaml: question {qid} was removed, but the fact it checks is still there")
        else:
            changes.append(f"evals/golden.yaml question {qid}: retired (the fact it checked is gone)")
    for qid, item in news.items():
        prev = olds.get(qid)
        if prev is None or item == prev:
            continue
        moved = {k for k in set(item) | set(prev) if item.get(k) != prev.get(k)}
        if moved - EXPECTED_FIELDS - {"tool_args"} or item.get("must_call_tool") not in tools:
            problems.append(f"evals/golden.yaml: question {qid} changed {sorted(moved)}; only the expected values "
                            f"and tool_args of questions that call {', '.join(tools) or 'these tools'} may change")
            continue
        for k in moved & {"must_contain", "must_match"}:
            for kind, norm, raw in grounding.exact_values(item.get(k)):
                if index is not None and not index.has(kind, norm):
                    problems.append(f"evals/golden.yaml: question {qid} now expects {raw!r}, which {src} doesn't say")
        changes.append(f"evals/golden.yaml question {qid}: " + "; ".join(
            f"{k} {prev.get(k)!r} -> {item.get(k)!r}" for k in sorted(moved)))
    return changes, problems


def _fact_gone(project: Project, question: dict) -> bool:
    """Is the fact a golden question checked really gone: its tool removed, or nothing found for its arguments?"""
    from groundtruth import server
    facts.store(project).refresh(force=True)
    tool = question.get("must_call_tool")
    if tool not in facts.store(project).tools:
        return True
    try:
        result = server.call_tool(project, str(tool), dict(question.get("tool_args") or {}), log=False)
    except Exception:  # noqa: BLE001
        return True
    return isinstance(result, dict) and not result.get("count")


# ===================================================================== one source


@dataclass
class Result:
    outcome: rewrite.Outcome
    changes: list[str] = field(default_factory=list)


def reconcile_source(project: Project, cfg: rewrite.Config, state: dict, src: str, files: list[str],
                     aliases: list[str], stamp: str, base: rewrite.Baseline) -> Result:
    pre = rewrite._prefix(project)
    golden = project.rel(project.golden_path)
    keep = {pre + f for f in files} | {pre + golden}
    originals = {f: (project.root / f).read_text(encoding="utf-8") for f in files}
    golden_before = project.golden_path.read_text(encoding="utf-8") if project.golden_path.exists() else ""
    tools = _tools_in(project, files)
    slug = src.replace("/", "_").removesuffix(".md")
    errors: list[str] = []
    last = Result(rewrite.Outcome("failed"))
    for alias in aliases:
        res = rewrite.run_claude(cfg, alias, build_prompt(project, src, files, errors),
                                 cfg.logs / f"{stamp}-facts-{slug}-{alias}.jsonl",
                                 allowed=rewrite.writer_tools(sorted(keep)))
        if res.limited or res.auth_error:
            rewrite.restore(cfg.cwd)
            return Result(rewrite.Outcome("limited" if res.limited else "auth", problems=[res.text],
                                          pause_until=res.pause_until, pause_reason=res.pause_reason))
        if res.is_error:
            rewrite.restore(cfg.cwd)
            errors = [f"the previous run ended with an error: {res.text[:300]}"]
            last = Result(rewrite.Outcome("failed", problems=errors))
            continue
        model_id = res.model_id or alias
        for note in rewrite.revert_others(cfg.cwd, keep):
            log(f"  {src}: {note}")
        problems, changes, touched, total = [], [], 0, 0
        for f in files:  # a file the writer emptied is one whose last fact went away: remove it
            path = project.root / f
            data = _yaml(path.read_text(encoding="utf-8")) if path.exists() else None
            if path.exists() and (not path.read_text(encoding="utf-8").strip()
                                  or (isinstance(data, dict) and not data.get("rows"))):
                path.unlink()
        for f in files:
            path = project.root / f
            d = diff_facts(project, f, originals[f], path.read_text(encoding="utf-8") if path.exists() else "", src)
            problems += d.problems
            changes += d.changed
            touched, total = touched + d.touched, total + d.rows_before
        golden_changes, golden_problems = diff_golden(
            project, golden_before, project.golden_path.read_text(encoding="utf-8") if project.golden_path.exists()
            else "", tools, src)
        problems += golden_problems
        changes += golden_changes
        # Every exact value left in these files must be in its source, changed or not: a price the
        # writer left alone is just as wrong if the source no longer says it.
        problems += [p for p in grounding.problems(project) if p.startswith(tuple(f"{f}:" for f in files))
                     and p not in problems]
        big = touched > 3 and touched > max_change_share() * max(total, 1)
        if big and not (cfg.check_model and cfg.escalation):
            problems.append(f"{touched} of {total} rows changed at once (over {max_change_share():.0%}), and a change "
                            f"that large needs two checking models (GT_REWRITE_CHECK_MODEL and "
                            f"GT_REWRITE_ESCALATION_MODEL)")
        if not problems:
            problems = rewrite.gate_problems(project, base)
            rewrite.revert_others(cfg.cwd, keep)
        checked_by = ""
        if not problems and cfg.check_model:
            check = rewrite.run_claude(cfg, cfg.check_model, build_check_prompt(project, src, files, changes),
                                       cfg.logs / f"{stamp}-facts-{slug}-check.jsonl",
                                       allowed=rewrite.CHECK_TOOLS, denied=rewrite.CHECK_DENIED)
            if check.limited or check.auth_error:
                rewrite.restore(cfg.cwd)
                return Result(rewrite.Outcome("limited" if check.limited else "auth", problems=[check.text],
                                              pause_until=check.pause_until, pause_reason=check.pause_reason))
            verdict, found = rewrite.parse_verdict(check.text) if not check.is_error else (None, [])
            problems = found if verdict else [f"the fact check gave no clear verdict: {check.text.strip()[:200]}"]
            checked_by = check.model_id or cfg.check_model
            if not problems and big:  # a redesigned source, or a broken one: a second, stronger reader decides
                second = rewrite.run_claude(cfg, cfg.escalation, build_check_prompt(project, src, files, changes),
                                            cfg.logs / f"{stamp}-facts-{slug}-check2.jsonl",
                                            allowed=rewrite.CHECK_TOOLS, denied=rewrite.CHECK_DENIED)
                if second.limited or second.auth_error:
                    rewrite.restore(cfg.cwd)
                    return Result(rewrite.Outcome("limited" if second.limited else "auth", problems=[second.text],
                                                  pause_until=second.pause_until, pause_reason=second.pause_reason))
                verdict, found = rewrite.parse_verdict(second.text) if not second.is_error else (None, [])
                problems = found if verdict else [f"the second fact check gave no clear verdict: {second.text[:200]}"]
                checked_by = f"{checked_by} and {second.model_id or cfg.escalation} (a large change: {touched} of " \
                             f"{total} rows)"
        if not problems:
            summary = next((line.strip() for line in res.text.splitlines() if line.strip()), "")
            return Result(rewrite.Outcome("ok", model_id=model_id, summary=summary[:200], checked_by=checked_by,
                                          pause_until=res.pause_until, pause_reason=res.pause_reason), changes)
        log(f"  {src}: {alias} attempt failed: {problems[0][:200]}")
        rewrite.restore(cfg.cwd)
        errors = problems
        last = Result(rewrite.Outcome("failed", model_id=model_id, problems=problems))
    return last


def commit(project: Project, cfg: rewrite.Config, src: str, files: list[str], result: Result,
           settled: list[str]) -> None:
    pre = rewrite._prefix(project)
    paths = [pre + f for f in files] + [pre + project.rel(project.golden_path)]
    if settled:
        paths.append(pre + "sources.lock.yaml")
        if host.git(cfg.cwd, "ls-files", "--", pre + "sources/.history").stdout.strip() or \
                (project.sources_dir / ".history").exists():
            paths.append(pre + "sources/.history")
    o = result.outcome
    body = [f"{src} changed. What changed in the facts:", "", *(result.changes or ["(nothing: the facts already "
            "matched the source)"]), "", "Every exact value above appears in its cited source, where it describes its row (checked by script).",
            f"Model: {o.model_id} (Claude Code, headless)"]
    if o.checked_by:
        body.append(f"Rows checked by: {o.checked_by}; each one is supported by the source")
    if settled:
        body.append(f"Recorded as reviewed: {', '.join(settled)} (everything citing it has been checked)")
    tracked = host.git(cfg.cwd, "ls-files", "--", *paths).stdout.split()
    host.git(cfg.cwd, "add", "-A", "--", *[p for p in paths if (cfg.cwd / p).exists() or p in tracked
                                           or p.endswith(".history")])
    if host.git(cfg.cwd, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return  # nothing to commit: the facts already matched and no review was recorded
    names = ", ".join(Path(f).name for f in files)
    host.git(cfg.cwd, "commit", "--quiet", "-m", f"{COMMIT_PREFIX} {names} from {src}", "-m", "\n".join(body))


def run(project: Project, cfg: rewrite.Config, state: dict, aliases: list[str], stamp: str,
        base: rewrite.Baseline, *, dry_run: bool, budget: int) -> tuple[int, rewrite.Outcome | None, rewrite.Baseline]:
    """Reconcile up to ``budget`` sources. Returns (sources handled, the outcome that stopped the run, baseline)."""
    queue = todo(project, state)
    failed = state.setdefault("facts_failed", {})
    now = dt.datetime.now().timestamp()
    hashes = review.current(project)
    escalate = rewrite.sort_failed(failed, now, lambda s, info: info.get("hash") == hashes.get(s))
    handled = 0
    for src, files in queue.items():
        if handled >= budget:
            break
        if src in failed and src not in escalate:
            continue
        handled += 1
        result = reconcile_source(project, cfg, state, src, files,
                                  rewrite.aliases_for(cfg, state, src, escalate, aliases), stamp, base)
        o = result.outcome
        if o.status == "ok":
            if dry_run:
                log(f"  {src}: facts ok on {o.model_id} (dry run): " + ("; ".join(c.splitlines()[0] for c in
                                                                                    result.changes) or "no change"))
                rewrite.restore(cfg.cwd)
            else:
                if o.checked_by:  # without the second model's check, nothing here counts as a review
                    for f in files:
                        rewrite.remember(project, state, "facts_verified", f, {src: review.current(project).get(src)})
                settled = rewrite.settle_sources(project, state, [src])
                commit(project, cfg, src, files, result, settled)
                base = rewrite.baseline(project)
                log(f"  {src}: facts updated on {o.model_id} ({len(result.changes)} change(s))"
                    + (f"; recorded {', '.join(settled)} as reviewed" if settled else ""))
                failed.pop(src, None)
                alerts.resolve(project, f"facts would not update — {', '.join(files)}")
        elif o.status == "failed" and not dry_run:
            reason = (o.problems or ["unknown"])[0][:500]
            if rewrite.record_failure(cfg, failed, src, {"hash": hashes.get(src), "reason": reason}, src in escalate):
                log(f"  {src}: facts gave up for {rewrite.FAILED_PAGE_RETRY_DAYS} days: {reason[:200]}")
                alerts.notify(project, "facts would not update",
                              f"{', '.join(files)} couldn't be updated from {src} on any model ({reason[:200]}). "
                              f"Until they are, ./gt check stays red and the server keeps the last version that "
                              f"passed. {rewrite.RETRY_NOTE}")
            else:
                log(f"  {src}: facts failed; {cfg.escalation} gets one more try in a few hours: {reason[:200]}")
        elif o.status == "failed":
            log(f"  {src}: facts failed on every model (dry run): {(o.problems or ['unknown'])[0][:200]}")
        if o.status in {"limited", "auth"} or o.pause_until:
            return handled, o, base
    return handled, None, base
