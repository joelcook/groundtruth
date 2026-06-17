#!/usr/bin/env python3
"""The linter that makes "facts come from tools" a build rule, not a hope.

It scans every public wiki page and FAILS if a calendar date (e.g. "July 4,
2026", including ranges) or a dollar amount ("$0.10") appears in the prose —
because that is a fact that should have been pulled from a tool at answer time,
not baked into a page that quietly goes stale.

Allowed, and skipped:
  * YAML frontmatter (dates like last_compiled live there)
  * the "## Sources & Lineage" table at the bottom (provenance, by design)
  * a justified escape hatch on the offending line or the line above it:
        <!-- fact-ok: this is a stable, illustrative figure, not a live price -->

Exit 0 if clean, 1 if any unjustified hard fact is found. Wire it into CI and
the refresh pipeline. This single check is most of what keeps the bot honest.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = REPO_ROOT / "wiki"

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_RE = re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:\s*[–-]\s*\d{{1,2}})?,\s*\d{{4}}\b")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MONEY_RE = re.compile(r"\$\s?\d")
FACT_OK_RE = re.compile(r"<!--\s*fact-ok:")
FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    return FRONTMATTER_RE.sub("", text)


def _strip_lineage(text: str) -> str:
    return re.split(r"\n##\s+Sources\s+&\s+Lineage\b", text, maxsplit=1, flags=re.I)[0]


def lint_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    body = _strip_lineage(_strip_frontmatter(text))
    lines = body.splitlines()
    violations: list[str] = []
    for i, line in enumerate(lines):
        if FACT_OK_RE.search(line):
            continue
        prev = lines[i - 1] if i > 0 else ""
        if FACT_OK_RE.search(prev):
            continue
        for label, rx in (("date", DATE_RE), ("iso-date", ISO_DATE_RE), ("money", MONEY_RE)):
            m = rx.search(line)
            if m:
                try:
                    rel = path.relative_to(REPO_ROOT)
                except ValueError:
                    rel = path
                violations.append(f"{rel}: {label} {m.group(0)!r} in prose — route it through a tool")
    return violations


def main() -> int:
    pages = [p for p in sorted(WIKI_DIR.rglob("*.md"))
             if "internal" not in p.relative_to(WIKI_DIR).parts]
    all_violations: list[str] = []
    for page in pages:
        all_violations.extend(lint_file(page))
    if all_violations:
        print("no-hard-facts lint FAILED:", file=sys.stderr)
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        print(f"\n{len(all_violations)} hard fact(s) found in prose across "
              f"{len(pages)} page(s).", file=sys.stderr)
        return 1
    print(f"no-hard-facts lint OK — {len(pages)} page(s) clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
