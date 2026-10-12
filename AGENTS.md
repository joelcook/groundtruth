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
| `ALERTS.md` | Open alerts from the refresh host (`./gt host`) | Delete a line only after you've fixed its cause |
| `build/`, `logs/`, `reports/` | Generated | Never edit, never commit |

## The one rule: facts come from tools

The assistant must never state a date, time, dollar amount, phone number, email,
hours, deadline, or limit from memory or from wiki prose. Those live in
`facts/*.yaml` and are served by tools. In wiki prose, name the tool instead:
"the assistant looks up the current fee with `get_fee`." If a wiki page and a
fact tool disagree, the tool is right and the page is stale.

## The gates: run before you say you're done

```bash
./gt check                # facts valid, sources reviewed, wiki structure, no facts in prose, golden set wired
./gt eval                 # free, no model: tools return the expected facts, search finds the pages
./gt check --production   # before shipping: also fails on leftover template examples
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
3. `./gt compile` writes proposed facts per new source (`facts/_proposed-*.yaml`:
   one row per exact statement; the `_` keeps them offline), a draft page per
   new source in `wiki/drafts/` (`status: draft`: not served, not review-gated),
   and `NAME/build/compile-packet.md`,
   which lists the exact values it found in each source (all of them:
   `build/fact-candidates.md`). Do what the packet says:
   - Exact things become rows in `facts/*.yaml`. Start from the proposals:
     verify every row against its source, and reshape statements into real
     fields where you can (`price_usd: 39`; reuse a template file's shape), then
     rename `_proposed-x.yaml` to `x.yaml`. Give every tool a sharp `description`.
   - Explanations become wiki pages: finish the drafts (or write new pages), one
     topic per page, the words people use in `title` and `tags`, sources listed,
     lineage table at the bottom. Set `status: published` when a page is done.
   - Rewrite `prompts/system_prompt.md` for the organization (scope, tone, what
     to refuse and where to redirect). Keep `{{FACT_TOOLS}}`: it lists the tools.
   - Replace the template's example questions in `evals/golden.yaml`: a
     `hard_fact` item (with `must_call_tool`, `tool_args`, and `must_match` or
     `must_contain`) per tool, and a `narrative` item (`must_cite_page`) per page.
   - Remove the template's fictional examples (`example: true`) with
     `./gt examples --remove` once real content replaces them.
   - `./gt review --done` once facts and pages match the sources.
4. Run the gates. Fix everything. Then `./gt ask "..."` a few real questions.
   `./gt status` lists anything still unfinished, in order, with the command for each.

### "Do the work in build/compile-packet.md"

Read the packet, do every item, run the gates, and summarize what you added and
anything in the sources you couldn't place.

### "The assistant got X wrong"

1. Add X to `evals/golden.yaml` first, and watch `./gt eval` fail (or, for
   model behavior, `./gt eval --tier live --only ID`).
2. Fix the cause upstream: a missing or wrong fact row, an alias, a page title,
   a synonym in `groundtruth.yaml`, or the prompt.
3. Watch it pass. Run the full gates.

### Working alongside the refresh host

If the project names a `host:` in `groundtruth.yaml`, one machine also commits
to main on its own (docs/operations.md). Pull before you start. Its commits are
`refresh: ...` (re-captured sources), `wiki: rewrite ...` (a stale page that
headless Claude Code rewrote and a second model checked against its sources),
and `alerts: ...`. Don't revert them; fix what's upstream instead.

- **An alert in `ALERTS.md`:** fix the cause, run the gates, then delete its line
  in the same commit. "Facts cite sources that changed" means: `./gt review
  --diff`, fix the facts, `./gt review --done`. That one is always yours: the
  host never reviews a source that facts cite.
- **`./gt rewrite`** rewrites stale pages the same way the host does. It needs a
  clean checkout (it reverts every file but the page) and the `claude` CLI.
  `./gt rewrite --list` shows what's stale without changing anything.

### Changing the code in `groundtruth/`

Keep it dependency-light and readable; every module starts with a docstring
explaining why it exists. Add a test in `tests/` for any behavior you change.
Don't add per-domain logic to the code: domains belong in project YAML.

## Don'ts

- Don't put facts in wiki prose (dates, money, durations, percentages, limits,
  phone numbers, emails). The checker knows the common formats, not every one:
  the rule applies to what it misses too. Don't use `<!-- fact-ok -->` to dodge
  it; it's for genuinely stable, illustrative values, with a reason.
- Don't run `./gt review --done` to make a check go green. It records a human
  (or agent) judgment: "I compared the facts with the changed source and they're
  right." Nothing verifies that judgment for you.
- Don't edit `sources/`; re-run `./gt ingest`.
- Don't commit `build/`, `logs/`, `reports/`, or `evals/runs/*-tools.json`.
- Don't put a key or token in a tracked file (`ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `GT_MCP_API_KEY`...). They go in `.env`, which is
  gitignored; `.env.example` holds empty placeholders only.
- Don't invent facts to make an eval pass. If the evidence doesn't say it, the
  fact doesn't exist yet: tell the human.
