#!/usr/bin/env python3
"""Recompile stale wiki pages from their frozen sources — governed by review.

This is the one step in the pipeline that *authors* prose with an LLM. It is
deliberately fenced in:
  * Default run is a DRY RUN (prints what it would do). ``--write`` is required
    to touch disk.
  * Even with ``--write`` it only writes files; it never commits to main. The
    refresh pipeline runs it on a ``refresh/<date>`` branch a human reviews.
  * A page is recompiled from the sources listed in its own frontmatter, and the
    output must still pass the no-hard-facts linter — facts stay in the database.

Staleness (default): ``last_compiled`` older than GT_STALE_DAYS (default 30).
On a fresh clone nothing is stale, so the nightly agent does nothing and spends
nothing — which is exactly the behavior you want most nights.

    python scripts/recompile.py --list-stale
    python scripts/recompile.py --stale --write          # needs ANTHROPIC_API_KEY
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = REPO_ROOT / "wiki"
STALE_DAYS = int(os.environ.get("GT_STALE_DAYS", "30"))
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

COMPILE_SYSTEM = """You compile a single knowledge-wiki page from frozen source
documents. Hard rules:
- Keep the YAML frontmatter; update last_compiled to today.
- NO dates or dollar amounts in the prose — those are served by tools at answer
  time. Refer readers to the tool (e.g. get_fee, get_event) instead of a number.
- Use [[wikilinks]] for cross-references; keep the existing 'Sources & Lineage'
  table accurate.
- Plain, friendly, 8th-grade reading level. Output ONLY the page markdown."""


def _meta(path: Path) -> dict:
    m = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    return (yaml.safe_load(m.group(1)) or {}) if m else {}


def find_stale() -> list[tuple[Path, str]]:
    today = dt.date.today()
    stale: list[tuple[Path, str]] = []
    for path in sorted(WIKI_DIR.rglob("*.md")):
        if path.name in {"index.md", "log.md", "gaps.md"}:
            continue
        meta = _meta(path)
        lc = meta.get("last_compiled")
        if not lc:
            stale.append((path, "no last_compiled"))
            continue
        try:
            age = (today - dt.date.fromisoformat(str(lc))).days
        except ValueError:
            stale.append((path, f"unparseable last_compiled {lc!r}"))
            continue
        if age > STALE_DAYS:
            stale.append((path, f"last_compiled {age} days ago"))
    return stale


def compile_page(path: Path) -> str:
    """Regenerate one page from its sources via Claude. Requires ANTHROPIC_API_KEY."""
    import anthropic

    meta = _meta(path)
    sources = meta.get("sources") or []
    if not isinstance(sources, list):
        sources = [sources]
    evidence = []
    for s in sources:
        sp = REPO_ROOT / str(s).strip("[]").split("|")[0]
        if sp.exists():
            evidence.append(f"### {sp.relative_to(REPO_ROOT)}\n{sp.read_text(encoding='utf-8')}")
    client = anthropic.Anthropic()
    model = os.environ.get("RECOMPILE_MODEL", "claude-opus-4-8")
    user = (f"Current page:\n\n{path.read_text(encoding='utf-8')}\n\n"
            f"Frozen sources:\n\n" + "\n\n".join(evidence))
    resp = client.messages.create(
        model=model, max_tokens=4096,
        system=[{"type": "text", "text": COMPILE_SYSTEM}],
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list-stale", action="store_true")
    ap.add_argument("--stale", action="store_true", help="recompile all stale pages")
    ap.add_argument("--write", action="store_true", help="write changes to disk")
    ap.add_argument("--branch", help="(informational) review branch the writes land on")
    args = ap.parse_args()

    stale = find_stale()

    if args.list_stale:
        if not stale:
            print("No stale pages.")
        else:
            for path, reason in stale:
                print(f"{path.relative_to(REPO_ROOT)}  — {reason}")
        return 0

    if not args.stale:
        print("Dry run. Stale pages that --stale --write would recompile:")
        for path, reason in stale:
            print(f"  {path.relative_to(REPO_ROOT)}  — {reason}")
        print("(none)" if not stale else "")
        return 0

    if not stale:
        print("No stale pages to recompile.")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY required to recompile; "
              f"{len(stale)} page(s) are stale.", file=sys.stderr)
        return 1

    for path, reason in stale:
        print(f"recompiling {path.relative_to(REPO_ROOT)} ({reason})", file=sys.stderr)
        new_text = compile_page(path)
        if args.write:
            path.write_text(new_text, encoding="utf-8")
        else:
            print(new_text)
    print(f"recompiled {len(stale)} page(s)" + (" (written)" if args.write else " (dry run)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
