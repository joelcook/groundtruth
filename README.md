# groundtruth

**Give your LLM a single source of truth.**

Everyone wants "a chatbot on our stuff." The usual recipe — embed all your docs
into a vector database, retrieve the top-k chunks, stuff them in the prompt, and
hope — will cheerfully invent a price, a deadline, or a policy. For a lot of
questions that's annoying. For the questions that *matter* — *when does it
close, how much does it cost, am I allowed to* — a confident wrong answer is a
real-world harm.

groundtruth is a blueprint (and a working, runnable example) for doing it the
strict way:

> **One rule: the assistant does not get to make things up.**

Every volatile fact — a date, a dollar amount, an opening time, a phone number —
is answered by a **database tool**, not by prose. Everything else — *how do I…,
what is…, why…* — is answered from a **compiled, source-cited wiki**. A linter
**fails the build if a date or a "$" ever appears in the prose.** That's the
whole trick, and it's surprisingly hard to beat.

It ships as three reusable pieces most teams get wrong or skip:

| | Pillar | What it is |
|---|---|---|
| **1** | **Retrieval over something you care about** | A narrative wiki + a hard-fact SQLite database + an optional embedding store, behind one tool surface. |
| **2** | **An agent that does weekly work** | A scheduled, multi-step pipeline that re-scrapes, rebuilds, recompiles, gates, and opens a PR — with real state and failure modes. |
| **3** | **An eval harness** | Catches the assistant *answering from memory instead of looking it up* — a confident "$0.10" that didn't come from the database fails, because next month it's wrong. Tests facts, explanations, refusals, and contradictions; runs offline or live and flags any regression. The most undervalued thing in the repo. |

This repo is wired up as a tiny **public-library** example so you can run it in
two minutes. Then you delete the library and [hand it to Claude with your own
data](#bring-your-own-data).

---

## The idea in 30 seconds

Three layers, each derived from the one before it:

```
your sources (captured evidence: a scrape, an export, hand-authored YAML)
      │
      ├─ project (scripts/build_db.py) ─────▶  db/groundtruth.db   hard facts (dates, $, hours, contacts)
      │                                              │
      └─ compile (scripts/recompile.py: LLM) ─▶  wiki/   narrative, source-cited, NO loose facts
                                                     │
                          wiki/ + db ────────────────┴─▶  MCP server (10 read-only tools)
                                                                   │
                                                                   ▼
                                                            your LLM / chat platform
```

- **`sources/`** — the immutable evidence. You never hand-edit the database; you
  edit the evidence and re-project it. Every fact can be traced to a source URL.
- **`db/groundtruth.db`** — a SQLite projection of the facts you can be *harmed*
  by getting wrong. The single source of truth for those values.
- **`wiki/`** — human-readable guidance, source-cited, narrative only. If a date
  or a price appears here, `lint_no_hardfacts.py` fails the build.
- **MCP server** — the only thing the model talks to. Facts come through tools;
  narrative comes through `search_wiki`.

When prose and the database disagree, **the database wins** and the page gets
flagged. Narrative is allowed to be approximate; facts are not allowed to be wrong.

---

## Quick start

```bash
git clone https://github.com/joelcook/groundtruth
cd groundtruth
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The database and a recorded eval run are committed, so everything works
immediately — no API key needed for any of this:

```bash
# 1. Rebuild the hard-fact DB from captured evidence (it's already committed):
.venv/bin/python scripts/build_db.py
#   built db/groundtruth.db
#     branches 3 · hours 21 · events 6 · programs 4 · fees 5 · contacts 4

# 2. Run the tests:
.venv/bin/python -m unittest discover -s tests        # → Ran 32 tests ... OK

# 3. Run the launch gate (deterministic replay, no API key, $0):
.venv/bin/python scripts/run_evals.py --tier fast     # → 15/15 passed
```

## See it answer — the MCP server

The model never imports Python. It talks to an MCP server:

```bash
.venv/bin/python scripts/serve_mcp.py          # http://127.0.0.1:8765/mcp
```

Point the runtime check at it (or use `--stdio` to have it launch the server
itself) to watch it answer over the wire:

```bash
.venv/bin/python scripts/check_mcp_runtime.py --stdio
# ok initialize: serverInfo.name=groundtruth
# ok tools/list: 10 tools advertised
# ok tools/call: list_branches returned 3 row(s)
```

The ten tools, split by job:

| narrative | hard facts |
|---|---|
| `search_wiki`, `read_wiki_page` | `get_hours`, `list_events`, `get_event`, `get_fee`, `get_contact`, `list_programs`, `get_program`, `list_branches` |

Set `GT_MCP_API_KEY` and every HTTP request must carry `Authorization: Bearer
<key>` (unauthenticated requests get a `401`).

---

## The three pillars, in depth

### 1 · Retrieval over something you care about → [docs/01-retrieval.md](docs/01-retrieval.md)

The split *is* the architecture:

- **Hard facts → SQLite** (`groundtruth/db.py`, `db/schema.sql`). Read-only query
  functions over a small schema. Every row carries `source_path`/`source_url`.
  Forgiving inputs ("central", "Saturday", "printing"), exact outputs.
- **Narrative → a curated wiki** (`wiki/`). Source-cited markdown with
  frontmatter and a lineage table. Small enough to actually govern.
- **The embedding store, made useful** (`groundtruth/embeddings.py`). Two-tier
  retrieval behind one interface:
  - **scan** (default): a dependency-free lexical scorer that ranks the whole
    wiki in ~1 ms. For a few hundred curated pages this is genuinely hard to
    beat — most teams reach for vectors far too early.
  - **semantic**: embeddings stored *in the same SQLite family as your facts*,
    with a pluggable `Embedder` (an offline hashing stand-in by default; swap in
    a real model in one method). `auto` blends both.

```bash
.venv/bin/python scripts/build_index.py                  # build the vector index
GT_SEARCH_BACKEND=auto .venv/bin/python scripts/serve_mcp.py
```

> Why split facts from narrative? Because a wrong date is a missed event and a
> wrong price is a wrong bill, but "explain holds" can be fuzzy. Different
> failure tolerances deserve different machinery. See
> [docs/01-retrieval.md](docs/01-retrieval.md) for when to add vectors (and when
> not to).

### 2 · An agent that does weekly work → [docs/02-weekly-agent.md](docs/02-weekly-agent.md)

`scripts/refresh_pipeline.sh` is a real autonomous agent, not a cron one-liner:

```
scrape → rebuild facts → reindex → recompile stale prose → gate → eval → draft PR
```

What makes it safe to leave running:

- **State lives in git.** Work happens on a `refresh/<date>` branch, never `main`.
- **Change-gated & idempotent.** If scraping produced no upstream change, it
  makes no commits, opens no PR, and spends **$0** on the LLM. That's most nights.
- **Failure is legible.** On a gate failure it stops *on the refresh branch* so a
  human can see exactly what it saw; on success it returns you to your branch.
- **Evals auto-escalate.** The free deterministic replay runs nightly; the paid
  release-tier set runs *only* when the run actually changed content.

```bash
./scripts/refresh_pipeline.sh                 # dry run: audit + every gate, no writes
./scripts/refresh_pipeline.sh --write --open-pr   # the scheduled invocation
```

Schedule it with `scripts/com.groundtruth.refresh.plist` (launchd) or cron — see
the doc.

### 3 · An eval harness → [docs/03-eval-harness.md](docs/03-eval-harness.md)

Nobody builds these. It's the difference between *hoping* and *knowing*.
`evals/golden.yaml` asserts behavior across four failure modes:

- **hard_fact** — grades the **retrieval path** (`must_call_tool`), not the text.
  A right-looking date that came from the model's memory instead of `get_event`
  **fails** — because next month that memory is wrong.
- **narrative** — an LLM judge scores the answer against a rubric.
- **out_of_scope** — personal questions ("how many books do I have out?") must
  refuse and redirect, and must **not** waste a tool call on data the KB can't hold.
- **contradiction** — when sources disagree, the bot must cite the **current**
  one and not repeat the retired fact.

```bash
.venv/bin/python scripts/run_evals.py --tier fast      # deterministic replay, no key
.venv/bin/python scripts/run_evals.py --tier release   # live model + judge; 100% to ship
```

The eval model **must mirror your production model**, or you're testing a
different assistant than your users talk to.

---

## Bring your own data

This is the part you hand to Claude. Open the repo in Claude Code (or paste
[CLAUDE.md](CLAUDE.md) into your tool of choice) and say:

> *"Here's my data: \<paste / point at it>. Replace the library example with my
> domain following CLAUDE.md."*

The swap, in order:

1. **Schema** — edit `db/schema.sql` to the entities *you* can be harmed by
   getting wrong. Keep `source_path`/`source_url` on every table.
2. **Evidence** — replace `sources/` with your scrape/export/YAML, and update the
   loaders in `scripts/build_db.py`.
3. **Tools** — adjust the query functions in `groundtruth/db.py` and the tool
   wrappers in `groundtruth/mcp.py` to match.
4. **Wiki** — replace `wiki/` with your narrative pages (keep frontmatter +
   lineage, keep facts out of prose).
5. **Prompt** — rewrite `prompts/system_prompt.md` for your domain's fact rules.
6. **Evals** — rewrite `evals/golden.yaml`. Start with 10 questions you know the
   answers to; grow it from every real miss.

`CLAUDE.md` spells out the contract so an agent can do most of this for you.

---

## Keeping it honest

A pile of cheap, deterministic checks run locally and in the refresh agent:

```bash
.venv/bin/python scripts/lint_no_hardfacts.py    # FAILS if a date or $ is in wiki prose
.venv/bin/python scripts/check_wiki.py           # frontmatter, page types, wikilinks resolve
.venv/bin/python -m unittest discover -s tests   # the unit suite
.venv/bin/python scripts/run_evals.py --tier fast
```

## Deploy

```bash
docker build -t groundtruth .
docker run --rm -p 8765:8765 -e GT_MCP_API_KEY="$GT_MCP_API_KEY" groundtruth
```

The image carries the committed `db/`, `wiki/`, `sources/`, `prompts/`, and code,
and starts the MCP server. Put it behind any platform that speaks MCP.

## Layout

```
sources/      captured evidence — never hand-edited; the audit trail
wiki/         compiled, source-cited narrative (no loose facts)
db/           schema.sql + the committed SQLite projection
groundtruth/  the library: db, search, embeddings, mcp, evals, observability
scripts/      CLI entry points — build, index, serve, evals, linters, refresh agent
evals/        the Golden Set + recorded runs
prompts/      the system prompt (source of truth; paste into your platform)
docs/         the three-pillar deep dives + the MCP tool contract
```

## What this is (and isn't)

It **is** a way to ground an LLM in a corpus small enough to *govern*, so the
facts that matter are correct, cited, and current — and to keep it that way with
an agent and prove it with evals.

It is **not** a million-document RAG system, and isn't trying to be. The bet is
the opposite: curate the sources *well*, keep the corpus small, route every
dangerous fact through a tool, and let automation keep it fresh. The quality
ceiling is the quality of your sources — so when an answer is wrong, the fix is
usually upstream, not a hand-edit here.

## Work with me

This repo is the open blueprint. **Doing it for real — on your sources, with your
constraints, kept correct over time — is the work, and it's what I do.** If your
organization needs an assistant that won't make up the facts that matter:

- **Done-for-you build** — a grounded assistant on your data in a fixed scope:
  schema, sources, tools, wiki, prompt, evals, and a deployed MCP endpoint.
- **"Is your AI lying to your users?" audit** — I point this methodology at an
  existing bot and report where it hallucinates facts, with an eval set you keep.
- **Governance retainer** — the corpus and the eval set need care forever. I run
  the refresh loop, grow the Golden Set from real misses, and keep prod honest.
- **Training & workshops** — hands-on, with this repo as the lab.

📬 **Get in touch: [joeltcook93@gmail.com](mailto:joeltcook93@gmail.com)**

The framework is free forever. What's free vs. paid — and why open-sourcing the
blueprint *grows* the work rather than giving it away — is spelled out in
**[docs/open-core.md](docs/open-core.md)**.

## License

MIT — see [LICENSE](LICENSE). The shape of this (raw sources compiled into an
LLM-friendly wiki, with a hard-fact projection underneath) is inspired by
Andrej Karpathy's "LLM wiki" idea. Built on the Model Context Protocol, SQLite,
and the Anthropic API.
