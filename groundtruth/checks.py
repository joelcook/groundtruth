"""``./gt check``: every cheap, deterministic check in one place.

  facts    every facts/*.yaml parses, has keys, sources, and valid tools
  wiki     frontmatter, page types, a Sources & Lineage table, links that resolve
  prose    no dates or dollar amounts in wiki prose (the rule that keeps the bot honest)
  evals    every golden question points at a real tool and a real page

Errors fail the check (exit 1). Warnings are printed but don't fail it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from groundtruth import facts
from groundtruth.project import Project

VALID_PAGE_TYPES = {"journey", "process", "policy", "entity", "comparison", "faq", "guide", "internal"}
REQUIRED_KEYS = {"title", "page_type", "sources"}
SKIP = {"index.md", "log.md", "gaps.md"}
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
LINEAGE_RE = re.compile(r"\n##\s+Sources\s+(&|&amp;|and)\s+Lineage\b", re.I)

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_RE = re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[–-]\s*\d{{1,2}})?,?\s+\d{{4}}\b")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
SLASH_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
MONEY_RE = re.compile(r"(?:\$\s?\d|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|dollars)\b)")
FACT_OK_RE = re.compile(r"<!--\s*fact-ok:")
GOLDEN_CATEGORIES = {"hard_fact", "narrative", "out_of_scope", "contradiction"}


@dataclass
class Report:
    errors: dict[str, list[str]] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def error(self, section: str, msg: str) -> None:
        self.errors.setdefault(section, []).append(msg)

    def warn(self, section: str, msg: str) -> None:
        self.warnings.setdefault(section, []).append(msg)

    @property
    def ok(self) -> bool:
        return not any(self.errors.values())


# ===================================================================== wiki


def wiki_pages(project: Project) -> list[Path]:
    if not project.wiki_dir.exists():
        return []
    return [p for p in sorted(project.wiki_dir.rglob("*.md")) if p.name not in SKIP]


def _slugs(project: Project) -> set[str]:
    out = set()
    for p in wiki_pages(project):
        out.add(p.stem.lower())
        out.add(p.relative_to(project.wiki_dir).as_posix().lower().removesuffix(".md"))
    return out


def split_frontmatter(text: str) -> tuple[dict, str] | tuple[None, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None, text
    return (meta if isinstance(meta, dict) else None), text[m.end():]


def check_wiki(project: Project, report: Report) -> None:
    pages = wiki_pages(project)
    slugs = _slugs(project)
    for path in pages:
        rel = project.rel(path)
        text = path.read_text(encoding="utf-8")
        meta, _ = split_frontmatter(text)
        if meta is None:
            report.error("wiki", f"{rel}: missing or invalid YAML frontmatter (--- title: ... ---)")
            continue
        missing = REQUIRED_KEYS - set(meta)
        if missing:
            report.error("wiki", f"{rel}: frontmatter is missing {sorted(missing)}")
        ptype = str(meta.get("page_type", "")).lower()
        if ptype and ptype not in VALID_PAGE_TYPES:
            report.error("wiki", f"{rel}: page_type {ptype!r} must be one of {sorted(VALID_PAGE_TYPES)}")
        if not LINEAGE_RE.search(text):
            report.error("wiki", f"{rel}: add a '## Sources & Lineage' section at the bottom (the audit trail)")
        for src in meta.get("sources") or []:
            if isinstance(src, str) and src.startswith("sources/") and not (project.root / src).exists():
                report.warn("wiki", f"{rel}: source {src} does not exist")
        for target in WIKILINK_RE.findall(text):
            t = target.strip().lower()
            if t.startswith(("sources/", "raw/", "http")):
                continue
            t = t.removeprefix("wiki/").removesuffix(".md")
            if t not in slugs and t.rsplit("/", 1)[-1] not in slugs:
                report.error("wiki", f"{rel}: broken link [[{target}]]")
    report.notes["wiki"] = f"{len(pages)} page(s)"


def hard_facts_in(path: Path) -> list[tuple[int, str, str]]:
    """(line number, kind, text) for every date or dollar amount in a page's prose."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    offset = text[: m.end()].count("\n") if m else 0
    body = text[m.end():] if m else text
    body = LINEAGE_RE.split(body, maxsplit=1)[0]
    lines = body.splitlines()
    found = []
    for i, line in enumerate(lines):
        if FACT_OK_RE.search(line) or (i > 0 and FACT_OK_RE.search(lines[i - 1])):
            continue
        seen: set[str] = set()
        for kind, rx in (("date", DATE_RE), ("date", ISO_DATE_RE), ("date", SLASH_DATE_RE), ("money", MONEY_RE)):
            hit = rx.search(line)
            if hit and kind not in seen:
                seen.add(kind)
                found.append((i + 1 + offset, kind, hit.group(0)))
    return found


def check_prose(project: Project, report: Report) -> None:
    pages = [p for p in wiki_pages(project) if "internal" not in p.relative_to(project.wiki_dir).parts]
    for path in pages:
        for line, kind, text in hard_facts_in(path):
            report.error("prose", f"{project.rel(path)}:{line}: {kind} {text!r} in prose. Put it in facts/ "
                                  f"and let a tool answer it (or justify with <!-- fact-ok: why -->)")
    report.notes["prose"] = f"{len(pages)} page(s) scanned for dates and dollar amounts"


# ===================================================================== facts


def check_facts(project: Project, report: Report) -> list[facts.Collection]:
    files = facts.fact_files(project)
    if not files:
        report.error("facts", f"no facts files in {project.rel(project.facts_dir)}/. "
                              "Add one (see docs/facts.md) or run ./gt init")
        return []
    try:
        collections = facts.load(project)
    except facts.FactsError as exc:
        for p in exc.problems:
            report.error("facts", p)
        return []
    for w in facts.warnings(project, collections):
        report.warn("facts", w)
    n_rows = sum(len(c.rows) for c in collections)
    n_tools = sum(len(c.tools) for c in collections)
    report.notes["facts"] = f"{len(collections)} collection(s), {n_rows} row(s), {n_tools} fact tool(s)"
    return collections


# ===================================================================== evals


def load_golden(project: Project) -> list[dict]:
    if not project.golden_path.exists():
        return []
    data = yaml.safe_load(project.golden_path.read_text(encoding="utf-8")) or []
    return data if isinstance(data, list) else []


def check_golden(project: Project, report: Report, collections: list[facts.Collection]) -> None:
    if not project.golden_path.exists():
        report.warn("evals", f"no {project.rel(project.golden_path)}. Add ~10 questions you know the answers to")
        return
    try:
        items = load_golden(project)
    except yaml.YAMLError as exc:
        report.error("evals", f"golden.yaml is not valid YAML: {exc}")
        return
    params: dict[str, set[str]] = {"search_wiki": {"query", "limit"}, "read_wiki_page": {"path_or_slug", "max_chars"}}
    for c in collections:
        for t in c.tools:
            if t.kind == "lookup":
                params[t.name] = {t.arg, "limit"}
            else:
                params[t.name] = {f.param for f in t.filters} | {"limit"} | ({"upcoming_only"} if t.upcoming else set())
    slugs = _slugs(project)
    seen: set[str] = set()
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            report.error("evals", f"item {i}: must be a mapping")
            continue
        iid = str(item.get("id") or f"#{i}")
        if iid in seen:
            report.error("evals", f"{iid}: duplicate id")
        seen.add(iid)
        if not item.get("question"):
            report.error("evals", f"{iid}: missing question")
        cat = item.get("category")
        if cat not in GOLDEN_CATEGORIES:
            report.error("evals", f"{iid}: category must be one of {sorted(GOLDEN_CATEGORIES)}")
        tool = item.get("must_call_tool")
        if tool and collections and tool not in params:
            report.error("evals", f"{iid}: must_call_tool {tool!r} is not a tool (have: {sorted(params)})")
        if tool and tool in params:
            for k in (item.get("tool_args") or {}):
                if k not in params[tool]:
                    report.error("evals", f"{iid}: tool_args has {k!r}, but {tool} takes {sorted(params[tool])}")
        if cat == "hard_fact" and tool and not item.get("tool_args") and not item.get("must_contain"):
            report.warn("evals", f"{iid}: add tool_args + must_contain so ./gt eval can check the data offline")
        page = item.get("must_cite_page")
        if page:
            p = str(page).lower().removeprefix("wiki/").removesuffix(".md")
            if p not in slugs and p.rsplit("/", 1)[-1] not in slugs:
                report.error("evals", f"{iid}: must_cite_page {page!r} is not a wiki page")
    report.notes["evals"] = f"{len(items)} golden question(s)"


# ===================================================================== all


def run(project: Project) -> Report:
    report = Report()
    collections = check_facts(project, report)
    check_wiki(project, report)
    check_prose(project, report)
    check_golden(project, report, collections)
    if not project.prompt_path.exists():
        report.warn("prompt", f"no {project.rel(project.prompt_path)}; live evals will use the server's built-in "
                              "instructions")
    return report
