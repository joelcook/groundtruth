# CLAUDE.md — working in (and adopting) groundtruth

This file governs how an agent works in this repo. It has two jobs: keep the
knowledge base honest, and help a new owner **replace the example domain with
their own**. If you are an LLM reading this: follow it exactly.

## What this repo is

A grounded knowledge base for an LLM. Two assets, one rule:

- `sources/` — immutable evidence (a scrape/export/hand-authored YAML). Never
  hand-edit facts elsewhere; edit the evidence and re-project.
- `db/groundtruth.db` — SQLite projection of **hard facts** (dates, dollars,
  hours, contacts). Built by `scripts/build_db.py`. Read-only at runtime.
- `wiki/` — compiled **narrative** pages, source-cited, no loose facts.
- `groundtruth/` — the runtime library (`db`, `search`, `embeddings`, `mcp`,
  `evals`, `observability`).
- `evals/golden.yaml` — the launch gate.

## The one rule: facts come from tools

The assistant MUST NOT state any of these from memory or from wiki prose:

| fact | tool |
|---|---|
| a date / time of an event | `get_event`, `list_events` |
| whether/when a place is open | `get_hours` |
| any dollar amount | `get_fee` |
| a phone number or email | `get_contact` |
| whether a program needs registration | `get_program`, `list_programs` |

If a wiki page disagrees with a tool, **the tool is right** and the page is
stale — cite the tool and flag the page.

**Enforcement.** `scripts/lint_no_hardfacts.py` fails the build if a calendar
date or a `$` amount appears in wiki prose (outside frontmatter and the
`## Sources & Lineage` table). If a value genuinely must stay in prose (a stable,
illustrative figure), justify it inline on the same or previous line:

```
<!-- fact-ok: why this value is safe to hard-code -->
```

Use it sparingly. The default is: route the fact through a tool.

## Wiki conventions

- YAML frontmatter on every page: `title, page_type, last_compiled, compiled_by,
  sources, related, audience, status, tags`.
- `page_type` ∈ {journey, process, policy, entity, comparison, internal}.
- Every page ends with a `## Sources & Lineage` table (the audit trail).
- Use `[[wikilinks]]` for cross-references; link to evidence as
  `[[sources/path/to/file.md]]`.
- Pages in `wiki/internal/` are never surfaced by the public tools.
- Recompiles are review-gated: `scripts/recompile.py` writes only with `--write`,
  and the refresh pipeline lands changes on a `refresh/<date>` branch for a human
  to merge. Direct human edits to `wiki/` are welcome.

## Before you claim a change is done, run the gates

```bash
.venv/bin/python scripts/check_wiki.py
.venv/bin/python scripts/lint_no_hardfacts.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/run_evals.py --tier fast
```

All four must be green. If you changed the prompt, tools, or content, run
`scripts/run_evals.py --tier release` (needs `ANTHROPIC_API_KEY`).

---

## Adopting groundtruth for a new domain (the swap)

When the owner says *"here's my data, let's build this,"* do this in order and
run the gates after each step.

1. **Model the facts that are dangerous to get wrong.** Edit `db/schema.sql`.
   Replace the library tables with your entities (e.g. for a SaaS help center:
   `plans`, `limits`, `releases`, `contacts`). Keep `source_path` + `source_url`
   on every table — provenance is non-negotiable.

2. **Bring the evidence.** Replace `sources/` with the owner's scrape, export, or
   hand-authored YAML. Update the loaders in `scripts/build_db.py` to read it.
   Rebuild: `python scripts/build_db.py`.

3. **Expose the facts.** Update the query functions in `groundtruth/db.py` and
   the thin tool wrappers in `groundtruth/mcp.py`. Keep tools read-only, with
   forgiving inputs and exact outputs. Keep the docstrings sharp — the model
   reads them to decide which tool to call. Update `EXPECTED_TOOLS` in
   `scripts/check_mcp_runtime.py` and `TOOL_DISPATCH` in `groundtruth/evals.py`.

4. **Write the narrative.** Replace `wiki/` with the owner's how/why/process
   pages. Keep frontmatter + lineage. Keep facts OUT of prose — refer to tools.
   Tune the `SYNONYMS` map in `groundtruth/search.py` to the owner's vocabulary.

5. **Rewrite the prompt.** `prompts/system_prompt.md` is the contract: which
   facts route to which tools, how to refuse out-of-scope questions, tone.

6. **Write the eval set.** `evals/golden.yaml`. Start with ~10 questions whose
   answers you know cold, one per category. Then **grow it from every real
   miss** — that is the whole discipline. Record a baseline run so `--tier fast`
   works offline (or just run `--tier full` once with a key).

7. **Delete the library.** Remove leftover Maplewood references in docs and the
   README header. Update the example commands' expected output.

Keep the architecture; replace the data. The framework is the engine — the
owner brings the fuel.
