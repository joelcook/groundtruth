# groundtruth

**Give your LLM a single source of truth.**

Every "chatbot on our docs" makes the same bet: embed everything, fetch a few
paragraphs that look relevant, and hope the model doesn't invent a price, a
deadline, or an opening time. It will. Not often, but it only takes once. A
wrong date is a missed deadline. A wrong number is a wrong bill.

groundtruth makes the opposite bet, and it's a simple one: **the model never
has to guess.** Every fact is one tool call away, and every answer can say
where it came from.

- **Facts live in a database.** Prices, dates, hours, phone numbers. You write
  them in plain YAML, and each file becomes tools the model has to call. No
  Python, no SQL.
- **Explanations live in a small wiki.** How-tos and policies in markdown, every
  page citing its sources. `./gt check` fails if a date, a time, a price, a
  limit, or a phone number sneaks into the prose, and the server only serves
  versions that pass.
- **Every fact is grounded.** Each number, date, time, price, phone number,
  email, and URL in your facts must appear in the captured source it cites. A
  script checks every one, so a mistyped or invented value can't ship.
- **Changed evidence can't hide.** Re-capture your sources and anything built on
  a page that changed, or disappeared, gets flagged with a diff until it's
  reconciled: by a person, or by the refresh host's checked rewrites.
- **Evals prove it.** Questions with known answers. The free tier checks your
  data and your search with no API key. The live tier asks Claude, with the real
  tools, and fails any answer that came from memory instead of a lookup.

It speaks MCP, so it works with Claude Desktop, Claude Code, Codex, Cursor, VS
Code, Copilot Studio, and anything else that does. It runs on your laptop or any
server. It's MIT licensed, and every piece described here is in this repo. All of
it runs free except what needs a model: live evals, and the unattended rewrites,
which run on your Claude plan or API key.

## Try it: 2 minutes, no API key

```bash
git clone https://github.com/joelcook/groundtruth
cd groundtruth
./gt demo
```

The first run sets itself up (it needs Python 3.10 or newer; on Windows run
`py gt demo`). Then it builds and tests a small public-library example:

```
  ✓ facts: 6 collections, 43 rows, 8 tools generated from YAML
  ✓ check: 6 page(s), no facts in prose, 18 golden question(s)
  ✓ eval (free): 15/15 passed, 3 need a model
  ✓ server: answered over MCP (initialize, tools/list, a fact tool, search_wiki)
```

Now poke at it:

```bash
./gt ask "what time does the central library open on saturday?"
./gt connect claude-desktop --install     # restart Claude Desktop, then ask it about the library
```

Without an API key, `./gt ask` shows what the model would look up
(`get_hours(branch='central', day='saturday')`) and what comes back. With
`ANTHROPIC_API_KEY` set, Claude writes the answer using those tools.

## Make your own

The fastest way: fork this repo, open it in Claude Code (or Codex, or Cursor),
and say what you want:

> Build an assistant for our organization from https://our-site.example and
> our policy folder, then take it to production and keep it current.

[CLAUDE.md](CLAUDE.md) walks the agent through every step. It builds the
facts, pages, and eval questions, deploys, and turns on the autopilot and the
watchdog. From then on the assistant keeps itself current. The agent asks you
only for credentials (a hosting login and a Claude key), all at once, and makes
every other decision itself. `./gt status` shows where you are at any point. By
hand, it's these commands:

```bash
./gt init my-assistant --template saas    # or: college, city, clinic, blank
./gt ingest https://your-site.example      # pull your pages (or a folder) in as evidence
./gt compile                                # proposed facts, draft pages, and a work order
# now the real work: check the proposed facts, finish the pages, write eval
# questions (or have Claude Code / Codex do the work order). ./gt status guides you.
./gt review --done                          # record that your facts match the sources
./gt check --production && ./gt eval        # prove it
./gt connect claude-desktop --install       # use it
```

1. **`init`** creates `my-assistant/` from a template and points `./gt` at it.
   It's born passing every check, so each change you make is one you can test.
   The template's example rows and pages are fictional and marked as examples;
   `./gt check` reminds you until they're replaced (`./gt examples --remove`).
2. **`ingest`** crawls your site (sitemap first, `robots.txt` respected) or reads
   a folder of `.md`, `.html`, `.txt`, or `.pdf` files into `sources/pages/`.
   Re-running only rewrites pages that actually changed, and keeps two documents
   with the same name separate. If a page your facts or wiki cite changed or
   disappeared upstream, `./gt check` fails until you've re-checked them
   (`./gt review --diff` shows exactly what changed) and run `./gt review --done`.
3. **`compile`** does the first pass for you, no API key needed:
   - **Proposed facts**: `facts/_proposed-pricing.yaml`, one row per exact
     statement it found in the source ("The Team plan costs USD 39 per month"),
     with a lookup tool and the source wired in. The leading `_` keeps it
     offline. Check the rows, rename it to `facts/pricing.yaml`, and it's live.
   - **Draft pages**: a skeleton per source in `wiki/drafts/`, not served until
     you write it and set `status: published`.
   - **A work order**, `build/compile-packet.md`: what's left, starter eval
     questions built from your sources, and the commands to verify.

   Or open the repo in Claude Code (or Codex, or Cursor) and say *"do the work
   in my-assistant/build/compile-packet.md"*: it reshapes the facts, writes the
   pages, swaps out the template examples, and runs the checks. Have an API key?
   `./gt compile --write` drafts the pages with Claude directly.
4. **`check` and `eval`** catch facts in prose, broken links, bad YAML, and
   questions your data can't answer. `./gt check --production` also fails on
   leftover template examples: that's the gate the server and Docker use.
5. **`connect`** prints setup for every common client with your real paths filled
   in. `--install` does it for you: it writes Claude Desktop, Cursor, or Windsurf
   config (backing up the old file) or runs `claude mcp add` / `codex mcp add`.

Lost? **`./gt status`** shows the road from template to production: what's
done, what's left, and the exact next command.

**How long it takes:** the commands take minutes. The real work is curation:
deciding what's a fact, writing pages, choosing eval questions. A coding agent
working from the compile packet does most of it for a small site in an
afternoon; by hand, budget a day for your first few dozen facts and pages.

Prefer to do it by hand? Every file is plain YAML or markdown. Edit `facts/`
and `wiki/` directly, and `./gt check` tells you if anything's off.

## A fact is a few lines of YAML

```yaml
# my-assistant/facts/plans.yaml
description: Subscription plans with prices and limits.
key: id
source: {path: sources/pages/pricing.md, url: https://example.com/pricing}
tools:
  - name: get_plan
    kind: lookup              # get_plan(plan="pro") finds "team" through its aliases
    arg: plan
    description: Price and limits for a plan. Every price the assistant states comes from here.
rows:
  - {id: starter, aliases: [basic], price_usd: 9, projects: 3}
  - {id: team, aliases: [pro, business], price_usd: 29, projects: unlimited}
```

That file gives the model a `get_plan` tool. Lookups are forgiving ("pro",
"business plan", "how much is team") and answers are exact, each with a `source`
(the URL, or the file for local documents) so the assistant can cite it. Edit
the YAML and the running server picks it up as soon as it passes `./gt check`. `list` tools add filters (`get_hours(branch="main", day="sat")`),
references between files, and "upcoming only" for dated rows. Full reference:
[docs/facts.md](docs/facts.md).

## A wiki page is markdown with receipts

```markdown
---
title: Refunds and cancellation
page_type: policy
sources: [sources/pages/refunds.md]
---

# Refunds and cancellation

Annual plans can be refunded within the refund window. The assistant looks up
the exact window with `get_limit`, so it's always current.

## Sources & Lineage

| Source | Captured | URL |
|---|---|---|
| [[sources/pages/refunds.md]] | 2026-01-15 | https://example.com/legal/refunds |
```

Notice what's missing: the number. "Within 30 days" in prose is a fact that goes
stale silently, so `./gt check` fails on it, along with dates, times, prices,
percentages, limits, phone numbers, and emails. It knows the common ways of
writing those, not every possible one, so the rule still applies to what it
misses. See [docs/wiki.md](docs/wiki.md).

## Evals that grade the lookup, not the vibes

```yaml
# my-assistant/evals/golden.yaml
- id: team-plan-price
  question: "How much is the Team plan?"
  category: hard_fact
  must_call_tool: get_plan
  tool_args: {plan: team}       # lets the free tier check your data with no model
  must_match: {price_usd: 29}   # or must_contain: ["29"] (numbers compare by value)
```

- `./gt eval` (free, no key): calls each tool and confirms the answer is in your
  data, and confirms search finds the right wiki page for each how-to question.
  It catches data and retrieval regressions before anyone sees them, and says
  so: it reports data and retrieval separately, and it doesn't claim to know
  whether a model would pick that tool.
- `./gt eval --tier live`: runs every question through Claude with the real tools.
  A correct-looking price that wasn't looked up still fails, because next month
  it won't be correct.

Four categories: `hard_fact`, `narrative`, `out_of_scope` (refuse personal or
account questions without wasting a tool call), and `contradiction` (an old
source and a new one disagree; use the new one). See [docs/evals.md](docs/evals.md).

Be clear about what this buys you: nothing makes a model *incapable* of guessing.
groundtruth makes guessing unnecessary (every fact is one tool call away), makes
it visible (every answer cites a source), and fails the build when the evals
catch it. Test on the model you actually run.

## Run it for real

```bash
GT_MCP_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))") ./gt serve
docker build -t my-assistant --build-arg PROJECT=my-assistant . && docker run -p 8765:8765 -e GT_MCP_API_KEY=... my-assistant
```

- **Deploy anywhere that runs a container**: Fly.io, Render, Railway, Azure
  Container Apps, Google Cloud Run, a VPS. Recipes in [docs/deploy.md](docs/deploy.md).
- **It only serves checked content.** The server won't start on a project that
  fails the checks: `./gt check --production` over HTTP, `./gt check` over stdio
  (what Claude Desktop and other local clients use, so you can still explore a
  template). While it runs, every edit is checked first: one that fails is
  rejected, the last good version keeps serving, and `/healthz` says so. **`./gt watchdog --url ...`** checks a deployed server end
  to end, daily via `.github/workflows/watchdog.yml`.
- **Weekly refresh**: `.github/workflows/refresh.yml` re-ingests your sources,
  rebuilds, runs the gates, and opens a pull request. A quiet week opens nothing
  and costs nothing.
- **Or nobody in the loop at all.** With `host: github-actions`, a scheduled
  workflow keeps everything current by itself, with no machine to run. It
  re-captures sources nightly, and the ones your facts cite every two hours.
  Headless Claude Code (Haiku writes, Sonnet checks) then:
  - updates the facts a changed source affects,
  - writes pages for new sources,
  - rewrites stale pages.

  A change ships only when every value in it appears in its source, the gates
  pass, and the second model agrees. Anything it can't handle becomes a line in
  `ALERTS.md`, and the daily watchdog fails until it's fixed. See
  [docs/operations.md](docs/operations.md#the-refresh-host-nobody-in-the-loop).
- **A chat page, no client to set up**: with `GT_CHAT=1` and an Anthropic key on
  the server, `/chat` is a web chatbot for your users. It uses the same tools and
  instructions your evals grade, shows its sources, and rate-limits visitors.
- **Data without redeploys**: `./gt bundle` packages your facts and wiki;
  servers started with `GT_DATA_URL` verify and hot-swap new bundles.
- **`./gt report`**: what people ask, error rates, latency, and the gaps:
  lookups that found nothing, each a ready-made to-do.

## Every command

| Command | What it does |
|---|---|
| `./gt demo` | Build and test the bundled library example |
| `./gt ask "..."` | What the assistant would answer, and from which tools |
| `./gt init NAME -t TEMPLATE` | Start your own assistant (`./gt init --list` for templates) |
| `./gt ingest URL\|FOLDER` | Pull a site, sitemap, or folder into `sources/pages/` |
| `./gt compile [--write]` | Draft pages, fact candidates, and a work order for a coding agent (or draft pages with Claude) |
| `./gt check [--production]` | Facts, source review, wiki structure, no facts in prose, eval wiring (`--production`: no template examples) |
| `./gt review [--diff] [--done]` | Sources that changed (or vanished) since your facts and wiki were checked against them |
| `./gt examples [--remove]` | The template's fictional example content |
| `./gt eval [--tier live]` | Run the golden set (free tier needs nothing; `--export copilot-studio` to grade the model there) |
| `./gt serve [--stdio]` | The MCP server |
| `./gt connect [CLIENT] [--install]` | Setup for Claude, Codex, Cursor, VS Code, Copilot Studio... |
| `./gt tools` | The tools your assistant gets |
| `./gt prompt` | The exact instructions every client receives |
| `./gt report` | Usage, errors, latency, and gaps from the tool-call log |
| `./gt watchdog [--url URL] [--repo]` | Outside-in health check (`--repo`: open alerts, last refresh, CI, deployed code lag) |
| `./gt refresh [--write --open-pr]` | The weekly agent |
| `./gt rewrite [--list] [--page P] [--dry-run]` | Update changed facts, cover new sources, rewrite stale pages, all with headless Claude Code and checked |
| `./gt host autopilot\|install\|check` | The refresh host: GitHub Actions (`autopilot.yml`) or a machine (cron) |
| `./gt alerts` | What the unattended jobs need a person for (`ALERTS.md`) |
| `./gt bundle` | Package data for live updates |
| `./gt status` | What's done, what's left, and the next command |
| `./gt use PATH` · `doctor` · `setup` | Housekeeping |

Add `-p PATH` before any command to work on a different project.

## How it works

```
your website / exports ──ingest──▶ sources/pages/   (evidence, with source URLs)
                                        │
             ┌──────────── you, or a coding agent, or ./gt compile ───────────┐
             ▼                                                                 ▼
     facts/*.yaml  ──build──▶ build/facts.db                      wiki/*.md (no loose facts)
             │                                                                 │
             └──────────────▶  MCP server: get_plan, get_hours, ...  +  search_wiki, read_wiki_page
                                        │
                     Claude Desktop · Claude Code · Codex · Cursor · Copilot Studio · your app
```

The model only ever talks to the server, and every tool is read-only. When a wiki
page and a fact tool disagree, the fact tool wins. More in
[ARCHITECTURE.md](ARCHITECTURE.md).

## What this is, and isn't

It **is** a way to ground an assistant in a body of knowledge small enough to keep
honest: hundreds of pages and thousands of facts, not millions of documents. The
quality ceiling is the quality of your sources, so when an answer is wrong, the
fix is usually upstream, and the eval makes sure it stays fixed.

It is **not** a search-everything system over a giant document dump, and it
doesn't try to be.

## Docs

[Getting started](docs/getting-started.md) · [Facts](docs/facts.md) ·
[Wiki](docs/wiki.md) · [Evals](docs/evals.md) · [Connect](docs/connect.md) ·
[Deploy](docs/deploy.md) · [Operations](docs/operations.md) ·
[Retrieval](docs/retrieval.md) · [Troubleshooting](docs/troubleshooting.md)

Working with a coding agent? [CLAUDE.md](CLAUDE.md) (also [AGENTS.md](AGENTS.md))
tells it exactly how to work in this repo.

## License

MIT. Free forever, all of it. See [LICENSE](LICENSE).

The shape of this (raw sources compiled into an LLM-friendly wiki) comes from
Andrej Karpathy's [LLM wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
idea; groundtruth adds the rule that facts live in a database instead. Built on
the [Model Context Protocol](https://modelcontextprotocol.io), SQLite, and Claude.
