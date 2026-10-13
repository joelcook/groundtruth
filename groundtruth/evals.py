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
    allowed = set(item.get("may_call_tool") or [])  # e.g. get_contact, to point someone to the right desk
    if forbidden in ("any", "*") and [c for c in called if c not in allowed]:
        reasons.append(f"should not call any tool{f' but {sorted(allowed)}' if allowed else ''}, but called "
                       f"{[c for c in called if c not in allowed]}")
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


# What each kind of claim has to match in the tool results, unit and all: a dollar amount a dollar field, a
# number of weeks a field in weeks, a count a field that isn't a price or a duration. So a returned
# `duration_days: 30` backs "30 days", not "30 weeks", "$30", or "up to 30 books".
MONEY_WORDS = {"usd", "eur", "gbp", "cad", "aud", "jpy", "price", "prices", "cost", "costs", "fee", "fees", "amount",
               "charge", "charges", "tuition", "dollar", "dollars", "fine", "fines", "cents", "rent", "deposit"}
DURATION_WORDS = {"minute", "minutes", "hour", "hours", "hr", "hrs", "day", "days", "week", "weeks",
                  "month", "months", "year", "years", "duration", "window", "period", "length", "term"}
PERCENT_WORDS = {"pct", "percent", "percentage", "rate", "share"}
SIZE_WORDS = {"kb", "mb", "gb", "tb", "kib", "mib", "gib", "tib", "bytes", "size", "megabytes", "gigabytes"}
NOT_EVIDENCE = {"query", "filters", "hint", "error", "score", "backend", "count", "collection", "truncated",
                "ambiguous"}  # echoes of the question, or bookkeeping: never proof of a fact
CURRENCIES = (("$", r"\$|\b(?:usd|cad|aud|dollars?)\b"), ("€", r"€|\b(?:eur|euros?)\b"), ("£", r"£|\b(?:gbp|pounds?)\b"),
              ("¥", r"¥|\b(?:jpy|yen)\b"))
UNIT_WORDS = (("minute", r"\bmins?\b|\bminutes?\b"), ("hour", r"\bhours?\b|\bhrs?\b"), ("day", r"\bdays?\b"),
              ("week", r"\bweeks?\b"), ("month", r"\bmonths?\b"), ("year", r"\byears?\b"))
SIZE_UNITS = (("kb", r"\bki?b\b|kilobytes?"), ("mb", r"\bmi?b\b|megabytes?"), ("gb", r"\bgi?b\b|gigabytes?"),
              ("tb", r"\bti?b\b|terabytes?"), ("bytes", r"\bbytes\b"))
ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
NUMBER_WORD_RE = re.compile(rf"\b(?:(?:{'|'.join(TENS)})(?:[\s-](?:{'|'.join(ONES)}))?|{'|'.join(TEENS)}|"
                            rf"{'|'.join(ONES)}|a hundred|one hundred)\b(?=[\s-](?:business[\s-]|calendar[\s-])?"
                            rf"(?:days?|weeks?|months?|years?|hours?|hrs?|minutes?|mins?)\b)", re.I)
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _word_value(words: str) -> int:
    parts = re.split(r"[\s-]+", words.lower())
    if parts[-1] == "hundred":
        return 100
    return sum(TENS.get(w, 0) + TEENS.get(w, 0) + ONES.get(w, 0) for w in parts)


def digits(text: str) -> str:
    """Spelled-out durations as digits ("twenty-one days" -> "21 days"), so a duration is compared by value,
    never as a piece of another ("one day" inside "twenty-one days")."""
    return NUMBER_WORD_RE.sub(lambda m: str(_word_value(m.group(0))), text)


MONEY_WORD_RE = re.compile(rf"\b(?:(?:{'|'.join(TENS)})(?:[\s-](?:{'|'.join(ONES)}))?|{'|'.join(TEENS)}|"
                           rf"{'|'.join(ONES)}|a hundred|one hundred)(?=[\s-](?:dollars?|cents?|percent)\b)", re.I)


def spelled_out(text: str) -> str:
    """Amounts written in words as digits too: "a ten dollar cap" -> "a 10 dollar cap"."""
    return MONEY_WORD_RE.sub(lambda m: str(_word_value(m.group(0))), digits(text))


def _number(text: str):
    """The first number in a claim."""
    from decimal import Decimal, InvalidOperation
    m = NUMBER_RE.search(digits(text))
    try:
        return Decimal(m.group(0).replace(",", "")) if m else None
    except InvalidOperation:
        return None


def _tokens(key: str) -> set[str]:
    """A field's name as words: `price_usd` -> {price, usd}; `loanPeriodDays` -> {loan, period, days}."""
    return set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", key).lower())) - {""}


def _currency(text: str) -> str | None:
    """$, €, £, or ¥ for a claim or a field name that says which; None when it doesn't."""
    return next((sym for sym, rx in CURRENCIES if re.search(rx, text.replace("_", " "), re.I)), None)


def _unit(text: str) -> str | None:
    return next((unit for unit, rx in UNIT_WORDS if re.search(rx, text.replace("_", " "), re.I)), None)


def _size_unit(text: str) -> str | None:
    return next((unit for unit, rx in SIZE_UNITS if re.search(rx, text.replace("_", " "), re.I)), None)


def _money(text: str):
    n = _number(text)
    if n is not None and re.search(r"\bcents?\b", text, re.I):
        return None, n / 100  # cents of whichever currency
    return _currency(text), n


GENERIC_FIELDS = {"value", "amount", "quantity", "number", "total", "qty"}  # say what they measure in `unit`


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)) \
        or (isinstance(value, str) and bool(NUMBER_RE.fullmatch(value.strip())))


def _leaves(obj, key: str = ""):
    """(field, value) for every value in a tool result. A generic number field takes the row's `unit` (and
    a money field its `currency`) with it: `value: 30` next to `unit: days` is "value days". A field that
    names its own kind keeps it: `trial_days` beside `currency: USD` is still days."""
    if isinstance(obj, dict):
        unit = " ".join(str(obj[c]) for c in ("unit", "units") if isinstance(obj.get(c), str))
        currency = str(obj["currency"]) if isinstance(obj.get("currency"), str) else ""
        for k, v in obj.items():
            if str(k) in NOT_EVIDENCE:
                continue
            name = str(k)
            if _is_number(v):
                words = _tokens(name)
                if words & GENERIC_FIELDS and unit:
                    # `amount` with `unit: days` is days; with `unit: per page` it's still an amount (a rate)
                    names_kind = _tokens(unit) & (DURATION_WORDS | SIZE_WORDS | PERCENT_WORDS)
                    pure = words <= GENERIC_FIELDS and not unit.lower().startswith("per ")
                    name = f"value {unit}" if pure and names_kind else f"{name} {unit}"
                if currency and (words & MONEY_WORDS or _tokens(name) & MONEY_WORDS):
                    name = f"{name} {currency}"
            yield from _leaves(v, name)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v, key)
    elif obj is not None and not isinstance(obj, bool):
        yield key, obj


class Evidence:
    """What the tools actually returned, typed: the values of the rows and pages, not the echo of the query.

    Money is kept as (currency, amount), durations as (unit, number); either is None when the field doesn't
    say (a plain `fee`, a `loan_period`), which then matches any. Counts are the numbers that aren't money,
    durations, or rates.
    """

    def __init__(self, results: list) -> None:
        from groundtruth import grounding
        leaves = [leaf for r in results for leaf in _leaves(r)]
        self.text = "\n".join(str(v) for _, v in leaves)
        self.index = grounding.SourceIndex.of(self.text)
        self.money: set = set()
        self.durations: set = set()
        self.percents: set = set()
        self.sizes: set = set()
        self.counts: set = set()
        for key, value in leaves:
            if _is_number(value):
                n, words = _number(str(value)), _tokens(key)
                spaced = " ".join(sorted(words))  # loanPeriodDays -> "days loan period"
                if words & MONEY_WORDS:
                    self.money.add((_currency(spaced), n / 100 if "cents" in words else n))
                elif words & PERCENT_WORDS:
                    self.percents.add(n)
                elif words & DURATION_WORDS:
                    self.durations.add((_unit(spaced), n))
                elif words & SIZE_WORDS:
                    self.sizes.add((_size_unit(spaced), n))
                else:
                    self.counts.add(n)
                continue
            text = digits(str(value))  # prose and statements: the unit is written next to the number
            typed = []
            for rx, add in ((checks.MONEY_RE, lambda s: self.money.add(_money(s))),
                            (checks.DURATION_RE, lambda s: self.durations.add((_unit(s), _number(s)))),
                            (checks.PERCENT_RE, lambda s: self.percents.add(_number(s))),
                            (checks.PERCENT_WORDS_RE, lambda s: self.percents.add(_number(s))),
                            (checks.SIZE_RE, lambda s: self.sizes.add((_size_unit(s), _number(s))))):
                for hit in rx.finditer(text):
                    add(hit.group(0))
                    typed.append(hit.span())
            plain = "".join(" " if any(a <= i < b for a, b in typed) else ch for i, ch in enumerate(text))
            self.counts.update(n for k, n, _ in grounding.exact_values(plain) if k == "number")

    def backs(self, kind: str, raw: str) -> bool:
        from groundtruth import grounding
        if kind == "money":
            cur, n = _money(raw)
            return any(n == m and (c is None or cur is None or c == cur) for c, m in self.money)
        if kind == "duration":
            unit, n = _unit(raw), _number(raw)
            return any(n == m and (u is None or u == unit) for u, m in self.durations)
        if kind == "percentage":
            return _number(raw) in self.percents
        if kind == "size limit":
            unit, n = _size_unit(raw), _number(raw)
            return any(n == m and (u is None or u == unit) for u, m in self.sizes)
        if kind in ("limit", "amount"):
            return _number(raw) in self.counts
        if kind == "time" and re.search(r"noon|midnight", raw, re.I):
            raw = "12:00" if "noon" in raw.lower() else "00:00"
        if kind == "time" and re.fullmatch(r"\d{1,2}:\d{2}", raw.strip()):  # no am/pm: "open 10:00 to 5:00"
            hour, minute = (int(x) for x in raw.split(":"))
            return any(self.index.has("time", (h, minute)) for h in {hour, (hour + 12) % 24} if h < 24)
        values = grounding.exact_values(raw)
        if not values:
            return " ".join(raw.lower().split()) in " ".join(self.text.lower().split())
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
    asked = Evidence([spelled_out(question)]) if question_counts else None
    return [raw for kind, raw in _exact_in(response)
            if not evidence.backs(kind, raw) and not (asked is not None and (raw in question or asked.backs(kind, raw)))]


TYPED_KINDS = {"money", "duration", "percentage", "size limit", "date", "time"}
APPROXIMATE_RE = re.compile(r"\b(?:about|around|roughly|approximately|nearly|almost|over|under|some)\s+$", re.I)


def _exact_in(text: str) -> list[tuple[str, str]]:
    """(kind, as written) for every exact value in a piece of text. A bare count ("up to 30") whose number
    is part of a typed value ("up to 30 days") is judged as that value instead."""
    hits: list[tuple[str, str, tuple[int, int]]] = []
    for kind, rx in checks.PATTERNS:
        if kind not in EXACT_KINDS:
            continue
        for m in rx.finditer(text):
            raw, (a, b) = m.group(0), m.span()
            if kind == "time" and (half := re.match(r"\s?(?:a\.m\.|p\.m\.|am|pm)(?![a-z])", text[b:b + 6], re.I)):
                raw, b = raw + half.group(0), b + half.end()  # "5:00 PM" is 17:00, not 5:00
            if kind == "duration" and APPROXIMATE_RE.search(text[max(0, a - 16):a]):
                continue  # "about 8 months away" isn't a stated fact
            hits.append((kind, raw, (a, b)))
    typed = [span for kind, _, span in hits if kind in TYPED_KINDS]
    found: list[tuple[str, str]] = []
    for kind, raw, (a, b) in hits:
        if kind in ("limit", "amount"):
            m = NUMBER_RE.search(raw)
            at = (a + m.start(), a + m.end()) if m else (a, b)
            if any(s <= at[0] and at[1] <= e for s, e in typed):
                continue
        if raw not in [f for _, f in found]:
            found.append((kind, raw))
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
        if out.get("stop_reason") == "error":  # "API Error: 401 ..." is the model not answering, not a wrong answer
            results.append(_result(item, False, [f"agent failed: {out['response'][:300]}"], tool_calls=[],
                                   response=""))
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
