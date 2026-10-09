"""Template examples: fictional content that's clearly marked and easy to remove.

Templates ship with example facts, pages, sources, and eval questions so a new
project works on day one. Every one of them is marked ``example: true`` (top of a
facts file, a page's or source's frontmatter, a golden question). Until they're
gone, ``./gt check`` warns that fictional data is live.

    ./gt examples            # what's still example content
    ./gt examples --remove   # delete all of it (then add your own: ./gt compile)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from groundtruth import checks, facts
from groundtruth.project import Project

EXAMPLE_LINE = re.compile(r"(?m)^\s+example:\s*true\s*$")


@dataclass
class Found:
    facts: list[Path] = field(default_factory=list)
    wiki: list[Path] = field(default_factory=list)
    sources: list[Path] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.facts) + len(self.wiki) + len(self.sources) + len(self.questions)


def _flagged(path: Path) -> bool:
    meta, _ = checks.split_frontmatter(path.read_text(encoding="utf-8"))
    return bool((meta or {}).get("example"))


def find(project: Project) -> Found:
    found = Found()
    for path in facts.fact_files(project):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and data.get("example"):
            found.facts.append(path)
    found.wiki = [p for p in checks.wiki_pages(project) if _flagged(p)]
    if project.pages_dir.exists():
        found.sources = [p for p in sorted(project.pages_dir.rglob("*.md")) if _flagged(p)]
    try:
        found.questions = [str(i.get("id")) for i in checks.load_golden(project)
                           if isinstance(i, dict) and i.get("example")]
    except yaml.YAMLError:
        pass
    return found


def _remove_questions(project: Project) -> int:
    path = project.golden_path
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=- id:)", text)
    head, items = parts[0], parts[1:]
    kept = [b for b in items if not EXAMPLE_LINE.search(b)]
    path.write_text(head + "".join(kept), encoding="utf-8")
    return len(items) - len(kept)


def remove(project: Project) -> Found:
    found = find(project)
    for path in [*found.facts, *found.wiki, *found.sources]:
        path.unlink()
    _remove_questions(project)
    index = project.wiki_dir / "index.md"
    if index.exists() and found.wiki:
        slugs = {p.stem for p in found.wiki}
        lines = [ln for ln in index.read_text(encoding="utf-8").splitlines()
                 if not any(f"[[{s}]]" in ln for s in slugs)]
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return found
