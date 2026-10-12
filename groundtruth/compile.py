"""``./gt compile``: turn evidence into wiki pages and fact rows.

Two ways to run it:

    ./gt compile            writes build/compile-packet.md: a precise work order for
                            a coding agent. Open the repo in Claude Code (or Codex,
                            Cursor...) and say "do build/compile-packet.md". It lists
                            new sources no page covers yet, the exact values found in
                            each (the fact candidates), pages whose sources changed,
                            starter eval questions, the rules, and how to verify. It
                            also writes a draft page skeleton per new source
                            (status: draft, not served until you finish it).
                            No API key needed.

    ./gt compile --write    does the wiki part itself with Claude: drafts a page for
                            each uncovered source and refreshes each stale page.
                            Needs ANTHROPIC_API_KEY. Writes files only; review them
                            (git diff) and run ./gt check.

Facts stay a human (or agent) decision: the packet tells the agent exactly how to
add rows to facts/*.yaml, and ./gt check verifies them.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from groundtruth import checks, facts
from groundtruth.project import Project

STALE_DAYS = int(os.environ.get("GT_STALE_DAYS", "0"))  # 0 = only flag pages whose sources changed
CANDIDATES_SHOWN = 60  # per source in the packet; the count is always shown in full
TABLE_RULE_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


@dataclass
class SourceInfo:
    path: Path
    rel: str
    title: str
    url: str | None
    captured: dt.date | None


def _frontmatter(path: Path) -> dict:
    meta, _ = checks.split_frontmatter(path.read_text(encoding="utf-8"))
    return meta or {}


def _date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def sources(project: Project) -> list[SourceInfo]:
    if not project.pages_dir.exists():
        return []
    out = []
    for path in sorted(project.pages_dir.rglob("*.md")):
        meta = _frontmatter(path)
        captured = _date(meta.get("captured") or meta.get("scraped_at"))
        if captured is None:
            captured = dt.date.fromtimestamp(path.stat().st_mtime)
        out.append(SourceInfo(path=path, rel=project.rel(path), url=meta.get("source_url"),
                              title=str(meta.get("title") or path.stem.replace("-", " ").title()),
                              captured=captured))
    return out


def _is_draft(path: Path) -> bool:
    return str(_frontmatter(path).get("status", "")).lower() == "draft"


def cited_sources(project: Project, *, drafts: bool = False) -> set[str]:
    """Every source path referenced by a wiki page or a facts file (draft pages only if asked)."""
    cited: set[str] = set()
    for page in checks.wiki_pages(project):
        if not drafts and _is_draft(page):
            continue
        for s in _frontmatter(page).get("sources") or []:
            cited.add(str(s).strip("[]").split("|")[0])
    try:
        for c in facts.load(project):
            if c.source_path:
                cited.add(c.source_path)
            for r in c.rows:
                if r.get("source_path"):
                    cited.add(str(r["source_path"]))
    except facts.FactsError:
        pass
    return cited


def _retired(path: Path) -> bool:
    meta = _frontmatter(path)
    return (meta.get("canonical") is False or bool(meta.get("removed_upstream"))
            or str(meta.get("status", "")).lower() in {"retired", "superseded", "archived"})


def uncovered(project: Project) -> list[SourceInfo]:
    """Sources no wiki page or facts file cites yet (retired/superseded sources excluded)."""
    cited = cited_sources(project)
    return [s for s in sources(project) if s.rel not in cited and not _retired(s.path)]


@dataclass
class Staleness:
    """Every reason a page may be out of date. ``./gt rewrite`` needs them all, not just the first."""
    changed: list[str]       # cited sources that changed since the last review (sources.lock.yaml)
    newer: list[str]         # cited sources captured after the page was last compiled
    compiled: dt.date | None  # the page's last_compiled (None: missing)
    age_days: int | None     # set when older than GT_STALE_DAYS

    def reason(self) -> str | None:
        if self.changed:
            return f"source changed since the last review: {', '.join(self.changed)}"
        if self.compiled is None:
            return "no last_compiled date"
        if self.newer:
            return f"source changed since {self.compiled}: {', '.join(self.newer)}"
        if self.age_days is not None:
            return f"compiled {self.age_days} days ago"
        return None


def page_sources(page: Path) -> list[str]:
    """The source paths a page cites, as written in its frontmatter (wikilink brackets removed)."""
    return [str(s).strip("[]").split("|")[0] for s in (_frontmatter(page).get("sources") or [])]


def staleness(project: Project, page: Path, changed: set[str], by_rel: dict[str, SourceInfo]) -> Staleness:
    cited = page_sources(page)
    compiled = _date(_frontmatter(page).get("last_compiled"))
    newer = [s for s in cited if compiled and (src := by_rel.get(s)) and src.captured and src.captured > compiled]
    age = (dt.date.today() - compiled).days if compiled else None
    return Staleness(changed=[s for s in cited if s in changed], newer=newer, compiled=compiled,
                     age_days=age if age is not None and STALE_DAYS and age > STALE_DAYS else None)


def served_pages(project: Project) -> list[Path]:
    """Published pages outside wiki/internal/: the ones staleness applies to."""
    return [p for p in checks.wiki_pages(project)
            if "internal" not in p.relative_to(project.wiki_dir).parts and not _is_draft(p)]


def stale_pages(project: Project) -> list[tuple[Path, str]]:
    from groundtruth import review
    changed = {c.source for c in review.changes(project)}
    by_rel = {s.rel: s for s in sources(project)}
    out = []
    for page in served_pages(project):
        why = staleness(project, page, changed, by_rel).reason()
        if why:
            out.append((page, why))
    return out


# ===================================================================== scaffolding


def candidates(path: Path) -> list[tuple[int, str, str]]:
    """(line, kind, the line itself) for each line of a source with an exact value in it.

    These are the fact candidates: each one belongs in a facts/ row, not in wiki prose.
    It uses the same patterns as the prose check, so it finds common formats, not all.
    """
    text = path.read_text(encoding="utf-8")
    m = checks.FRONTMATTER_RE.match(text)
    offset = text[: m.end()].count("\n") if m else 0
    out = []
    lines = (text[m.end():] if m else text).splitlines()
    header: list[str] | None = None
    for i, line in enumerate(lines):
        row = line.strip()
        if row.startswith("|"):  # a markdown table: each row with a number becomes "Column: value; ..."
            cells = [c.strip() for c in row.strip("|").split("|")]
            if TABLE_RULE_RE.match(row):
                continue
            if header is None and i + 1 < len(lines) and TABLE_RULE_RE.match(lines[i + 1].strip()):
                header = cells
                continue
            if header and any(re.search(r"\d", c) for c in cells):
                pairs = [f"{h}: {c}" if h else c for h, c in zip(header, cells) if c]
                out.append((i + 1 + offset, "table row", "; ".join(pairs)))
                continue
        else:
            header = None
        kinds = [k for k, rx in checks.PATTERNS if rx.search(line)]
        if kinds:
            clean = " ".join(line.strip().lstrip("#*-> ").split())
            out.append((i + 1 + offset, ", ".join(dict.fromkeys(kinds)), clean))  # whole line: never cut a value off
    return out


def _yaml_value(value: str) -> str:
    """A string as a safe one-line YAML scalar (quoted when it needs to be: colons, #, quotes...)."""
    import yaml
    return yaml.safe_dump(value, allow_unicode=True, width=10_000).removesuffix("\n...\n").strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "page"


def write_drafts(project: Project) -> list[Path]:
    """A page skeleton for each source nothing covers yet. Status draft: not served, not in evals.

    Finishing one means writing the explanation, picking page_type, and setting
    status: published. Re-running skips sources that already have a draft.
    """
    drafted = cited_sources(project, drafts=True)
    today = dt.date.today().isoformat()
    out = []
    for src in uncovered(project):
        if src.rel in drafted:
            continue
        slug = _slug(src.title)
        target = project.wiki_dir / "drafts" / f"{slug}.md"
        n = 2
        while target.exists():
            target, n = target.with_name(f"{slug}-{n}.md"), n + 1
        words = [w for w in re.findall(r"[a-z0-9]+", src.title.lower()) if len(w) > 2][:6]
        captured = src.captured.isoformat() if src.captured else today
        where = src.url or str(_frontmatter(src.path).get("source_path") or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"""---
title: {_yaml_value(src.title)}
page_type: guide
last_compiled: {today}
compiled_by: ./gt compile (draft)
sources:
  - {src.rel}
status: draft
tags: [{", ".join(words)}]
---

# {src.title}

<!-- DRAFT. Not served until you finish it:
     1. Write the explanation from {src.rel} in plain words. Lead with the answer.
     2. Exact values (prices, dates, hours, limits, contacts) go in facts/, not here.
        Name the tool instead: "the assistant looks up the current fee with get_fee".
        build/compile-packet.md lists the exact values found in this source.
     3. Set page_type (journey, process, policy, entity, comparison, faq, guide),
        add the words people would search for to tags, set status: published,
        and move it out of drafts/ if you like. Then run ./gt check. -->

## Sources & Lineage

| Source | Captured | URL |
|---|---|---|
| [[{src.rel}]] | {captured} | {where} |
""", encoding="utf-8")
        out.append(target)
    return out


PROPOSAL_PREFIX = "_proposed-"


def _proposal_path(project: Project, src: SourceInfo) -> Path:
    """facts/_proposed-<name>.yaml, named after the source without ingest's collision suffix when that's free."""
    for stem in dict.fromkeys([re.sub(r"-[0-9a-f]{6}$", "", _slug(src.path.stem)), _slug(src.path.stem)]):
        target = project.facts_dir / f"{PROPOSAL_PREFIX}{stem}.yaml"
        if (project.facts_dir / f"{stem}.yaml").exists():
            continue
        if not target.exists() or f"from {src.rel}" in target.read_text(encoding="utf-8").split("\n", 1)[0]:
            return target
    return project.facts_dir / f"{PROPOSAL_PREFIX}{_slug(src.path.stem)}.yaml"


def proposals(project: Project) -> list[Path]:
    """Proposed facts files waiting for review (facts/_proposed-*.yaml; the leading _ keeps them offline)."""
    return sorted(project.facts_dir.glob(f"{PROPOSAL_PREFIX}*.yaml")) if project.facts_dir.exists() else []


def live_name(proposal: Path) -> Path:
    """facts/_proposed-pricing.yaml -> facts/pricing.yaml: where it goes to become live."""
    return proposal.with_name(proposal.name.removeprefix(PROPOSAL_PREFIX))


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _proposal_header(path: Path) -> tuple[dict[str, str], str]:
    """The ``# key: value`` lines compile wrote at the top, and the YAML below them."""
    meta, body = {}, []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        m = re.match(r"# (source|source-sha256|body-sha256): (.*)$", line.rstrip("\n"))
        if m:
            meta[m.group(1)] = m.group(2).strip()
        elif not line.startswith("#") or body:
            body.append(line)
    return meta, "".join(body)


def _source_sha(project: Project, rel: str) -> str | None:
    path = project.root / rel
    return str(_frontmatter(path).get("sha256") or "") or None if path.exists() else None


def stale_proposals(project: Project) -> list[Path]:
    """Proposals whose source changed after they were written (and that someone has since edited)."""
    out = []
    for p in proposals(project):
        meta, _ = _proposal_header(p)
        now = _source_sha(project, meta.get("source", ""))
        if meta.get("source-sha256") and now and now != meta["source-sha256"]:
            out.append(p)
    return out


def _statement_query(text: str, value: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", text.replace(value, " "))
             if w.lower() not in facts.STOPWORDS and len(w) > 2]
    return " ".join(words[:3]).lower()


def write_fact_proposals(project: Project) -> list[Path]:
    """A ready-to-review facts file per new source: one row per line with an exact value in it.

    Each row is the exact statement from the source, served by a lookup tool, with the
    source wired in. It's written as facts/_proposed-<source>.yaml, which the loader
    ignores. Check it, rename it to facts/<source>.yaml, and it's live.
    Reshaping rows into real fields (price_usd: 39) is better still; this gets you there.
    """
    import yaml
    out = []
    taken = set()
    try:
        taken = {t.name for c in facts.load(project) for t in c.tools}
    except facts.FactsError:
        pass
    for p in proposals(project):
        try:
            taken |= {t["name"] for t in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("tools") or []}
        except (yaml.YAMLError, AttributeError, TypeError):
            pass
    for src in uncovered(project):
        found = candidates(src.path)
        target = _proposal_path(project, src)
        stem = target.name.removeprefix(PROPOSAL_PREFIX).removesuffix(".yaml")
        if not found or live_name(target).exists():
            continue
        if target.exists():
            meta, body = _proposal_header(target)
            untouched = meta.get("body-sha256") == _sha(body)
            changed = meta.get("source-sha256") != _source_sha(project, src.rel)
            if not (changed and untouched):
                continue  # current, or edited by someone: stale_proposals() flags the edited-and-stale case
            for t in (yaml.safe_load(body) or {}).get("tools") or []:
                taken.discard(t.get("name"))
        base = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
        tool = f"get_{base}_fact"[:64] if base and base[0].isalpha() else f"get_fact_{base}"[:64]
        n = 2
        while tool in taken:
            tool, n = f"get_{base}_fact_{n}"[:64], n + 1
        taken.add(tool)
        rows, ids = [], set()
        for line, kind, text in found:
            rid = _slug(" ".join(text.split()[:6])) or f"line-{line}"
            while rid in ids:
                rid = f"{rid}-{line}"
            ids.add(rid)
            rows.append({"id": rid, "statement": text})
        source = {"path": src.rel}
        origin = src.url or _frontmatter(src.path).get("source_path")
        if src.url:
            source["url"] = src.url
        data = {
            "description": f"Exact statements from {src.title}.",
            "key": "id",
            "source": source,
            "tools": [{
                "name": tool, "kind": "lookup", "arg": "topic",
                "description": (f"Exact statements from {src.title} (prices, dates, limits, contacts). "
                                f"Quote the statement and cite its source."),
            }],
            "rows": rows,
        }
        body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=10_000)  # one line per statement
        header = (f"# PROPOSED by ./gt compile from {src.rel}" + (f" ({origin})" if origin else "") + ".\n"
                  "# Not live yet: files whose names start with _ are ignored.\n"
                  "# To make it live: check every row against the source, delete what isn't a fact,\n"
                  "# give the tool a sharper name and description, then rename this file to\n"
                  f"# {project.rel(live_name(target))}. Better still, reshape rows into real fields\n"
                  "# (price_usd: 39) as shown in docs/facts.md.\n"
                  f"# source: {src.rel}\n"
                  f"# source-sha256: {_source_sha(project, src.rel) or ''}\n"
                  f"# body-sha256: {_sha(body)}\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(header + body, encoding="utf-8")
        out.append(target)
    return out


# ===================================================================== the packet


RULES = """\
1. **Facts never go in wiki prose.** Dates, times, dollar amounts, phone numbers,
   emails, hours, deadlines, limits: put them in `facts/*.yaml` and refer to the
   tool in prose ("the assistant looks up the current price with `get_fee`").
   `./gt check` fails on the common ways of writing these; the rule covers the
   rest too.
2. **Every fact row has a source.** Use the file-level `source: {path, url}` or a
   per-row `source_url`. Point at the `sources/pages/...` file it came from.
3. **Every wiki page has frontmatter and a lineage table** (for local files with
   no URL, put the source path in the URL column):
   ```markdown
   ---
   title: How to reset your password
   page_type: process        # journey | process | policy | entity | comparison | faq | guide
   last_compiled: {today}
   compiled_by: <you>
   sources:
     - sources/pages/password-help.md
   status: published
   tags: [password, login, account]
   ---

   # How to reset your password
   ...plain, friendly prose, 8th-grade reading level...

   ## Sources & Lineage

   | Source | Captured | URL |
   |---|---|---|
   | [[sources/pages/password-help.md]] | <captured date> | <source_url> |
   ```
4. **Link pages** with `[[slug]]`. Put pages in the folder for their type:
   `wiki/journeys/`, `wiki/processes/`, `wiki/policies/`, `wiki/entities/`,
   `wiki/comparisons/`, `wiki/faqs/`, or `wiki/guides/` (e.g.
   `wiki/processes/reset-password.md`), and list new pages in `wiki/index.md`.
5. **Don't edit `sources/`.** It's the evidence; `./gt ingest` owns it.
6. **Add eval questions** to `evals/golden.yaml` for what you added: a `hard_fact`
   item (with `must_call_tool`, `tool_args`, and `must_contain` or `must_match`)
   for each fact tool, and a `narrative` item (`must_cite_page`) for each page.
   Replace any template example questions (`example: true`). `./gt check` warns
   about tools no question exercises.
7. **The prompt.** If `prompts/system_prompt.md` contains `{{FACT_TOOLS}}`, its
   routing table is generated from `facts/` and you don't touch it. Otherwise
   update its table. Rewrite its scope and refusal rules for this organization
   (what's personal/account data it must redirect, and where to).
8. **Template examples** (`example: true` in facts files, pages, sources, and
   questions) are fictional. Remove them once real content replaces them:
   `./gt examples --remove`.
"""

FACTS_HOWTO = """\
A collection is one YAML file. Copy this shape (for a local document with no
URL, `source: {path: sources/pages/pricing.md}` is enough):

```yaml
# facts/plans.yaml
description: Subscription plans with prices and limits.
key: id                       # unique per row
source: {path: sources/pages/pricing.md, url: https://example.org/pricing}
tools:
  - name: get_plan            # lowercase_snake_case, unique across collections
    kind: lookup              # lookup(<arg>) does a forgiving text match
    arg: plan
    description: Price and limits for a plan. Every price the assistant states MUST come from here.
  - name: list_plans
    kind: list                # list with optional filters
    filters: [billing]
    description: List all plans, optionally by billing period.
rows:
  - {id: starter, name: Starter, aliases: [basic], price_usd: 9, billing: monthly, projects: 3}
```

Dated rows: add `date_field: date` with ISO `YYYY-MM-DD` values, and `upcoming: true`
on a list tool to get "upcoming only". Full reference: docs/facts.md.
"""


def _where(project: Project) -> str:
    from groundtruth.project import REPO_ROOT
    try:
        return str(project.root.relative_to(REPO_ROOT))
    except ValueError:
        return str(project.root)


def packet(project: Project) -> Path:
    from groundtruth import review
    today = dt.date.today().isoformat()
    new = uncovered(project)
    stale = stale_pages(project)
    changed = review.changes(project)
    try:
        collections = facts.load(project)
        coll_lines = [f"- `{Path(c.file).name}`: {c.description or c.name} "
                      f"({len(c.rows)} rows; tools: {', '.join(t.name for t in c.tools)})" for c in collections]
    except facts.FactsError as exc:
        coll_lines = ["- (facts currently fail validation:)"] + [f"  - {p}" for p in exc.problems]
    pages = [project.rel(p) for p in checks.wiki_pages(project) if not _is_draft(p)]
    drafts = {str(s).strip("[]"): p for p in checks.wiki_pages(project) if _is_draft(p)
              for s in (_frontmatter(p).get("sources") or [])}
    gt = f"./gt -p {_where(project)}"

    lines = [
        f"# Compile packet: {project.name}",
        "",
        f"Generated {today} by `./gt compile`. You are a coding agent working in this repo.",
        f"The project lives in `{_where(project)}/`: edit files only inside it. Run the `./gt`",
        "commands below from the repo root (the folder with the `gt` script).",
        "",
        "## Goal",
        "",
        "Turn the evidence in `sources/pages/` into (a) source-cited wiki pages for how/why",
        "questions and (b) fact rows in `facts/*.yaml` for anything exact. Then prove it",
        "with the checks below.",
        "",
        "## Work list",
        "",
    ]
    if changed:
        lines += [f"### Sources that changed since the last review ({len(changed)})", "",
                  "Facts and pages below were written against an older version of the source. Compare",
                  "them with the current source (`./gt review --diff` shows what changed), fix anything out of",
                  "date, then run `./gt review --done`. `./gt check` fails until you do.", ""]
        for ch in changed:
            lines.append(f"- `{ch.source}` ({ch.reason}): `{gt} review --diff {ch.source}`")
            lines += [f"  - facts: `{f}`" for f in ch.facts]
            lines += [f"  - wiki: `{w}`" for w in ch.wiki]
        lines.append("")
    if new:
        lines += [f"### New sources nothing covers yet ({len(new)})", "",
                  "For each: decide what's a fact (goes in `facts/`) and what's explanation (a wiki",
                  "page). Several related sources can share one page.", ""]
        for src in new:
            lines.append(f"- `{src.rel}`: {src.title}" + (f" ({src.url})" if src.url else ""))
            if src.rel in drafts:
                lines.append(f"  - draft page waiting for you: `{project.rel(drafts[src.rel])}`")
            proposal = _proposal_path(project, src)
            if proposal.exists():
                lines.append(f"  - proposed facts waiting for review: `{project.rel(proposal)}` (rename it to "
                             f"`{project.rel(live_name(proposal))}` to make it live)")
                if proposal in stale_proposals(project):
                    lines.append("    - ! the source changed after this was proposed, and the proposal has been "
                                 "edited since: compare every row with the current source")
            found = candidates(src.path)
            if found:
                lines.append(f"  - exact values in it ({len(found)} line(s); each is a facts row, not prose; "
                             f"full text in `build/fact-candidates.md`):")
                lines += [f"    - line {n} ({kind}): {text if len(text) <= 240 else text[:240] + ' [...]'}"
                          for n, kind, text in found[:CANDIDATES_SHOWN]]
                if len(found) > CANDIDATES_SHOWN:
                    lines.append(f"    - ...and {len(found) - CANDIDATES_SHOWN} more: the complete list is in "
                                 f"`build/fact-candidates.md`")
        lines.append("")
    if stale:
        lines += [f"### Pages whose sources changed ({len(stale)})", "",
                  "Re-read the sources, update the prose, bump `last_compiled`, and update any",
                  "facts the change affects.", ""]
        lines += [f"- `{project.rel(p)}`: {why}" for p, why in stale]
        lines.append("")
    from groundtruth import examples
    ex = examples.find(project)
    if ex.total:
        lines += [f"### Template examples still live ({ex.total})", "",
                  "These are fictional placeholders from the template. Once the evidence above is",
                  "turned into real facts, pages, and questions, delete them all with",
                  "`./gt examples --remove` (or delete them one by one). Don't leave fictional prices",
                  "or contacts in a live assistant.", ""]
        lines += [f"- `{project.rel(p)}`" for p in [*ex.facts, *ex.wiki, *ex.sources]]
        lines += [f"- `evals/golden.yaml`: question `{q}`" for q in ex.questions]
        lines.append("")
    if not new and not stale and not changed and not ex.total:
        lines += ["Nothing to do: every source is covered and no page is stale. Run `./gt ingest`",
                  "to pull fresh evidence first.", ""]
    lines += [
        "## Rules",
        "",
        RULES.replace("{today}", today),
        "## Facts files",
        "",
        "Current collections:",
        "",
        *(coll_lines or ["- (none yet)"]),
        "",
        FACTS_HOWTO,
        "## Existing wiki pages",
        "",
        *([f"- `{p}`" for p in pages] or ["- (none yet)"]),
        "",
        "## Eval questions to add",
        "",
        "Add these to `evals/golden.yaml` as you go (full format: docs/evals.md). One `hard_fact`",
        "per fact tool, one `narrative` per published page. Write the question the way a real",
        "person would ask it.",
        "",
        "```yaml",
        *_golden_stubs(project, drafts),
        "```",
        "",
        "## When you're done",
        "",
        "Run these from the repo root and fix everything they report:",
        "",
        "```bash",
        f"{gt} check          # fix everything except 'changed since the last review'",
        f"{gt} eval           # free: tools return the expected facts, search finds the pages",
        f"{gt} review --done  # only after the facts and pages match the current sources",
        f"{gt} check --production   # must be fully green: no examples, no unreviewed sources",
        "```",
        "",
        "Then summarize what you added and anything in the sources you couldn't place.",
        "",
    ]
    out = project.build_dir / "compile-packet.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    write_candidates(project, new)
    return out


def write_candidates(project: Project, srcs: list[SourceInfo]) -> Path:
    """build/fact-candidates.md: every line with an exact value, in every source nothing covers yet."""
    lines = ["# Fact candidates", "",
             "Every line in an uncovered source with a value the prose check recognizes (a price, a date,",
             "a duration, a limit, a contact), and every table row with a number in it. Each belongs in a",
             "facts/ row. The patterns cover common formats, not every possible one, so read the sources too.",
             ""]
    for src in srcs:
        found = candidates(src.path)
        lines += [f"## `{src.rel}` ({len(found)} line(s))", ""]
        lines += [f"- line {n} ({kind}): {text}" for n, kind, text in found] or ["- (none recognized)"]
        lines.append("")
    out = project.build_dir / "fact-candidates.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _golden_stubs(project: Project, drafts: dict[str, Path]) -> list[str]:
    from groundtruth import examples
    out = []
    fictional = {p.resolve() for p in examples.find(project).facts}
    try:
        collections = [c for c in facts.load(project) if Path(c.file).resolve() not in fictional]
    except facts.FactsError:
        collections = []
    for c in collections:
        for t in c.tools[:1]:
            out += [f"- id: {t.name.replace('_', '-')}-1", f"  question: \"TODO: a real question {t.name} answers\"",
                    "  category: hard_fact", f"  must_call_tool: {t.name}",
                    "  tool_args: {TODO: the arguments a model would pass}",
                    "  must_contain: [\"TODO: the exact value the answer must include\"]", ""]
    import yaml
    for prop in proposals(project):
        try:
            data = yaml.safe_load(prop.read_text(encoding="utf-8")) or {}
            tool, row = data["tools"][0]["name"], data["rows"][0]
        except (yaml.YAMLError, KeyError, IndexError, TypeError):
            continue
        value = next((rx.search(row["statement"]).group(0) for _, rx in checks.PATTERNS
                      if rx.search(row["statement"])), "")
        if not value:  # a table row: the first number is the value to check
            hit = re.search(r"(?<=: )\d[\d,.]*", row["statement"]) or re.search(r"\d[\d,.]*", row["statement"])
            value = hit.group(0).rstrip(".,") if hit else "TODO"
        out += [f"- id: {row['id']}"[:70] + f"          # once {prop.name} is live",
                f"  question: \"TODO: how a real person would ask this ({row['statement'][:60]})\"",
                "  category: hard_fact", f"  must_call_tool: {tool}",
                f"  tool_args: {{topic: {_statement_query(row['statement'], value) or 'TODO'}}}",
                f"  must_contain: [{json.dumps(value)}]", ""]
    if not collections and not proposals(project):
        out += ["- id: TODO-question-id                    # one like this per fact tool you add",
                "  question: \"TODO: a question a real person asks\"", "  category: hard_fact",
                "  must_call_tool: TODO_tool_name          # a tool from facts/*.yaml",
                "  tool_args: {TODO_arg: TODO_value}       # what a model would pass it",
                "  must_contain: [\"TODO: the exact value from the source\"]", ""]
    for page in sorted(set(drafts.values())):
        slug = page.stem
        out += [f"- id: {slug}", f"  question: \"TODO: how would someone ask about {_frontmatter(page).get('title', slug)}?\"",
                "  category: narrative", f"  must_cite_page: {slug}", ""]
    return out


# ===================================================================== compiling with Claude


SYSTEM = """You write one page of a knowledge wiki from captured source documents.
Hard rules:
- Output ONLY the page markdown, starting with the YAML frontmatter.
- NO dates, times, dollar amounts, phone numbers, or emails in the prose. Those are
  served by fact tools at answer time; mention the tool instead (for example
  "the assistant looks up the current fee with `get_fee`"). Available fact tools
  are listed in the request.
- Frontmatter keys: title, page_type, last_compiled, compiled_by, sources, status, tags.
- End with a '## Sources & Lineage' table: | Source | Captured | URL |.
- Link related pages with [[slug]] using only slugs from the list provided.
- Plain, friendly, 8th-grade reading level. Lead with the answer."""


def _tool_lines(project: Project) -> str:
    try:
        return "\n".join(f"- {t.name}: {t.description}" for c in facts.load(project) for t in c.tools) or "(none)"
    except facts.FactsError:
        return "(facts invalid)"


def _write_page(project: Project, text: str, target: Path) -> list[str]:
    text = text.strip()
    text = re.sub(r"\A```(?:markdown)?\s*\n(.*)\n```\s*\Z", r"\1", text, flags=re.DOTALL)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.strip() + "\n", encoding="utf-8")
    return [f"{project.rel(target)}:{ln} {kind} {val!r}" for ln, kind, val in checks.hard_facts_in(target)]


def compile_with_claude(project: Project, *, limit: int = 20) -> tuple[list[str], list[str]]:
    """Draft pages for uncovered sources and refresh stale ones. Returns (written, lint problems)."""
    from groundtruth import llm

    today = dt.date.today().isoformat()
    tools = _tool_lines(project)
    slugs = sorted({p.stem for p in checks.wiki_pages(project)})
    written: list[str] = []
    problems: list[str] = []

    for src in uncovered(project)[:limit]:
        prompt = (f"Today is {today}. compiled_by: {llm.model()}.\n\nFact tools:\n{tools}\n\n"
                  f"Existing page slugs: {', '.join(slugs) or '(none)'}\n\n"
                  f"Write a new page from this source (sources: [{src.rel}]).\n\n"
                  f"### {src.rel}\n{src.path.read_text(encoding='utf-8')}")
        text = llm.complete(SYSTEM, prompt)
        meta, _ = checks.split_frontmatter(text.strip().removeprefix("```markdown").strip())
        ptype = str((meta or {}).get("page_type") or "guide").lower()
        folder = {"policy": "policies", "entity": "entities", "faq": "faq"}.get(ptype, f"{ptype}s")
        target = project.wiki_dir / folder / f"{src.path.stem}.md"
        problems += _write_page(project, text, target)
        written.append(project.rel(target))

    for page, why in stale_pages(project)[:limit]:
        meta = _frontmatter(page)
        evidence = []
        for s in meta.get("sources") or []:
            sp = project.root / str(s).strip("[]").split("|")[0]
            if sp.exists():
                evidence.append(f"### {s}\n{sp.read_text(encoding='utf-8')}")
        prompt = (f"Today is {today}. compiled_by: {llm.model()}. This page is stale ({why}).\n\n"
                  f"Fact tools:\n{tools}\n\nExisting page slugs: {', '.join(slugs)}\n\n"
                  f"Current page:\n\n{page.read_text(encoding='utf-8')}\n\nSources:\n\n" + "\n\n".join(evidence))
        problems += _write_page(project, llm.complete(SYSTEM, prompt), page)
        written.append(project.rel(page))
    return written, problems


def status(project: Project) -> dict:
    return {"uncovered": [s.rel for s in uncovered(project)],
            "stale": [(project.rel(p), why) for p, why in stale_pages(project)]}

