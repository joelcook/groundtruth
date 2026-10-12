"""Grounding: every exact value in facts/ must appear in the source it cites.

Facts are what the assistant states as true: a price, a date, an opening time, a
phone number. Anyone can mistype or invent one: a person, the coding agent that
built the facts, or the unattended ``./gt rewrite`` updating them. Nothing else
would notice, because the tool would serve the wrong value faithfully. This
checks every one against the text of its cited source, deterministically:

    numbers   39 matches "$39", "39.00", or "USD 39"; 1200 matches "1,200";
              0.1 matches "$0.10" or "10 cents". Digits inside a date, a time,
              or a phone number don't count: "8:00 p.m." doesn't state an 8
    dates     2026-09-01 matches "September 1, 2026", "9/1/2026", 2026-09-01, or
              "Sept. 1" on a page that says 2026; 5/6/2026 reads US-style (May 6)
    times     "13:00" matches "1 p.m.", "1:00 PM", "13:00", or an hours range like "9-1";
              "12:00" matches "noon"
    contacts  phone numbers by all their digits; emails and URLs as written

Words (a weekday, a branch, a plan name) aren't checked here: they're names, not
measurements, and ``./gt rewrite``'s second model reads those. Fields that are
yours rather than the source's (the key, aliases, tags, sources) are skipped;
``notes`` are checked, because the assistant reads them as part of the fact.

``./gt check`` warns about ungrounded values; ``./gt check --production``, which
the server, Docker, and data bundles use, fails on them.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from groundtruth import checks
from groundtruth.project import Project

SKIP_FIELDS = {"id", "aliases", "tags", "source", "source_path", "source_url", "example"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|" \
           r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NAMED_DATE = re.compile(MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*[–-]\s*\d{1,2})?(?:,?\s+(\d{4}))?", re.I)
DAY_FIRST_DATE = re.compile(r"\b(\d{1,2})\s+" + MONTH_RE + r"\.?,?\s+(\d{4})", re.I)
SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
TIME_24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*(?:a\.?m\.?|p\.?m\.?))", re.I)
HOUR_RANGE = re.compile(r"\b(1[0-2]|0?[1-9])\s*(?:-|–|—|to|until|till)\s*(1[0-2]|0?[1-9])\b(?!\s*(?:a\.?m|p\.?m))", re.I)
CLOCK_CONTEXT = re.compile(r"\b(?:open|opens|opening|close|closes|closed|hours|daily|weekdays?|weekends?|"
                           r"mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
TIME_WORDS = {"am", "pm", "a", "p", "daily", "weekdays", "weekday", "weekends", "every", "on", "and", "or", "except"}


def _about_hours(text: str, m: re.Match) -> bool:
    """Is this "9-5" a span of hours? Only when the same sentence says so just before it, or "daily" etc. after."""
    before = re.split(r"[.;:!?\n]", text[max(0, m.start() - 40):m.start()])[-1]
    after = re.split(r"[.;:!?\n]", text[m.end():m.end() + 20])[0]
    word = re.match(r"\s*([a-z]+)", after, re.I)
    if word and word.group(1).lower() not in TIME_WORDS:
        return False  # "1-3 books", "9-5 items": a quantity, whatever came before it
    return bool(CLOCK_CONTEXT.search(before) or re.search(r"\b(?:daily|weekdays?|weekends?|every day)\b", after, re.I))
TIME_12 = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", re.I)
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
URL = re.compile(r"https?://[^\s)>\]\"']+", re.I)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def _month(name: str) -> int:
    return next(i for i, m in enumerate(MONTHS, 1) if m.startswith(name.lower()[:3]))


def _num(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


@dataclass
class SourceIndex:
    """Everything exact a source says, normalized so a fact can be looked up in it."""
    text: str
    numbers: set[Decimal] = field(default_factory=set)
    dates: set[tuple[int | None, int, int]] = field(default_factory=set)
    times: set[tuple[int, int]] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    years: set[int] = field(default_factory=set)

    @classmethod
    def of(cls, raw: str) -> "SourceIndex":
        m = checks.FRONTMATTER_RE.match(raw)
        text = raw[m.end():] if m else raw  # the capture date in the frontmatter isn't evidence
        idx = cls(text=" ".join(text.lower().split()))
        idx.years = {int(y) for y in re.findall(r"\b(19\d\d|20\d\d|21\d\d)\b", text)}
        for y, mo, d in ISO_DATE.findall(text):
            idx.dates.add((int(y), int(mo), int(d)))
        for name, d, y in NAMED_DATE.findall(text):
            idx.dates.add((int(y) if y else None, _month(name), int(d)))
        for d, name, y in DAY_FIRST_DATE.findall(text):
            idx.dates.add((int(y), _month(name), int(d)))
        for a, b, y in SLASH_DATE.findall(text):
            year = int(y) + (2000 if len(y) == 2 else 0)
            first, second = int(a), int(b)
            # US order (month/day); day/month only when it can't be US, so 5/6 is never also June 5
            idx.dates.add((year, second, first) if first > 12 else (year, first, second))
        for h, mi in TIME_24.findall(text):
            idx.times.add((int(h), int(mi)))
        for h, mi, half in TIME_12.findall(text):
            hour = int(h) % 12 + (12 if half.lower().startswith("p") else 0)
            idx.times.add((hour, int(mi or 0)))
        clock_ranges = [m for m in HOUR_RANGE.finditer(text) if _about_hours(text, m)]
        for m in clock_ranges:  # "Open Monday to Friday 9-5": opening hours written loosely
            for h in (int(m.group(1)), int(m.group(2))):
                idx.times.update({(h % 12, 0), (h % 12 + 12, 0)})
        if re.search(r"\bnoon\b", text, re.I):
            idx.times.add((12, 0))
        if re.search(r"\bmidnight\b", text, re.I):
            idx.times.add((0, 0))
        for hit in checks.PHONE_RE.findall(text):
            idx.phones.add(re.sub(r"\D", "", hit))
        for amount in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:cents?|¢)", text, re.I):
            idx.numbers.add(Decimal(amount) / 100)  # "10 cents" states 0.10; "10 books" doesn't
        # Numbers, once the dates, times, phone numbers, emails, and URLs are out of the way:
        # "8:00 p.m." doesn't state an 8, and a phone number doesn't state its area code.
        rest = text
        for m in reversed(clock_ranges):  # a clock range states times, not numbers
            rest = rest[:m.start()] + " " + rest[m.end():]
        for rx in (URL, EMAIL, ISO_DATE, NAMED_DATE, DAY_FIRST_DATE, SLASH_DATE, checks.PHONE_RE, TIME_12, TIME_24):
            rest = rx.sub(" ", rest)
        for hit in NUMBER.findall(rest):
            n = _num(hit)
            if n is not None:
                idx.numbers.add(n)
        idx.numbers.update(Decimal(y) for y in idx.years)  # "Fall 2026": a year is stated even inside a date
        return idx

    def has(self, kind: str, value: Any) -> bool:
        if kind == "number":
            return value in self.numbers
        if kind == "date":
            y, mo, d = value
            if y is None:
                return any((m_, d_) == (mo, d) for _, m_, d_ in self.dates)
            # "September 1" with no year only grounds a year the source states somewhere
            return (y, mo, d) in self.dates or ((None, mo, d) in self.dates and y in self.years)
        if kind == "time":
            return value in self.times
        if kind == "phone":  # every digit counts: another area code is another number
            n = 10 if len(value) >= 10 else 7
            return any(len(p) >= n and p[-n:] == value[-n:] for p in self.phones)
        return value in self.text  # email, url


def exact_values(value: Any) -> list[tuple[str, Any, str]]:
    """(kind, normalized, as written) for every exact value inside a field's value."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        n = _num(str(value))
        return [("number", n, str(value))] if n is not None else []
    if isinstance(value, dt.date):
        return [("date", (value.year, value.month, value.day), value.isoformat())]
    if isinstance(value, dict):
        return [v for x in value.values() for v in exact_values(x)]
    if isinstance(value, (list, tuple)):
        return [v for x in value for v in exact_values(x)]
    text = str(value)
    out: list[tuple[str, Any, str]] = []

    def take(rx: re.Pattern, kind: str, norm) -> None:
        nonlocal text
        for hit in rx.finditer(text):
            out.append((kind, norm(hit), hit.group(0)))
        text = rx.sub(" ", text)

    take(URL, "url", lambda h: h.group(0).rstrip(".,;").lower())
    take(EMAIL, "email", lambda h: h.group(0).lower())
    take(ISO_DATE, "date", lambda h: (int(h.group(1)), int(h.group(2)), int(h.group(3))))
    take(NAMED_DATE, "date", lambda h: (int(h.group(3)) if h.group(3) else None, _month(h.group(1)), int(h.group(2))))
    take(checks.PHONE_RE, "phone", lambda h: re.sub(r"\D", "", h.group(0)))
    take(TIME_12, "time", lambda h: (int(h.group(1)) % 12 + (12 if h.group(3).lower().startswith("p") else 0),
                                     int(h.group(2) or 0)))
    take(TIME_24, "time", lambda h: (int(h.group(1)), int(h.group(2))))
    for hit in NUMBER.findall(text):
        n = _num(hit)
        if n is not None:
            out.append(("number", n, hit))
    return [(k, (None, v[1], v[2]) if k == "date" and v[0] is None else v, raw) for k, v, raw in out]


def row_sources(row: dict, default: str | None) -> list[str]:
    """The sources a row cites: its own, else its file's."""
    nested = row.get("source") if isinstance(row.get("source"), dict) else {}
    own = row.get("source_path") or nested.get("path")
    return [str(own)] if own else ([default] if default else [])


def _load(project: Project, rel: str, cache: dict[str, SourceIndex | None]) -> SourceIndex | None:
    if rel not in cache:
        path = project.root / rel
        cache[rel] = SourceIndex.of(path.read_text(encoding="utf-8")) if path.is_file() else None
    return cache[rel]


def row_problems(project: Project, row: dict, key: str, sources: list[str], where: str,
                 cache: dict[str, SourceIndex | None] | None = None,
                 fields: set[str] | None = None) -> list[str]:
    """Exact values in one row (or only ``fields`` of it) that none of its sources state."""
    cache = {} if cache is None else cache
    indexes = [i for s in sources if (i := _load(project, s, cache)) is not None]
    if not indexes:
        return []  # no readable source: ./gt check reports that on its own
    out = []
    for name, value in row.items():
        if name in SKIP_FIELDS or name == key or (fields is not None and name not in fields):
            continue
        for kind, norm, raw in exact_values(value):
            if not any(i.has(kind, norm) for i in indexes):
                out.append(f"{where}: row {row.get(key)!s}: {name} has {raw!r}, which {', '.join(sources)} "
                           f"doesn't say")
    return out


def problems(project: Project) -> list[str]:
    """Every exact value in a live facts file that its cited source doesn't state."""
    from groundtruth import examples, facts
    try:
        collections = facts.load(project)
    except facts.FactsError:
        return []
    fictional = {p.resolve() for p in examples.find(project).facts}
    cache: dict[str, SourceIndex | None] = {}
    out = []
    for c in collections:
        if Path(c.file).resolve() in fictional:
            continue
        where = project.rel(c.file)
        for row in c.rows:
            sources = row_sources(row, c.source_path)
            if not sources or not any((project.root / s).is_file() for s in sources):
                out.append(f"{where}: row {row.get(c.key)!s} cites no captured source (a URL alone can't be "
                           f"checked). Ingest the page and cite sources/pages/...")
                continue
            out += row_problems(project, row, c.key, sources, where, cache)
    return out
