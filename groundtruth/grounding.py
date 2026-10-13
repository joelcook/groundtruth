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

Being on the page isn't enough, though. The likeliest wrong value isn't an
invented one: it's a real one from the wrong row, Sunday's 1 p.m. typed into
Saturday. So a value must also be where the source describes its row. The page
is split into blocks (a table row, a list item, a paragraph), each read with the
headings above it. A row's words (its key, aliases, text fields, and the names of
the rows it refers to) find the blocks about it, weighted by how much each word
tells it apart from the other rows citing that source. A value fails when:

    the source has a block about this row, stating a value of the same kind;
    the value isn't in it; and
    every block that does state the value is about another row, and doesn't
    mention what sets this row apart from that one.

A page that doesn't tell its rows apart (one long paragraph, a pricing grid with
one column per plan) can't fail this way; its values are checked against the
whole page, as above.

``./gt check`` warns about ungrounded and misplaced values; ``./gt check
--production``, which the server, Docker, and data bundles use, fails on them.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from groundtruth import checks, facts
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
    """Is this "9-5" a span of hours? Only when the same sentence says so just before it ("open", "Mon-Fri"),
    or "weekdays" etc. after. Not "daily" alone: "take 1-3 daily" is a dose."""
    before = re.split(r"[.;:!?\n]", text[max(0, m.start() - 40):m.start()])[-1]
    after = re.split(r"[.;:!?\n]", text[m.end():m.end() + 20])[0]
    word = re.match(r"\s*([a-z]+)", after, re.I)
    if word and word.group(1).lower() not in TIME_WORDS:
        return False  # "1-3 books", "9-5 items": a quantity, whatever came before it
    return bool(CLOCK_CONTEXT.search(before) or re.search(r"\b(?:weekdays?|weekends?|every day)\b", after, re.I))


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

    def states(self, kind: str) -> bool:
        """Does this text state any value of this kind at all?"""
        found = {"number": self.numbers, "date": self.dates, "time": self.times, "phone": self.phones}
        if kind in found:
            return bool(found[kind])
        return bool((URL if kind == "url" else EMAIL).search(self.text))


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
                           f"doesn't say. Fix the value, or cite the source that states it")
    return out


def problems(project: Project) -> list[str]:
    """Every exact value in a live facts file that its cited source doesn't state, or states for another row."""
    from groundtruth import examples
    try:
        collections = facts.load(project)
    except facts.FactsError:
        return []
    fictional = {p.resolve() for p in examples.find(project).facts}
    cache: dict[str, SourceIndex | None] = {}
    blocks: dict[str, list[Block]] = {}
    out = []
    for c in collections:
        if Path(c.file).resolve() in fictional:
            continue
        where = project.rel(c.file)
        grounded = []
        for row in c.rows:
            sources = row_sources(row, c.source_path)
            if not sources or not any((project.root / s).is_file() for s in sources):
                out.append(f"{where}: row {row.get(c.key)!s} cites no captured source (a URL alone can't be "
                           f"checked). Ingest the page and cite sources/pages/...")
                continue
            out += row_problems(project, row, c.key, sources, where, cache)
            grounded.append(row)
        out += placement_problems(project, c, grounded, collections, where, blocks)
    return out


# ===================================================================== placement


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_NAMES = {**{d: d for d in WEEKDAYS}, "mon": "monday", "tue": "tuesday", "tues": "tuesday",
             "wed": "wednesday", "thu": "thursday", "thur": "thursday", "thurs": "thursday", "fri": "friday",
             "sat": "saturday", "sun": "sunday"}
DAY = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)s?\.?"
DAY_RANGE = re.compile(rf"\b{DAY}\s*(?:-|–|—|to|through|thru|until|till)\s*{DAY}(?![a-z])", re.I)
EVERY_DAY = re.compile(r"\b(?:daily|every day|(?:7|seven) days a week)\b", re.I)
PLACE_STOPWORDS = facts.STOPWORDS | {"all", "am", "any", "as", "from", "has", "have", "if", "its", "no", "not",
                                     "our", "per", "pm", "than", "that", "then", "this", "through", "thru", "till",
                                     "until", "us", "usd", "we", "will"}
HEADING = re.compile(r"(#{1,6})\s+(.+?)[\s#]*")
BOLD_LINE = re.compile(r"\*\*([^*]{1,80})\*\*:?")  # "**Central Library**" alone on a line: a heading in all but name
LIST_ITEM = re.compile(r"(?:[-*+]|\d{1,3}[.)])\s")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*\"'(\[$])")
ABBREVIATION = re.compile(r"\b(?:[ap]\.m|e\.g|i\.e|etc|vs|approx|dept|no|st|dr|mr|mrs|ms|jr|sr|inc|co|ave|blvd|rd|"
                          r"jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|mon|tues?|wed|thu|thurs|fri|sat|sun|"
                          r"[a-z])\.$", re.I)  # "9 a.m. Monday", "Sept. 1", "J. Smith": not the end of a sentence
EPS = 1e-9


def _day(word: str) -> str:
    word = word.lower().rstrip(".")
    return DAY_NAMES.get(word) or DAY_NAMES[word[:-1]]


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def words(text: Any) -> set[str]:
    """What a row or a block is about, as words: lowercased, roughly singular, weekdays spelled out.
    "Monday to Thursday" says monday, tuesday, wednesday, and thursday; "weekdays" says all five."""
    low = EMAIL.sub(" ", URL.sub(" ", str(text))).lower()
    out: set[str] = set()
    for m in DAY_RANGE.finditer(low):
        start, end = WEEKDAYS.index(_day(m.group(1))), WEEKDAYS.index(_day(m.group(2)))
        out.update(WEEKDAYS[(start + k) % 7] for k in range((end - start) % 7 + 1))
    if EVERY_DAY.search(low):
        out.update(WEEKDAYS)
    for w in re.findall(r"[^\W\d_]+", low):
        if w in DAY_NAMES or (w.endswith("s") and w[:-1] in DAY_NAMES):
            out.add(_day(w))
        elif w in {"weekday", "weekdays"}:
            out.update(WEEKDAYS[:5])
        elif w in {"weekend", "weekends"}:
            out.update(WEEKDAYS[5:])
        elif len(w) > 1 and w not in PLACE_STOPWORDS:
            out.add(_stem(w))
    return out


@dataclass
class Block:
    """One place on a page that says something: a table row, a list item, or a sentence."""
    source: str
    text: str
    words: set[str]  # its own words, its table's header, and the headings above it
    index: SourceIndex

    def quote(self) -> str:
        text = " ".join(self.text.split())
        text = text[m.end():] if (m := LIST_ITEM.match(text)) else text.strip("| ")
        text = re.sub(r"\s*\|\s*", " | ", text).replace("**", "")
        return text if len(text) <= 140 else text[:137].rstrip() + "..."


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for piece in SENTENCE_END.split(text):
        if out and ABBREVIATION.search(out[-1]):
            out[-1] += " " + piece
        else:
            out.append(piece)
    return out


def blocks_of(raw: str, source: str = "") -> list[Block]:
    """Split a captured page into blocks, each knowing the headings (and table header) it sits under. A table
    row, a list item, or a sentence of a paragraph is a block: "Office visit $120. Flu shot $35." is two."""
    m = checks.FRONTMATTER_RE.match(raw)
    body = raw[m.end():] if m else raw
    years = SourceIndex.of(raw).years  # "June 19" in one block may lean on a year stated in another
    heads: list[tuple[int, str]] = []
    header: str | None = None
    para: list[str] = []
    found: list[tuple[str, set[str], set[str]]] = []  # (text, its own words and its table header's, its headings')

    def add(text: str, context: str = "") -> None:
        found.append((text, words(text) | words(context), words(" ".join(h for _, h in heads))))

    def flush() -> None:
        if para:
            text = " ".join(para)
            for piece in [text] if LIST_ITEM.match(text) else _sentences(text):
                add(piece)
            para.clear()

    for line in body.splitlines():
        s = line.strip()
        if s.startswith("|") and s.count("|") >= 2:
            flush()
            if re.fullmatch(r"[\s|:-]+", s):
                continue  # the |---|---| under a header
            if header is None:
                header = s
                add(s)
            else:
                add(s, header)
            continue
        header = None
        if len(para) == 1 and re.fullmatch(r"=+|-{3,}", s):  # a "Title" underlined with === or ---
            heads = [x for x in heads if x[0] < (1 if s[0] == "=" else 2)] + [(1 if s[0] == "=" else 2, para.pop())]
            continue
        h = HEADING.fullmatch(s)
        b = BOLD_LINE.fullmatch(s) if not para else None
        if h or b:
            flush()
            level, title = (len(h.group(1)), h.group(2)) if h else (7, b.group(1))
            heads = [x for x in heads if x[0] < level] + [(level, title)]
            continue
        if not s or LIST_ITEM.match(s):
            flush()
        if s:
            para.append(s)
    flush()
    # A heading over every block (the page title, usually) can't tell one block from another, and would
    # make every block seem to name a row that shares a word with it: "City offices" isn't about City Hall.
    common = set.intersection(*(h for _, _, h in found)) if len(found) > 1 else set()
    out = []
    for text, own, heading in found:
        idx = SourceIndex.of(text)
        idx.years = years
        out.append(Block(source, text, own | (heading - common), idx))
    return out


NAME_FIELDS = {"id", "aliases", "name", "title"}


def _row_words(row: dict, key: str, refs: dict[str, dict[str, tuple[str, dict]]],
               names_only: bool = False) -> set[str]:
    """A row's words: its key, aliases, tags, and text fields, plus the names of the rows its `ref` fields point
    to. ``names_only``: just the words that name it (key, aliases, name, title, and those referenced names)."""
    out: set[str] = set()
    for name, value in row.items():
        if name in SKIP_FIELDS - {"id", "aliases", "tags"} or isinstance(value, (bool, int, float)) or value is None:
            continue
        targets = refs.get(name, {})
        if names_only and name != key and name not in NAME_FIELDS and not targets:
            continue
        for v in value if isinstance(value, list) else (value.values() if isinstance(value, dict) else [value]):
            out |= words(v)
            if (target := targets.get(str(v).lower())) is not None:  # branch: central -> "Maplewood Central Library"
                out |= _row_words(target[1], target[0], {}, names_only)
    return out


def _refs(c: facts.Collection, collections: list[facts.Collection]) -> dict[str, dict[str, tuple[str, dict]]]:
    """For each `ref` filter field: every key and alias in the referenced collection -> (its key field, its row)."""
    by_name = {x.name: x for x in collections}
    out: dict[str, dict[str, tuple[str, dict]]] = {}
    for f in (f for t in c.tools for f in t.filters if f.ref in by_name):
        target = by_name[f.ref]
        names = out.setdefault(f.field, {})
        for r in target.rows:
            for n in [r.get(target.key), *(r.get("aliases") or [])]:
                if n is not None:
                    names.setdefault(str(n).lower(), (target.key, r))
    return out


def _rival(i: int, j: int, score: list[list[float]], names: list[set[str]], own: list[set[str]],
           block: Block) -> int | None:
    """The row that block j is about instead of row i, if any: one that matches the block better, where the
    block names nothing that sets row i apart from it."""
    for k in sorted(range(len(score)), key=lambda k: -score[k][j]):
        if k != i and score[k][j] > score[i][j] + EPS and not (names[i] - own[k]) & block.words:
            return k
    return None


def placement_problems(project: Project, c: facts.Collection, rows: list[dict], collections: list[facts.Collection],
                       where: str, cache: dict[str, list[Block]] | None = None) -> list[str]:
    """Values a source states, but only where it describes another row of this collection."""
    cache = {} if cache is None else cache
    refs = _refs(c, collections)
    groups: dict[tuple[str, ...], list[dict]] = {}
    for row in rows:
        groups.setdefault(tuple(row_sources(row, c.source_path)), []).append(row)
    out = []
    for sources, group in groups.items():
        if len(group) < 2:
            continue  # a row with no siblings on its page can't be confused with one
        blocks = []
        for s in sources:
            if s not in cache:
                path = project.root / s
                cache[s] = blocks_of(path.read_text(encoding="utf-8"), s) if path.is_file() else []
            blocks += cache[s]
        own = [_row_words(r, c.key, refs) for r in group]
        names = [_row_words(r, c.key, refs, names_only=True) for r in group]
        df = Counter(w for ws in own for w in ws)
        weight = {w: math.log(len(group) / n) for w, n in df.items()}  # a word every row has tells nothing
        score = [[sum(weight[w] for w in ws & b.words) for b in blocks] for ws in own]
        for i, row in enumerate(group):
            homes: dict[str, set[int]] = {}  # per kind of value: the blocks that best describe this row
            for name, value in row.items():
                if name in SKIP_FIELDS or name == c.key:
                    continue
                for kind, norm, raw in exact_values(value):
                    if kind not in homes:
                        stating = [j for j, b in enumerate(blocks) if b.index.states(kind)]
                        best = max((score[i][j] for j in stating), default=0.0)
                        homes[kind] = {j for j in stating if score[i][j] >= best - EPS} if best > EPS else set()
                    home = homes[kind]
                    hits = [j for j, b in enumerate(blocks) if b.index.has(kind, norm)]
                    if not home or not hits or home & set(hits):
                        continue  # nowhere to compare against, not on the page at all, or right where it belongs
                    found = [(j, k) for j in hits if (k := _rival(i, j, score, names, own, blocks[j])) is not None]
                    if len(found) < len(hits):
                        continue  # some block that states it could be about this row
                    j, k = found[0]
                    out.append(f"{where}: row {row.get(c.key)!s}: {name} has {raw!r}, which {blocks[j].source} "
                               f"gives for {group[k].get(c.key)!s} (\"{blocks[j].quote()}\"), not for this row "
                               f"(\"{blocks[min(home)].quote()}\"). Fix the value, or cite the source that states "
                               f"it for this row")
    return out
