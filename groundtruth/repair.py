"""``./gt repair``: when the live eval says the assistant got a question wrong, fix the cause.

A failing golden question means the content led the model astray: a page that
doesn't match how people ask, a missing search synonym, a vague sentence, a
prompt rule, a lookup alias. This is CLAUDE.md's "the assistant got X wrong" job,
done unattended. Each question that failed in the latest live run (up to
GT_REPAIR_MAX, default 2) goes to headless Claude Code on the escalation model
(GT_REWRITE_ESCALATION_MODEL, default opus), along with what the assistant said
and which tools it called. Claude Code may change only what can't make a fact
wrong:

  * search synonyms in groundtruth.yaml;
  * the prompt (keeping {{FACT_TOOLS}});
  * existing wiki pages, each checked against its sources like any rewrite;
  * lookup aliases, each a word the row's source uses.

Never a fact value, the golden set, or the evidence. The fix ships only if the
gates pass, that question now passes live, and the whole live eval still passes
everything it passed before. It's committed as ``fix: <question> ...``. The
autopilot runs this after a nightly live eval that fails without having gotten
worse.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import yaml

from groundtruth import alerts, checks, evals, facts, grounding, host, reconcile, rewrite
from groundtruth.project import Project

COMMIT_PREFIX = "fix:"
log = host.log


def failing(project: Project) -> tuple[list[dict], set[str]]:
    """(the failed results, the ids that passed) from the latest live run."""
    latest = evals.latest_live_run(project)
    if latest is None:
        return [], set()
    results = json.loads(latest.read_text()).get("results") or []
    graded = [r for r in results if not r.get("skipped")]
    wrong = [r for r in graded if not r.get("passed") and not host._agent_failed(r)]  # unanswered isn't wrong
    return wrong, {str(r["id"]) for r in graded if r.get("passed")}


def build_prompt(project: Project, item: dict, result: dict, errors: list[str]) -> str:
    pre = rewrite._prefix(project)
    calls = [f"- {c['name']}({json.dumps(c.get('input'), default=str)}) -> "
             f"{json.dumps(c.get('result'), default=str)[:1500]}" for c in result.get("tool_calls") or []]
    parts = [
        "You maintain this repo's knowledge base without a human in the loop. The assistant got a golden",
        "question wrong in the live eval. Find out why and fix the cause.",
        "",
        "First read CLAUDE.md (\"The assistant got X wrong\"), docs/wiki.md, and docs/facts.md.",
        "",
        "The question, as evals/golden.yaml has it:",
        "```yaml",
        yaml.safe_dump(item, sort_keys=False, allow_unicode=True).strip(),
        "```",
        "Why it failed: " + "; ".join(result.get("reasons") or ["(no reason recorded)"]),
        "What the assistant answered: " + str(result.get("response") or "(nothing)")[:2000],
        "The tools it called:" if calls else "It called no tools.",
        *calls,
        "",
        "You may change only:",
        f"- `search.synonyms` in `{pre}groundtruth.yaml` (the words people use -> the words pages use);",
        f"- `{pre}prompts/system_prompt.md` (keep `{{{{FACT_TOOLS}}}}` in it);",
        f"- existing pages under `{pre}wiki/`: titles, tags, and wording, every claim still from the page's sources;",
        f"- `aliases` of rows in `{pre}facts/*.yaml`, each one a word the row's source uses.",
        "Never change a fact value, the golden set, or anything under sources/. No exact facts in page prose.",
        rewrite.UNTRUSTED,
        "",
        "Afterwards a script runs the gates, checks every changed page against its sources, and asks the",
        "assistant this question again live; then the whole live eval must still pass everything it passed.",
        "When you finish, reply with one sentence saying what you changed and why it fixes the answer.",
    ]
    if errors:
        parts += ["", "A previous attempt failed these checks. Fix them:", *[f"- {e}" for e in errors[:20]]]
    return "\n".join(parts)


def _validate(project: Project, before: dict[str, str]) -> tuple[list[str], list[str]]:
    """(problems, changed pages) for what the repair changed, beyond the gates."""
    problems, pages = [], []
    cfg_rel, prompt_rel = "groundtruth.yaml", project.rel(project.root / "prompts" / "system_prompt.md")
    for rel, old in before.items():
        path = project.root / rel
        new = path.read_text(encoding="utf-8") if path.exists() else ""
        if new == old:
            continue
        if rel == cfg_rel:
            a, b = yaml.safe_load(old) or {}, yaml.safe_load(new) or {}
            other = {k for k in set(a) | set(b) if k != "search" and a.get(k) != b.get(k)}
            moved = {k for k in set(a.get("search") or {}) | set(b.get("search") or {})
                     if (a.get("search") or {}).get(k) != (b.get("search") or {}).get(k)}
            if other or moved - {"synonyms"}:
                problems.append(f"groundtruth.yaml: only search.synonyms may change (changed {sorted(other | moved)})")
        elif rel == prompt_rel:
            if "{{FACT_TOOLS}}" in old and "{{FACT_TOOLS}}" not in new:
                problems.append("the prompt lost {{FACT_TOOLS}}")
        elif rel.startswith("facts/"):
            a, b = reconcile._yaml(old) or {}, reconcile._yaml(new) or {}
            key = str(b.get("key") or "id")
            olds = {str(r.get(key)): r for r in a.get("rows") or [] if isinstance(r, dict)}
            news = {str(r.get(key)): r for r in b.get("rows") or [] if isinstance(r, dict)}
            if {k: v for k, v in a.items() if k != "rows"} != {k: v for k, v in b.items() if k != "rows"} \
                    or olds.keys() != news.keys():
                problems.append(f"{rel}: only aliases may change")
                continue
            default = (b.get("source") or {}).get("path") if isinstance(b.get("source"), dict) else None
            cache: dict = {}
            for rid, row in news.items():
                prev = olds[rid]
                if {f for f in set(row) | set(prev) if row.get(f) != prev.get(f)} - {"aliases"}:
                    problems.append(f"{rel}: row {rid}: only aliases may change")
                    continue
                gained = {str(x) for x in row.get("aliases") or []} - {str(x) for x in prev.get("aliases") or []}
                srcs = grounding.row_sources(row, default)
                for alias in gained:
                    if not any((idx := grounding._load(project, s, cache)) and alias.lower() in idx.text for s in srcs):
                        problems.append(f"{rel}: row {rid}: the alias {alias!r} isn't a word its source uses")
        elif rel.startswith("wiki/"):
            if new.count("fact-ok") > old.count("fact-ok"):
                problems.append(f"{rel}: added a fact-ok marker")
            pages.append(rel)
    return problems, pages


def repair_question(project: Project, cfg: rewrite.Config, item: dict, result: dict, passed_before: set[str],
                    stamp: str, base: rewrite.Baseline) -> rewrite.Outcome:
    pre = rewrite._prefix(project)
    editable = ["groundtruth.yaml", project.rel(project.root / "prompts" / "system_prompt.md"),
                *(project.rel(p) for p in checks.wiki_pages(project) if not checks.is_draft(p)),
                *(project.rel(f) for f in facts.fact_files(project))]
    before = {rel: (project.root / rel).read_text(encoding="utf-8") for rel in editable if (project.root / rel).exists()}
    keep = {pre + rel for rel in before}
    model = cfg.escalation or cfg.fallback
    errors: list[str] = []
    from groundtruth import project as project_mod

    def undo() -> None:  # back to main as it was, and forget the eval runs made against what's undone
        rewrite.restore(cfg.cwd)
        for run_file in set(project.runs_dir.glob("*.json")) - runs_before:
            run_file.unlink()
        project.config = project_mod.load(project.root).config

    runs_before = set(project.runs_dir.glob("*.json")) if project.runs_dir.exists() else set()
    for attempt in (1, 2):
        res = rewrite.run_claude(cfg, model, build_prompt(project, item, result, errors),
                                 cfg.logs / f"{stamp}-repair-{item['id']}-{attempt}.jsonl",
                                 allowed=rewrite.writer_tools(sorted(keep)))
        if res.limited or res.auth_error:
            undo()
            return rewrite.Outcome("limited" if res.limited else "auth", problems=[res.text],
                                   pause_until=res.pause_until, pause_reason=res.pause_reason)
        rewrite.revert_others(cfg.cwd, keep)
        problems, pages = ([f"the run ended with an error: {res.text[:300]}"], []) if res.is_error \
            else _validate(project, before)
        if not problems:
            problems = rewrite.gate_problems(project, base)
            rewrite.revert_others(cfg.cwd, keep)
        for rel in pages if not problems and cfg.check_model else []:
            check, found = rewrite.check_claims(cfg, project, rel, cfg.logs / f"{stamp}-repair-{item['id']}-check.jsonl",
                                                before[rel])
            if check.limited or check.auth_error:
                undo()
                return rewrite.Outcome("limited" if check.limited else "auth", problems=[check.text],
                                       pause_until=check.pause_until, pause_reason=check.pause_reason)
            if found:
                problems = [f"{rel}: {p}" for p in found]
                break
        if not problems:  # the answers must come from what's on disk now, not a cache or a config read earlier
            from groundtruth import search
            facts.store(project).refresh(force=True)
            search._page_cache.clear()
            project.config = project_mod.load(project.root).config
        if not problems and evals.run(project, "live", only=str(item["id"]), quiet=True) != 0:
            problems = ["the assistant still gets this question wrong"]
        if not problems:
            evals.run(project, "live", quiet=True)
            _, passed_now = failing(project)
            lost = sorted(passed_before - passed_now)
            if lost:
                problems = [f"it broke questions that passed before: {', '.join(lost[:5])}"]
            elif str(item["id"]) not in passed_now:
                problems = ["the question passed on its own but fails in the full run"]
        if not problems:
            summary = next((line.strip() for line in res.text.splitlines() if line.strip()), "")
            return rewrite.Outcome("ok", model_id=res.model_id or model, summary=summary[:200])
        log(f"  {item['id']}: repair attempt {attempt} failed: {problems[0][:200]}")
        undo()
        errors = problems
    return rewrite.Outcome("failed", problems=errors)


def run(project: Project, *, max_items: int | None = None, live_first: bool = False) -> int:
    """Repair what the latest live run got wrong. ``live_first`` runs the live eval first (a machine host's
    nightly job); then whatever stays wrong is an alert, and a clean run clears it."""
    import os
    try:
        cfg = rewrite.config(project)
    except (rewrite.RewriteError, host.HostError) as exc:
        print(f"  ✗ {exc}")
        return 2
    if rewrite.changed_paths(cfg.cwd):
        print("  ✗ the checkout has uncommitted changes; commit or stash them first")
        return 2
    if live_first:
        before = evals.latest_live_run(project)
        evals.run(project, "live", quiet=True)
        latest = evals.latest_live_run(project)
        if latest is None or latest == before or not host.answered(json.loads(latest.read_text())):
            alerts.notify(project, "the live eval couldn't run",
                          "./gt eval --tier live got no answers from the model (is GT_MODEL right, and the Claude "
                          "credential valid with credit?). Nothing was repaired, and later rewrites wait until "
                          "it answers again.")
            return 1
        alerts.resolve(project, "the live eval couldn't run")
    failed, passed = failing(project)
    golden = {str(i.get("id")): i for i in checks.load_golden(project) if isinstance(i, dict)}
    latest = evals.latest_live_run(project)
    worse = set(json.loads(latest.read_text()).get("regressions") or []) if latest else set()
    failed = sorted(failed, key=lambda r: str(r.get("id")) not in worse)  # what just got worse comes first
    queue = [r for r in failed if str(r.get("id")) in golden][: max_items or int(os.environ.get("GT_REPAIR_MAX") or 2)]
    if not queue:
        log("nothing to repair: the latest live run has no failures")
        if live_first and latest is not None:
            host.accept_live(project)
            if not failed:
                alerts.resolve(project, "the live eval failed")
        return 0
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = rewrite.baseline(project)
    fixed = 0
    for result in queue:
        qid = str(result["id"])
        outcome = repair_question(project, cfg, golden[qid], result, passed, stamp, base)
        if outcome.status == "ok":
            pre = rewrite._prefix(project)
            host.git(cfg.cwd, "add", "-A", "--", *(pre + p for p in ("groundtruth.yaml", "prompts", "wiki", "facts")))
            host.git(cfg.cwd, "commit", "--quiet", "-m", f"{COMMIT_PREFIX} {qid}: {outcome.summary[:60]}",
                     "-m", f"The live eval got {qid!r} wrong ({'; '.join(result.get('reasons') or [])[:300]}).\n\n"
                           f"{outcome.summary}\n\nIt passes now, and so does everything that passed before.\n"
                           f"Model: {outcome.model_id} (Claude Code, headless)")
            _, passed = failing(project)
            base = rewrite.baseline(project)
            fixed += 1
            log(f"  {qid}: fixed on {outcome.model_id}")
        elif outcome.status in {"limited", "auth"}:
            break
        else:
            log(f"  {qid}: couldn't fix it: {(outcome.problems or ['unknown'])[0][:200]}")
    remaining, _ = failing(project)
    if live_first:  # a machine host's nightly run: what main answers now is what later rewrites are held to
        host.accept_live(project)
    if not remaining:
        alerts.resolve(project, "the live eval failed")
    elif live_first:
        alerts.notify(project, "the live eval failed",
                      f"the assistant answers {len(remaining)} golden question(s) wrong on the production model, and "
                      f"./gt repair couldn't fix them ({', '.join(str(r['id']) for r in remaining[:3])}). "
                      f"Run ./gt eval --tier live to see why.")
    return 0 if fixed or not remaining else 1
