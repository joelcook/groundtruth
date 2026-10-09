"""The eval harness: turns "the bot seems fine" into a number you can defend.

``evals/golden.yaml`` lists questions and the behavior each must produce. Four
categories, four failure modes:

    hard_fact      the answer must come from the RIGHT TOOL. A right-looking date
                   that came from the model's memory still fails.
    narrative      the right wiki page must be found (and, live, a judge grades
                   the answer against a rubric).
    out_of_scope   personal/account questions must be refused without wasting a
                   tool call.
    contradiction  an old source and a current one disagree; the answer must use
                   the current one and never repeat the retired fact.

Tiers:

    tools     (default, free) No model. Calls each item's tool with its
              ``tool_args`` and checks the answer is really in your data, and
              checks search_wiki finds each item's ``must_cite_page``. Catches
              data and retrieval regressions. This is the ship gate in CI.
    replay    (free) Re-grade the latest recorded live run.
    live      Run every question through Claude with the real tools. Needs
              ANTHROPIC_API_KEY. Saves a run you can replay later.
    release   live, whole set, 100% required.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

from groundtruth import checks, server
from groundtruth.project import Project

TIERS = ("tools", "replay", "live", "release")
LIVE_TIERS = ("live", "release")


# ===================================================================== matching helpers


NUMBER_RE = re.compile(r"(?<![\d:.\w])\d[\d,]*(?:\.\d+)?(?![\d:])")


def _as_number(text: str) -> float | None:
    cleaned = str(text).strip().replace("$", "").replace(",", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    return float(cleaned)


def numbers_in(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in NUMBER_RE.findall(text)]


def contains(haystack: str, needle: str) -> bool:
    """Does ``haystack`` contain ``needle``?

    A plain number ("29", "$0.10") is compared by VALUE against every number in
    the text, so "0.10" matches 0.1 and "9" does NOT match 49. Anything else is
    a case-insensitive substring match.
    """
    number = _as_number(needle)
    if number is not None:
        return any(abs(n - number) < 1e-9 for n in numbers_in(haystack))
    return str(needle).lower() in haystack.lower()


def _field_equals(actual, expected) -> bool:
    a, e = _as_number(str(actual)), _as_number(str(expected))
    if a is not None and e is not None:
        return abs(a - e) < 1e-9
    if isinstance(expected, bool) or isinstance(actual, bool):
        return str(actual).lower() == str(expected).lower()
    return str(actual).strip().lower() == str(expected).strip().lower()


def match_rows(rows: list[dict], expected: dict) -> bool:
    """True when some row has every field in ``expected`` with that value."""
    return any(all(_field_equals(r.get(k), v) for k, v in expected.items()) for r in rows)


def _norm_page(value: str) -> str:
    return (value or "").strip().lower().removeprefix("wiki/").removesuffix(".md").replace("_", "-")


def page_matches(expected: str, actual: str) -> bool:
    e, a = _norm_page(expected), _norm_page(actual)
    return e == a or e.rsplit("/", 1)[-1] == a.rsplit("/", 1)[-1]


def _result(item: dict, passed: bool, reasons: list[str], **extra) -> dict:
    return {"id": item.get("id"), "category": item.get("category"), "passed": passed, "reasons": reasons, **extra}


# ===================================================================== tools tier (no model)


def _retrieval_check(project: Project, item: dict) -> list[str]:
    page = item.get("must_cite_page")
    if not page:
        return []
    hits = server.call_tool(project, "search_wiki", {"query": item["question"], "limit": 5}, log=False)
    paths = [h["path"] for h in hits]
    if not any(page_matches(page, p) for p in paths):
        return [f"search_wiki doesn't surface {page!r} for this question "
                f"(top: {[_norm_page(p) for p in paths[:3]] or 'nothing'}). Add synonyms or improve the page title"]
    return []


def run_tools_tier(project: Project, items: list[dict]) -> list[dict]:
    results = []
    for item in items:
        cat = item.get("category")
        reasons: list[str] = []
        payload_text = ""
        if cat == "out_of_scope":
            results.append(_result(item, True, ["skipped: needs a model (run --tier live)"], skipped=True))
            continue
        tool = item.get("must_call_tool")
        if tool:
            args = dict(item.get("tool_args") or {})
            try:
                payload = server.call_tool(project, tool, args, log=False)
                payload_text = json.dumps(payload, default=str)
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"{tool}({args}) raised {type(exc).__name__}: {exc}")
            else:
                if isinstance(payload, dict) and payload.get("count") == 0:
                    reasons.append(f"{tool}({args}) returned nothing. {payload.get('hint') or ''}".strip())
                for needle in item.get("must_contain", []) or []:
                    if not contains(payload_text, str(needle)):
                        reasons.append(f"{tool}({args}) result doesn't contain {needle!r}")
                expected = item.get("must_match")
                if expected and isinstance(payload, dict):
                    if not match_rows(payload.get("results") or [], expected):
                        got = [{k: r.get(k) for k in expected} for r in (payload.get("results") or [])[:3]]
                        reasons.append(f"{tool}({args}) has no row with {expected} (got {got})")
                for needle in item.get("must_not_contain", []) or []:
                    if str(needle).lower() in payload_text.lower():
                        reasons.append(f"{tool}({args}) result contains the stale value {needle!r}")
        reasons += _retrieval_check(project, item)
        if cat == "narrative" and item.get("must_cite_page") and not tool:
            page = server.call_tool(project, "read_wiki_page", {"path_or_slug": item["must_cite_page"]}, log=False)
            content = (page or {}).get("content", "")
            if not content:
                reasons.append(f"page {item['must_cite_page']!r} can't be read")
            for needle in item.get("must_contain", []) or []:
                if content and not contains(content, str(needle)):
                    reasons.append(f"page {item['must_cite_page']!r} never mentions {needle!r}")
        if cat == "hard_fact" and not tool:
            reasons.append("hard_fact items need must_call_tool")
        results.append(_result(item, not reasons, reasons, tool_calls=[{"name": tool, "input": item.get("tool_args") or {}}] if tool else []))
    return results


# ===================================================================== graders (live + replay)


def _grade_text(item: dict, text: str) -> list[str]:
    reasons = []
    for needle in item.get("must_contain", []) or []:
        if not contains(text, str(needle)):
            reasons.append(f"answer is missing {needle!r}")
    for needle in item.get("must_not_contain", []) or []:
        if str(needle).lower() in text.lower():
            reasons.append(f"answer contains {needle!r}")
    return reasons


def _grade_calls(item: dict, calls: list[dict]) -> list[str]:
    reasons = []
    called = [c["name"] for c in calls]
    want = item.get("must_call_tool")
    if want and want not in called:
        reasons.append(f"didn't call {want} (called: {called or 'nothing'})")
    forbidden = item.get("must_not_call_tool")
    if forbidden in ("any", "*") and called:
        reasons.append(f"should not call any tool, but called {called}")
    elif forbidden and forbidden not in ("any", "*"):
        names = [forbidden] if isinstance(forbidden, str) else list(forbidden)
        hit = [n for n in names if n in called]
        if hit:
            reasons.append(f"called forbidden tool(s) {hit}")
    page = item.get("must_cite_page")
    if page:
        reads = [str(c["input"].get("path_or_slug", "")) for c in calls if c["name"] == "read_wiki_page"]
        if not any(page_matches(page, r) for r in reads):
            reasons.append(f"didn't read {page!r} (read: {[_norm_page(r) for r in reads] or 'nothing'})")
    return reasons


def grade(item: dict, response: str, calls: list[dict], *, use_judge: bool) -> tuple[bool, list[str]]:
    reasons = _grade_calls(item, calls) + _grade_text(item, response)
    if item.get("category") == "narrative" and use_judge and not reasons and item.get("expected_summary"):
        from groundtruth import llm
        ok, why = llm.judge(item["question"], item["expected_summary"], response)
        if not ok:
            reasons.append(f"judge: {why}")
    return (not reasons, reasons)


# ===================================================================== live + replay


def system_prompt(project: Project) -> str:
    """Exactly what MCP clients get (see server.instructions)."""
    return server.instructions(project)


def run_live(project: Project, items: list[dict], *, use_judge: bool) -> list[dict]:
    from groundtruth import llm

    tools = server.tool_specs(project)
    system = system_prompt(project)
    results = []
    for item in items:
        print(f"  [{item['id']}] {item['question'][:70]}", file=sys.stderr)
        try:
            out = llm.agent(item["question"], system, tools,
                            lambda name, args: server.call_tool(project, name, args, log=False))
        except Exception as exc:  # noqa: BLE001
            results.append(_result(item, False, [f"agent failed: {exc}"], tool_calls=[], response=""))
            continue
        passed, reasons = grade(item, out["response"], out["tool_calls"], use_judge=use_judge)
        results.append(_result(item, passed, reasons, tool_calls=out["tool_calls"], response=out["response"]))
    return results


def latest_live_run(project: Project) -> Path | None:
    if not project.runs_dir.exists():
        return None
    runs = sorted(p for p in project.runs_dir.glob("*.json") if not p.stem.endswith(("-tools", "-replay")))
    return runs[-1] if runs else None


def run_replay(project: Project, items: list[dict]) -> tuple[list[dict], Path | None]:
    source = latest_live_run(project)
    if source is None:
        return [_result(i, False, ["no recorded live run to replay; run --tier live once"]) for i in items], None
    rows = {r["id"]: r for r in json.loads(source.read_text())["results"]}
    results = []
    for item in items:
        row = rows.get(item["id"])
        if row is None:
            results.append(_result(item, True, ["skipped: added after the recorded run (run --tier live)"],
                                   skipped=True))
            continue
        passed, reasons = grade(item, row.get("response", ""), row.get("tool_calls", []), use_judge=False)
        results.append(_result(item, passed, reasons, tool_calls=row.get("tool_calls", []),
                               response=row.get("response", "")))
    return results, source


# ===================================================================== runner


def run(project: Project, tier: str = "tools", *, only: str | None = None, judge: bool = True,
        quiet: bool = False) -> int:
    if tier == "fast":
        tier = "replay"
    if tier not in TIERS:
        print(f"unknown tier {tier!r}; use one of {TIERS}", file=sys.stderr)
        return 2
    items = checks.load_golden(project)
    if not items:
        print(f"no questions in {project.rel(project.golden_path)}", file=sys.stderr)
        return 2
    if only:
        if tier == "release":
            print("release runs the whole set; drop --only", file=sys.stderr)
            return 2
        items = [i for i in items if i.get("id") == only]
        if not items:
            print(f"no question with id {only!r}", file=sys.stderr)
            return 2

    from groundtruth import facts
    try:
        facts.load(project)
    except facts.FactsError as exc:
        print(f"your facts have {len(exc.problems)} problem(s); run ./gt check and fix them first:")
        for p in exc.problems[:10]:
            print(f"  ✗ {p}")
        return 1

    source = None
    previous = None
    if tier == "tools":
        results = run_tools_tier(project, items)
    elif tier == "replay":
        results, source = run_replay(project, items)
    else:
        from groundtruth import llm
        if not llm.available():
            print("live evals need Claude: set ANTHROPIC_API_KEY (console.anthropic.com) or run "
                  "`ant auth login`. The free `./gt eval` (tools tier) needs nothing.", file=sys.stderr)
            return 2
        prev_path = latest_live_run(project)
        if prev_path:
            previous = {r["id"]: r["passed"] for r in json.loads(prev_path.read_text())["results"]}
        print(f"running {len(items)} question(s) on {llm.model()}", file=sys.stderr)
        results = run_live(project, items, use_judge=judge)

    skipped = [r for r in results if r.get("skipped")]
    graded = [r for r in results if not r.get("skipped")]
    passed = sum(1 for r in graded if r["passed"])
    regressions = [r["id"] for r in graded if previous and previous.get(r["id"]) and not r["passed"]]

    project.runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    suffix = "" if tier in LIVE_TIERS else f"-{tier}"
    artifact = project.runs_dir / f"{stamp}{suffix}.json"
    meta: dict[str, Any] = {"tier": tier, "passed": passed, "total": len(graded), "skipped": len(skipped),
                            "regressions": regressions, "replayed_from": project.rel(source) if source else None}
    if tier in LIVE_TIERS:
        from groundtruth import llm
        meta |= {"model": llm.model(), "judge_model": llm.judge_model() if judge else None}
    artifact.write_text(json.dumps({**meta, "results": results}, indent=2, default=str))

    if not quiet:
        for r in graded:
            if not r["passed"]:
                print(f"  ✗ {r['id']}: {'; '.join(r['reasons'])}")
        why = "not in the recorded run" if tier == "replay" else "need a model"
        extra = f" ({len(skipped)} skipped: {why}, run --tier live)" if skipped else ""
        print(f"{tier}: {passed}/{len(graded)} passed{extra}")
        if regressions:
            print(f"  REGRESSIONS since the last live run: {', '.join(regressions)}")
        if tier == "replay" and source:
            print(f"  replayed from {project.rel(source)}")
    return 0 if passed == len(graded) else 1
