#!/usr/bin/env python3
"""Structural wiki checks — cheap, deterministic, run in CI and the refresh loop.

Validates the things a compiled page must always have, so the corpus can't rot
silently:
  * required frontmatter keys (title, page_type, sources, status, ...)
  * a known page_type
  * a "## Sources & Lineage" section (the audit trail)
  * every [[wikilink]] resolves to a real page (or is a known external like a
    sources/ path)

Exit 0 if clean, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = REPO_ROOT / "wiki"

REQUIRED_KEYS = {"title", "page_type", "sources", "status", "last_compiled"}
VALID_TYPES = {"journey", "process", "policy", "entity", "comparison", "internal"}
SKIP = {"index.md", "log.md", "gaps.md"}
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _slugs() -> set[str]:
    out = set()
    for p in WIKI_DIR.rglob("*.md"):
        if p.name in SKIP:
            continue
        out.add(p.stem.lower())
        out.add(p.relative_to(WIKI_DIR).as_posix().lower().removesuffix(".md"))
    return out


def main() -> int:
    slugs = _slugs()
    errors: list[str] = []
    pages = [p for p in sorted(WIKI_DIR.rglob("*.md")) if p.name not in SKIP]

    for path in pages:
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            errors.append(f"{rel}: missing YAML frontmatter")
            continue
        meta = yaml.safe_load(m.group(1)) or {}

        missing = REQUIRED_KEYS - set(meta)
        if missing:
            errors.append(f"{rel}: missing frontmatter keys {sorted(missing)}")
        ptype = str(meta.get("page_type", "")).lower()
        if ptype and ptype not in VALID_TYPES:
            errors.append(f"{rel}: unknown page_type {ptype!r}")
        if "## Sources & Lineage" not in text and "## Sources &amp; Lineage" not in text:
            errors.append(f"{rel}: missing 'Sources & Lineage' section")

        for target in WIKILINK_RE.findall(text):
            t = target.strip().lower()
            if t.startswith("sources/") or t.startswith("raw/"):
                continue  # link into the evidence layer
            t = t.removeprefix("wiki/").removesuffix(".md")
            if t not in slugs and t.rsplit("/", 1)[-1] not in slugs:
                errors.append(f"{rel}: broken wikilink [[{target}]]")

    if errors:
        print("wiki structure check FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print(f"wiki structure OK — {len(pages)} page(s), all wikilinks resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
