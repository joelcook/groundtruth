"""The eval harness — the launch gate, and the most undervalued thing here.

It runs each question in ``evals/golden.yaml`` through Claude with the *exact*
tool surface the production MCP server exposes, then grades the result. The
trick that makes "don't make things up" enforceable: for hard facts it grades
the **tool call**, not the prose. A right-looking date that came from the model's
memory instead of ``get_event`` fails — because next month that memory is wrong.

Tiers:
    fast      replay the latest recorded run; deterministic checks only. No API
              key, no spend. This is what CI and the nightly agent run by default.
    full      live model + LLM judge. Needs ANTHROPIC_API_KEY.
    release   full, over the whole set, 100% required to ship.

Grading by category:
    hard_fact      must_call_tool (+ must_contain / must_not_contain)
    narrative      LLM judge vs expected_summary (offline: required page read)
    out_of_scope   must refuse; must_not_call_tool guards wasted calls
    contradiction  must cite the current source; must_not_contain the stale fact

Run:  python scripts/run_evals.py --tier fast
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import anyio
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from groundtruth import db, search  # noqa: E402

GOLDEN_PATH = REPO_ROOT / "evals" / "golden.yaml"
RUNS_DIR = REPO_ROOT / "evals" / "runs"
PROMPT_PATH = REPO_ROOT / "prompts" / "system_prompt.md"
NON_MODEL_ARTIFACT_SUFFIXES = ("-fast",)

DEFAULT_MODEL = os.environ.get("EVAL_MODEL", "claude-sonnet-4-6")
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-4-8")

AGENT_SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

# The model talks to these. Same functions the MCP server wraps.
TOOL_DISPATCH = {
    "list_branches": db.list_branches,
    "get_hours": db.get_hours,
    "list_events": db.list_events,
    "get_event": db.get_event,
    "list_programs": db.list_programs,
    "get_program": db.get_program,
    "get_fee": db.get_fee,
    "get_contact": db.get_contact,
    "search_wiki": search.search_wiki,
    "read_wiki_page": search.read_wiki_page,
}


def get_tool_schemas() -> list[dict]:
    """Anthropic tool schemas, derived from the MCP server so there is one source
    of truth: if the server advertises a tool, the eval agent can call it."""
    from groundtruth.mcp import mcp as server

    tools = anyio.run(server.list_tools)
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in tools
    ]


# ---------- Agent (mirrors what the chatbot does at runtime) ----------

def call_agent(question: str, model: str) -> dict:
    """One agent turn with tool use until the model stops calling tools."""
    import anthropic

    client = anthropic.Anthropic()
    schemas = get_tool_schemas()
    messages: list[dict] = [{"role": "user", "content": question}]
    tool_calls: list[dict] = []

    for _ in range(8):
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": AGENT_SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            tools=schemas,
            messages=messages,
        )
        used = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        for b in used:
            tool_calls.append({"name": b.name, "input": dict(b.input)})
        if resp.stop_reason != "tool_use" or not used:
            text = "".join(b.text for b in resp.content
                           if getattr(b, "type", None) == "text").strip()
            return {"response": text, "tool_calls": tool_calls}
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in used:
            try:
                out = TOOL_DISPATCH[b.name](**b.input)
            except Exception as exc:
                out = {"error": str(exc)}
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(out, default=str)})
        messages.append({"role": "user", "content": results})

    return {"response": "[eval-error: tool-use loop cap]", "tool_calls": tool_calls}


# ---------- Graders ----------

def grade_hard_fact(item: dict, result: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    called = [tc["name"] for tc in result["tool_calls"]]

    want = item.get("must_call_tool")
    if want and want not in called:
        reasons.append(f"required tool {want!r} not called (called: {called or 'none'})")

    forbidden = item.get("must_not_call_tool")
    if forbidden:
        if forbidden in ("any", "*"):
            if called:
                reasons.append(f"expected no tool calls, but called: {called}")
        else:
            names = [forbidden] if isinstance(forbidden, str) else list(forbidden)
            hit = [n for n in names if n in called]
            if hit:
                reasons.append(f"called forbidden tool(s): {hit}")

    text = result["response"].lower()
    for needle in item.get("must_contain", []):
        if needle.lower() not in text:
            reasons.append(f"missing required substring: {needle!r}")
    for needle in item.get("must_not_contain", []):
        if needle.lower() in text:
            reasons.append(f"contains forbidden substring: {needle!r}")
    return (not reasons, reasons)


def _norm_page(value: str) -> str:
    return (value or "").strip().lower().removeprefix("wiki/").removesuffix(".md").replace("_", "-")


def _page_matches(expected: str, actual: str) -> bool:
    e, a = _norm_page(expected), _norm_page(actual)
    return e == a or e.rsplit("/", 1)[-1] == a.rsplit("/", 1)[-1]


def grade_required_page_read(item: dict, result: dict) -> list[str]:
    expected = item.get("must_cite_page")
    if not expected:
        return []
    reads = [str(tc["input"].get("path_or_slug", "")) for tc in result["tool_calls"]
             if tc["name"] == "read_wiki_page"]
    if not any(_page_matches(expected, r) for r in reads):
        return [f"required page {expected!r} not read (reads: {[_norm_page(r) for r in reads] or 'none'})"]
    return []


def grade_text(item: dict, result: dict) -> list[str]:
    reasons = []
    text = result["response"].lower()
    for needle in item.get("must_contain", []):
        if needle.lower() not in text:
            reasons.append(f"missing required substring: {needle!r}")
    for needle in item.get("must_not_contain", []):
        if needle.lower() in text:
            reasons.append(f"contains forbidden substring: {needle!r}")
    return reasons


def grade_narrative_fast(item: dict, result: dict) -> tuple[bool, list[str]]:
    reasons = grade_required_page_read(item, result) + grade_text(item, result)
    return (not reasons, reasons)


JUDGE_SYSTEM_PROMPT = """You are a strict grader for a library-assistant eval set.
You get the user question, an expected-answer rubric, and the actual response.
Decide if the response satisfies the rubric:
- Does it answer what was asked?
- Does it cover the substantive points (wording may differ)?
- Does it avoid invented or contradictory facts? The assistant answers from
  official sources you cannot see, so do not flag plausible operational details
  (a phone number, a step) unless they contradict the rubric or are clearly made up.
Output ONLY JSON: {"pass": true|false, "reason": "<one sentence>"}."""


def grade_narrative(item: dict, result: dict, model: str) -> tuple[bool, list[str]]:
    import anthropic

    reasons = grade_text(item, result)
    if reasons:
        return (False, reasons)
    client = anthropic.Anthropic()
    user = (f"## Question\n{item['question']}\n\n## Rubric\n"
            f"{item.get('expected_summary', '(none)')}\n\n## Response\n{result['response']}")
    resp = client.messages.create(
        model=model, max_tokens=400,
        system=[{"type": "text", "text": JUDGE_SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    try:
        verdict = json.loads(text)
    except json.JSONDecodeError:
        return (False, [f"judge returned non-JSON: {text[:160]}"])
    if not verdict.get("pass"):
        reasons.append(f"judge: {verdict.get('reason', '(no reason)')}")
    return (not reasons, reasons)


def grade_contradiction(item: dict, result: dict) -> tuple[bool, list[str]]:
    keys = ("must_call_tool", "must_contain", "must_not_contain", "must_cite_page")
    if not any(item.get(k) for k in keys):
        return (False, ["contradiction eval has no assertions"])
    passed, reasons = grade_hard_fact(item, result)
    reasons += grade_required_page_read(item, result)
    return (passed and not reasons, reasons)


def grade(item: dict, result: dict, judge_model: str, *, use_judge: bool) -> tuple[bool, list[str]]:
    cat = item["category"]
    if cat == "hard_fact":
        return grade_hard_fact(item, result)
    if cat == "out_of_scope":
        return grade_hard_fact(item, result)
    if cat == "narrative":
        return grade_narrative(item, result, judge_model) if use_judge else grade_narrative_fast(item, result)
    if cat == "contradiction":
        return grade_contradiction(item, result)
    return (False, [f"unknown category: {cat}"])


# ---------- Runner ----------

def is_model_artifact(path: Path) -> bool:
    return not path.stem.endswith(NON_MODEL_ARTIFACT_SUFFIXES)


def latest_run(*, model_only: bool) -> Path | None:
    if not RUNS_DIR.exists():
        return None
    runs = sorted(RUNS_DIR.glob("*.json"))
    if model_only:
        runs = [p for p in runs if is_model_artifact(p)]
    return runs[-1] if runs else None


def load_previous() -> dict[str, bool] | None:
    path = latest_run(model_only=True)
    if not path:
        return None
    return {r["id"]: r["passed"] for r in json.loads(path.read_text())["results"]}


def run_live(items: list[dict], model: str, judge_model: str) -> list[dict]:
    results = []
    for item in items:
        print(f"  [{item['id']}] {item['question'][:64]}", file=sys.stderr)
        try:
            agent = call_agent(item["question"], model)
        except Exception as exc:
            results.append({"id": item["id"], "category": item["category"], "passed": False,
                            "reasons": [f"agent raised: {exc}"], "tool_calls": [], "response": ""})
            continue
        passed, reasons = grade(item, agent, judge_model, use_judge=True)
        results.append({"id": item["id"], "category": item["category"], "passed": passed,
                        "reasons": reasons, "tool_calls": agent["tool_calls"], "response": agent["response"]})
    return results


def run_replay(items: list[dict], judge_model: str) -> tuple[list[dict], Path | None]:
    source = latest_run(model_only=True)
    if not source:
        miss = [{"id": i["id"], "category": i["category"], "passed": False,
                 "reasons": ["no recorded run to replay; run --tier full first"],
                 "tool_calls": [], "response": ""} for i in items]
        return miss, None
    rows = {r["id"]: r for r in json.loads(source.read_text())["results"]}
    print(f"replaying from {source.relative_to(REPO_ROOT)}", file=sys.stderr)
    results = []
    for item in items:
        row = rows.get(item["id"])
        if not row:
            results.append({"id": item["id"], "category": item["category"], "passed": False,
                            "reasons": ["missing in recorded run"], "tool_calls": [], "response": ""})
            continue
        replay = {"response": row.get("response", ""), "tool_calls": row.get("tool_calls", [])}
        passed, reasons = grade(item, replay, judge_model, use_judge=False)
        results.append({"id": item["id"], "category": item["category"], "passed": passed,
                        "reasons": reasons, "tool_calls": replay["tool_calls"], "response": replay["response"]})
    return results, source


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", choices=("fast", "full", "release"), default="fast")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--judge-model", default=JUDGE_MODEL)
    ap.add_argument("--only", help="run a single eval id (not allowed for release)")
    args = ap.parse_args()

    if not GOLDEN_PATH.exists():
        print(f"missing {GOLDEN_PATH}", file=sys.stderr)
        return 2
    items = yaml.safe_load(GOLDEN_PATH.read_text())
    if args.only:
        if args.tier == "release":
            print("--tier release runs the whole set; drop --only", file=sys.stderr)
            return 2
        items = [i for i in items if i["id"] == args.only]
        if not items:
            print(f"no eval with id {args.only!r}", file=sys.stderr)
            return 2

    source = None
    if args.tier == "fast":
        prev = None
        results, source = run_replay(items, args.judge_model)
    else:
        prev = load_previous()
        print(f"running {len(items)} eval(s) against {args.model}", file=sys.stderr)
        results = run_live(items, args.model, args.judge_model)

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    regressions = [r["id"] for r in results
                   if prev and prev.get(r["id"]) is True and not r["passed"]] if prev else []

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if args.tier == "full" else f"-{args.tier}"
    artifact = RUNS_DIR / f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}{suffix}.json"
    artifact.write_text(json.dumps({
        "tier": args.tier, "model": args.model,
        "judge_model": args.judge_model if args.tier != "fast" else None,
        "source_artifact": str(source.relative_to(REPO_ROOT)) if source else None,
        "passed": passed, "total": total, "regressions": regressions, "results": results,
    }, indent=2, default=str))

    print(f"\n{passed}/{total} passed", file=sys.stderr)
    if regressions:
        print(f"  REGRESSIONS: {', '.join(regressions)}", file=sys.stderr)
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['id']}: {'; '.join(r['reasons'])}", file=sys.stderr)
    print(f"artifact: {artifact.relative_to(REPO_ROOT)}", file=sys.stderr)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
