"""Source review: green checks must mean your facts match your current evidence.

``sources.lock.yaml`` (committed, in the project) records the content hash of
every source as of the last time someone reconciled the facts and wiki with it.
When ``./gt ingest`` (or a hand edit) changes a source that facts or wiki pages
cite, ``./gt check`` fails and names exactly what to re-check:

    ./gt review          # what changed, and which facts files and pages depend on it
    ./gt review --done   # I've reconciled them: record the current versions

Hashes, not dates, so a change made five minutes after the last review is still
caught. ``./gt init`` writes the lock for you; projects without one get a
warning instead of an error.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from groundtruth import checks, facts
from groundtruth.project import Project

HEADER = (
    "# The version of each source your facts and wiki were last reviewed against.\n"
    "# Written by `./gt review --done`. Don't edit by hand.\n"
)


@dataclass
class Change:
    source: str
    reason: str  # "changed" | "not yet reviewed"
    facts: list[str] = field(default_factory=list)
    wiki: list[str] = field(default_factory=list)


def lock_path(project: Project) -> Path:
    return project.root / "sources.lock.yaml"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def current(project: Project) -> dict[str, str]:
    if not project.pages_dir.exists():
        return {}
    return {project.rel(p): file_hash(p) for p in sorted(project.pages_dir.rglob("*.md"))}


def load_lock(project: Project) -> dict[str, str] | None:
    path = lock_path(project)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def dependents(project: Project) -> dict[str, dict[str, list[str]]]:
    """source path -> {"facts": [facts files], "wiki": [pages]} that cite it."""
    out: dict[str, dict[str, list[str]]] = {}

    def add(src: str, kind: str, who: str) -> None:
        entry = out.setdefault(src, {"facts": [], "wiki": []})
        if who not in entry[kind]:
            entry[kind].append(who)

    try:
        for c in facts.load(project):
            who = project.rel(c.file)
            if c.source_path:
                add(c.source_path, "facts", who)
            for row in c.rows:
                nested = row.get("source") if isinstance(row.get("source"), dict) else {}
                path = row.get("source_path") or nested.get("path")
                if path:
                    add(str(path), "facts", who)
    except facts.FactsError:
        pass
    for page in checks.wiki_pages(project):
        meta, _ = checks.split_frontmatter(page.read_text(encoding="utf-8"))
        for src in (meta or {}).get("sources") or []:
            add(str(src).strip("[]").split("|")[0], "wiki", project.rel(page))
    return out


def changes(project: Project) -> list[Change]:
    """Cited sources whose content differs from the last review. Empty without a lock."""
    lock = load_lock(project)
    if lock is None:
        return []
    now = current(project)
    out = []
    for src, deps in sorted(dependents(project).items()):
        if src not in now:
            continue  # a missing source is reported by ./gt check as a warning
        if src not in lock:
            out.append(Change(src, "not yet reviewed", deps["facts"], deps["wiki"]))
        elif lock[src] != now[src]:
            out.append(Change(src, "changed", deps["facts"], deps["wiki"]))
    return out


def mark_reviewed(project: Project, paths: list[str] | None = None) -> list[str]:
    """Record the current version of the given sources (default: all). Returns what was recorded."""
    now = current(project)
    lock = load_lock(project) or {}
    targets = list(now) if not paths else [project.rel(Path(p) if Path(p).is_absolute() else project.root / p)
                                           for p in paths]
    recorded = []
    for src in targets:
        if src in now:
            lock[src] = now[src]
            recorded.append(src)
    lock = {k: v for k, v in sorted(lock.items()) if k in now}
    lock_path(project).write_text(HEADER + yaml.safe_dump(lock, sort_keys=True), encoding="utf-8")
    return recorded
