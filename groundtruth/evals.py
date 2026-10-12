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

Your production model isn't Claude? ``./gt eval --export copilot-studio`` writes
the same questions as a test set Copilot Studio imports, so you grade the model
your users actually talk to.
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
                    if contains(payload_text, str(needle)):
                        reasons.append(f"{tool}({args}) result contains the stale value {needle!r}")
        data_ok = not reasons if tool else None
        retrieval = _retrieval_check(project, item)
        retrieval_ok = not retrieval if item.get("must_cite_page") else None
        reasons += retrieval
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
        results.append(_result(item, not reasons, reasons, data_ok=data_ok, retrieval_ok=retrieval_ok,
                               tool_calls=[{"name": tool, "input": item.get("tool_args") or {}}] if tool else []))
    return results


def _breakdown(results: list[dict]) -> list[str]:
    """What the free tier actually proved, and what it can't."""
    def line(key: str, label: str, what: str) -> str | None:
        rs = [r for r in results if r.get(key) is not None]
        score = f"{sum(1 for r in rs if r[key])}/{len(rs)}"
        return f"  {label:<10} {score:<6} {what}" if rs else None
    out = [line("data_ok", "data", "fact tools return the expected values for the recorded tool_args"),
           line("retrieval_ok", "retrieval", "search_wiki finds the expected page for the question")]
    return [x for x in out if x] + ["  not tested: whether a model picks that tool and those arguments, "
                                    "and answers faithfully (--tier live)"]


# ===================================================================== graders (live + replay)


def _grade_text(item: dict, text: str) -> list[str]:
    reasons = []
    for needle in item.get("must_contain", []) or []:
        if not contains(text, str(needle)):
            reasons.append(f"answer is missing {needle!r}")
    for needle in item.get("must_not_contain", []) or []:
        if contains(text, str(needle)):  # by value for numbers: "9" doesn't hit "49"
            reasons.append(f"answer contains {needle!r}")
    expected = item.get("must_match")
    if isinstance(expected, dict):  # the answer has to state the exact values, not just call the tool
        for field, value in expected.items():
            if _as_number(str(value)) is not None and not contains(text, str(value)):
                reasons.append(f"answer doesn't state {field} {value!r}")
    return reasons


def _grade_calls(item: dict, calls: list[dict]) -> list[str]:
    reasons = []
    called = [c["name"] for c in calls]
    want = item.get("must_call_tool")
    if want and want not in called:
        reasons.append(f"didn't call {want} (called: {called or 'nothing'})")
    results = [c["result"] for c in calls if c["name"] == want and isinstance(c.get("result"), dict)]
    if want in called and results:  # recorded runs from before results were kept skip this
        if not any(r.get("count") for r in results):
            reasons.append(f"{want} found nothing for the arguments the model used "
                           f"({[c['input'] for c in calls if c['name'] == want]})")
        expected = item.get("must_match")
        if isinstance(expected, dict) and not any(match_rows(r.get("results") or [], expected) for r in results):
            reasons.append(f"no {want} call returned a row with {expected}")
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


EXACT_KINDS = {"money", "date", "time", "phone number", "email", "duration", "percentage", "limit", "size limit",
               "amount"}


# What each kind of claim has to match in the tool results: a dollar amount a money field, a duration a
# duration field, a percentage a rate. So a returned `duration_days: 30` doesn't back up an invented "$30".
MONEY_FIELD = re.compile(r"usd|eur|gbp|cad|aud|price|cost|fee|amount|charge|tuition|dollar|fine", re.I)
DURATION_FIELD = re.compile(r"day|week|month|year|hour|minute|duration|window|period|length|term", re.I)
PERCENT_FIELD = re.compile(r"pct|percent|rate|share", re.I)
NOT_EVIDENCE = {"query", "filters", "hint", "error", "score", "backend", "count", "collection", "truncated",
                "ambiguous"}  # echoes of the question, or bookkeeping: never proof of a fact


def _leaves(obj, key: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) not in NOT_EVIDENCE:
                yield from _leaves(v, str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v, key)
    elif obj is not None and not isinstance(obj, bool):
        yield key, obj


class Evidence:
    """What the tools actually returned, typed: the values of the rows and pages, not the echo of the query."""

    def __init__(self, results: list) -> None:
        from groundtruth import grounding
        leaves = [leaf for r in results for leaf in _leaves(r)]
        self.text = "\n".join(str(v) for _, v in leaves)
        self.index = grounding.SourceIndex.of(self.text)
        self.money, self.durations, self.percents = set(), set(), set()
        for key, value in leaves:
            nums = [n for k, n, _ in grounding.exact_values(value) if k == "number"]
            if isinstance(value, (int, float)):
                if MONEY_FIELD.search(key):
                    self.money.update(nums)
                if DURATION_FIELD.search(key):
                    self.durations.update(nums)
                if PERCENT_FIELD.search(key):
                    self.percents.update(nums)
            else:  # prose and statements: the unit is written next to the number
                text = str(value)
                for rx, bucket in ((checks.MONEY_RE, self.money), (checks.DURATION_RE, self.durations),
                                   (checks.PERCENT_RE, self.percents), (checks.PERCENT_WORDS_RE, self.percents)):
                    for hit in rx.finditer(text):
                        bucket.update(n for k, n, _ in grounding.exact_values(hit.group(0)) if k == "number")

    def backs(self, kind: str, raw: str) -> bool:
        from groundtruth import grounding
        values = grounding.exact_values(raw)
        nums = [n for k, n, _ in values if k == "number"]
        if not values:  # "seven days", "at noon": compare the words themselves
            return " ".join(raw.lower().split()) in " ".join(self.text.lower().split())
        if kind == "money":
            return all(n in self.money for n in nums)
        if kind == "duration":
            return all(n in self.durations for n in nums) if nums else self.backs("text", raw)
        if kind == "percentage":
            return all(n in self.percents for n in nums)
        return all(self.index.has(k, n) for k, n, _ in values)


def invented_values(question: str, response: str, calls: list[dict], *, question_counts: bool = False) -> list[str]:
    """Exact values in an answer that no tool returned: said from memory.

    ``question_counts``: a value the question itself contains may be repeated (to correct a rumor, say). Off
    for anything a visitor typed, so nobody can talk the assistant into confirming a number they made up.
    Only for runs that recorded tool results (older recorded runs can't be judged this way).
    """
    if not any("result" in c for c in calls):
        return [] if calls else [v for _, v in _exact_in(response) if not (question_counts and v in question)]
    evidence = Evidence([c.get("result") for c in calls if "result" in c])
    return [raw for kind, raw in _exact_in(response)
            if not evidence.backs(kind, raw) and not (question_counts and raw in question)]


def _exact_in(text: str) -> list[tuple[str, str]]:
    """(kind, as written) for every exact value in a piece of text."""
    found: list[tuple[str, str]] = []
    for kind, rx in checks.PATTERNS:
        if kind in EXACT_KINDS:
            found += [(kind, m.group(0)) for m in rx.finditer(text) if m.group(0) not in [f for _, f in found]]
    return found


def grade(item: dict, response: str, calls: list[dict], *, use_judge: bool) -> tuple[bool, list[str]]:
    reasons = _grade_calls(item, calls) + _grade_text(item, response)
    reasons += [f"answer states {v!r}, which no tool returned (from memory?)"
                for v in invented_values(str(item.get("question") or ""), response, calls,
                                         question_counts=item.get("category") == "contradiction")]
    if item.get("category") == "narrative" and use_judge and not reasons and item.get("expected_summary"):
        from groundtruth import llm
        evidence = "\n\n".join(f"{c['name']}({json.dumps(c.get('input'), default=str)}):\n"
                               f"{json.dumps(c['result'], default=str)[:6000]}" for c in calls if "result" in c)
        ok, why = llm.judge(item["question"], item["expected_summary"], response, evidence=evidence[:30000])
        if not ok:
            reasons.append(f"judge: {why}")
    return (not reasons, reasons)


# ===================================================================== live + replay


def system_prompt(project: Project) -> str:
    """Exactly what MCP clients get (see server.instructions)."""
    return server.instructions(project)


def run_live(project: Project, items: list[dict], *, use_judge: bool) -> list[dict]:
    from groundtruth import claude_code, llm

    tools = server.tool_specs(project)
    system = system_prompt(project)
    via_api = llm.available()
    results = []
    for item in items:
        print(f"  [{item['id']}] {item['question'][:70]}", file=sys.stderr)
        try:
            if via_api:
                out = llm.agent(item["question"], system, tools,
                                lambda name, args: server.call_tool(project, name, args, log=False))
            else:  # a Claude plan, no API key: the same questions through Claude Code and the MCP server
                out = claude_code.agent(project, item["question"], system, model=llm.model())
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


# ===================================================================== grading another platform's model

COPILOT_COLUMNS = ["Question", "Expected response", "Testing method"]
COPILOT_MAX_ROWS = 100
COPILOT_MAX_QUESTION = 1000
COMPARE = "Compare meaning"


def expected_response(item: dict) -> str:
    """What a correct answer does, in words a platform's judge can grade.

    ``expected_response`` in the question wins. Otherwise it's built from the
    question's own checks. Exact values stay in it on purpose: the golden set
    already pins them, and regenerating the export keeps the two in step.
    """
    if item.get("expected_response"):
        return " ".join(str(item["expected_response"]).split())
    base = {
        "hard_fact": "Answers with the organization's current information, not a guess, and says where it came from.",
        "out_of_scope": ("Declines, because the assistant can't see personal or account information, and points "
                         "the person to where they can get it."),
    }.get(item.get("category"), "Answers from the organization's published information and says where it came from.")
    text = " ".join(str(item.get("expected_summary") or "").split()) or base
    must = [json.dumps(str(x)) for x in item.get("must_contain") or []]
    never = [json.dumps(str(x)) for x in item.get("must_not_contain") or []]
    if must:
        text += f" The answer includes {', '.join(must)}."
    if never:
        text += f" It does NOT say {', '.join(never)}."
    return text


def export_copilot_studio(project: Project, out: Path | None = None) -> tuple[Path, int]:
    """Write the golden set as a Copilot Studio test set: Question, Expected response, Testing method.

    Copilot Studio can't import tool-use assertions, so every row is graded by
    meaning against what a correct answer does. Its limits: 100 rows, 1000
    characters per question.
    """
    import csv
    items, problem = checks.read_golden(project)
    if problem:
        raise ValueError(problem)
    if len(items) > COPILOT_MAX_ROWS:
        raise ValueError(f"{len(items)} questions; Copilot Studio imports at most {COPILOT_MAX_ROWS} per test set")
    rows = []
    for item in items:
        question = " ".join(str(item.get("question") or "").split())
        if len(question) > COPILOT_MAX_QUESTION:
            raise ValueError(f"{item.get('id')}: the question is over {COPILOT_MAX_QUESTION} characters")
        rows.append({"Question": question, "Expected response": expected_response(item), "Testing method": COMPARE})
    out = out or project.build_dir / "copilot-studio-testset.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COPILOT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return out, len(rows)


def run(project: Project, tier: str = "tools", *, only: str | None = None, judge: bool = True,
        quiet: bool = False) -> int:
    if tier == "fast":
        tier = "replay"
    if tier not in TIERS:
        print(f"unknown tier {tier!r}; use one of {TIERS}", file=sys.stderr)
        return 2
    items, problem = checks.read_golden(project)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if not items:
        print(f"no questions in {project.rel(project.golden_path)}", file=sys.stderr)
        return 2
    if only:
        if tier == "release":
            print("release runs the whole set; drop --only", file=sys.stderr)
            return 2
        wanted = {x.strip() for x in str(only).split(",") if x.strip()}  # one id, or several: a,b,c
        items = [i for i in items if str(i.get("id")) in wanted]
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
        from groundtruth import claude_code, llm
        if not llm.available() and not claude_code.available():
            print("live evals need Claude: set ANTHROPIC_API_KEY (console.anthropic.com), or have the claude CLI "
                  "logged in (CLAUDE_CODE_OAUTH_TOKEN). The free `./gt eval` (tools tier) needs nothing.",
                  file=sys.stderr)
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
        if tier == "tools":
            print("\n".join(_breakdown(graded)))
        if regressions:
            print(f"  REGRESSIONS since the last live run: {', '.join(regressions)}")
        if tier == "replay" and source:
            print(f"  replayed from {project.rel(source)}")
    return 0 if passed == len(graded) else 1
