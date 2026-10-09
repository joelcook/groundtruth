# Working in groundtruth (for coding agents)

This file tells an agent (Claude Code, Codex, Cursor...) how to work in this
repo. `AGENTS.md` is the same file. Follow it exactly.

## What this repo is

A framework for grounding an LLM assistant in a body of knowledge:

- **Code** lives in `groundtruth/` (the Python package) and `./gt` (the CLI).
  You rarely need to touch it to build an assistant.
- **Projects** are folders with a `groundtruth.yaml`. Each one is a separate
  assistant. `examples/library/` is the demo; `templates/` are starting points;
  the active project is named in `groundtruth.project` (`./gt status` shows it).

Inside a project:

| Path | Holds | Rules |
|---|---|---|
| `facts/*.yaml` | Exact facts (prices, dates, hours, contacts, limits) and the tools that serve them | Every row has a source. Format: `docs/facts.md` |
| `wiki/**/*.md` | Explanations (how-to, policy, "what is") | Frontmatter + `## Sources & Lineage`. **No exact facts in prose.** Format: `docs/wiki.md` |
| `sources/pages/*.md` | Evidence, written by `./gt ingest` | Never edit by hand |
| `sources.lock.yaml` | Which version of each source the facts/wiki were last reviewed against | Only via `./gt review --done` |
| `prompts/system_prompt.md` | The assistant's instructions (what MCP clients receive) | Keep `{{FACT_TOOLS}}` in it: the routing table is generated from `facts/` |
| `evals/golden.yaml` | Questions with known answers | Format: `docs/evals.md` |
| `build/`, `logs/`, `reports/` | Generated | Never edit, never commit |

## The one rule: facts come from tools

The assistant must never state a date, time, dollar amount, phone number, email,
hours, deadline, or limit from memory or from wiki prose. Those live in
`facts/*.yaml` and are served by tools. In wiki prose, name the tool instead:
"the assistant looks up the current fee with `get_fee`." If a wiki page and a
fact tool disagree, the tool is right and the page is stale.

## The gates: run before you say you're done

```bash
./gt check     # facts valid, sources reviewed, wiki structure, no facts in prose, golden set wired
./gt eval      # free, no model: tools return the expected facts, search finds the pages
python -m unittest discover -s tests    # only if you changed code in groundtruth/
```

If `./gt check` says a source changed since the last review, run `./gt review
--diff`, compare the listed facts and pages with the change, fix what's stale,
and only then run `./gt review --done`. Never run `--done` without actually
re-checking. A source "removed upstream" means the document is gone: update or
remove whatever still cites it.

Both `./gt` commands must pass. `./gt -p PATH ...` targets a specific project.
If you changed the prompt, tools, or content and an `ANTHROPIC_API_KEY` is
available, also run `./gt eval --tier live` and report the result.

## Common jobs

### "Here's my site / my documents. Build the assistant."

1. `./gt init NAME --template TEMPLATE` (pick the closest: `./gt init --list`).
2. `./gt ingest URL_OR_FOLDER` to capture the evidence.
3. `./gt compile` writes `NAME/build/compile-packet.md`. Do what it says:
   - Exact things become rows in `facts/*.yaml`. Reuse a template file's shape;
     add a file per kind of thing; give every tool a sharp `description`.
   - Explanations become wiki pages: one topic per page, the words people use in
     `title` and `tags`, sources listed, lineage table at the bottom.
   - Rewrite `prompts/system_prompt.md` for the organization (scope, tone, what
     to refuse and where to redirect). Keep `{{FACT_TOOLS}}`: it lists the tools.
   - Replace the template's example questions in `evals/golden.yaml`: a
     `hard_fact` item (with `must_call_tool`, `tool_args`, and `must_match` or
     `must_contain`) per tool, and a `narrative` item (`must_cite_page`) per page.
   - Remove the template's fictional examples (`example: true`) with
     `./gt examples --remove` once real content replaces them.
   - `./gt review --done` once facts and pages match the sources.
4. Run the gates. Fix everything. Then `./gt ask "..."` a few real questions.

### "Do the work in build/compile-packet.md"

Read the packet, do every item, run the gates, and summarize what you added and
anything in the sources you couldn't place.

### "The assistant got X wrong"

1. Add X to `evals/golden.yaml` first, and watch `./gt eval` fail (or, for
   model behavior, `./gt eval --tier live --only ID`).
2. Fix the cause upstream: a missing or wrong fact row, an alias, a page title,
   a synonym in `groundtruth.yaml`, or the prompt.
3. Watch it pass. Run the full gates.

### Changing the code in `groundtruth/`

Keep it dependency-light and readable; every module starts with a docstring
explaining why it exists. Add a test in `tests/` for any behavior you change.
Don't add per-domain logic to the code: domains belong in project YAML.

## Don'ts

- Don't put facts in wiki prose (dates, money, durations, percentages, size
  limits, phone numbers, emails). Don't use `<!-- fact-ok -->` to dodge the rule;
  it's for genuinely stable, illustrative values, with a reason.
- Don't run `./gt review --done` to make a check go green. It means "I compared
  the facts with the changed source and they're right." 
- Don't edit `sources/`; re-run `./gt ingest`.
- Don't commit `build/`, `logs/`, `reports/`, or `evals/runs/*-tools.json`.
- Don't invent facts to make an eval pass. If the evidence doesn't say it, the
  fact doesn't exist yet: tell the human.
