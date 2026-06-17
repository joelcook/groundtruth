# groundtruth

**Give your LLM a single source of truth.**

Everyone wants "a chatbot on our stuff." The usual recipe — embed all your docs
into a vector database, pull the passages that look most relevant, stuff them in
the prompt, and hope — will cheerfully invent a price, a deadline, or a policy.
For a lot of questions that's annoying. For the questions that *matter* — *when
does it close, how much does it cost, am I allowed to* — a confident wrong answer
is a real-world harm.

groundtruth is a blueprint (and a working, runnable example) for doing it the
strict way:

> **One rule: the assistant does not get to make things up.**

Every volatile fact — a date, a dollar amount, an opening time, a phone number —
is looked up in a **database**, not written from memory. Everything else — *how
do I…, what is…, why…* — is answered from a **small, source-cited wiki**. A
linter **fails the build if a date or a "$" ever appears in the wiki prose.**
That's the whole trick, and it's surprisingly hard to beat.

It ships as three reusable pieces most teams get wrong or skip:

| | Pillar | What it is |
|---|---|---|
| **1** | **Retrieval over something you care about** | The stuff you can't get wrong — dates, prices, hours, phone numbers — lives in a database and gets *looked up*, never guessed. Everything else — how-tos, policies, explanations — lives in a small, source-cited wiki the assistant reads. One set of tools reaches both. |
| **2** | **An agent that does weekly work** | A scheduled job that keeps the knowledge current: re-checks your sources, rebuilds the database, rewrites any stale pages, runs every check, and opens a pull request for a human to approve. On a quiet night it changes nothing and costs nothing. |
| **3** | **An eval harness** | Catches the assistant *answering from memory instead of looking it up* — a confident "$0.10" that didn't come from the database fails, because next month it's wrong. Tests facts, explanations, refusals, and contradictions; runs offline or live and flags any regression. The most undervalued thing in the repo. |

This repo is wired up as a tiny **public-library** example so you can run it in
two minutes. Then you delete the library and [hand it to Claude with your own
data](#bring-your-own-data).

---

## The idea in 30 seconds

Three layers, each built from the one before it:

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

- **`sources/`** — the captured evidence: a snapshot of where your facts came
  from. You never hand-edit the database; you change the evidence and rebuild
  from it, so every fact traces back to a real source.
- **`db/groundtruth.db`** — a plain SQLite database holding the facts you can be
  *harmed* by getting wrong. The single source of truth for those values.
- **`wiki/`** — human-readable guidance, source-cited, explanations only. If a
  date or a price sneaks in here, a linter fails the build.
- **the tool server** — the only thing the model talks to. It looks facts up and
  fetches wiki pages through a small set of tools (over **MCP**, the open
  standard for giving an LLM tools). The model never reads files or works from
  memory.

When the wiki and the database disagree, **the database wins** and the page gets
flagged. Explanations are allowed to be approximate; facts are not allowed to be
wrong.

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

## See it answer — the tool server

The model doesn't import Python or read files. It calls tools over **MCP** (the
open standard for giving a model a set of tools). Start the server:

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

| explanations (the wiki) | hard facts (the database) |
|---|---|
| `search_wiki`, `read_wiki_page` | `get_hours`, `list_events`, `get_event`, `get_fee`, `get_contact`, `list_programs`, `get_program`, `list_branches` |

Set `GT_MCP_API_KEY` and every request must send `Authorization: Bearer <key>`;
requests without it are rejected (a `401`).

---

## The three pillars, in depth

### 1 · Retrieval over something you care about → [docs/01-retrieval.md](docs/01-retrieval.md)

The split *is* the architecture:

- **Hard facts → a database** (`groundtruth/db.py`, `db/schema.sql`). Read-only
  lookups over a small table set. Every row carries where it came from. Forgiving
  inputs ("central", "Saturday", "printing"), exact outputs.
- **Explanations → a curated wiki** (`wiki/`). Source-cited markdown, small
  enough that a person can actually keep it honest.
- **The search behind the wiki** (`groundtruth/search.py`, `embeddings.py`). Two
  ways to find the right page, same interface:
  - **scan** (default): plain keyword search — no extra libraries, no index —
    that ranks the whole wiki in about a millisecond. For a few hundred curated
    pages this is genuinely hard to beat; most teams reach for vectors far too
    early.
  - **semantic**: meaning-based search (embeddings), kept in the *same SQLite
    database as your facts* — no separate vector service to run. A placeholder
    model runs offline out of the box; swap in a real one by editing a single
    method. `auto` blends keyword and meaning.

```bash
.venv/bin/python scripts/build_index.py                  # build the semantic index
GT_SEARCH_BACKEND=auto .venv/bin/python scripts/serve_mcp.py
```

> Why split facts from explanations? Because a wrong date is a missed event and a
> wrong price is a wrong bill, but "explain how holds work" can be fuzzy.
> Different costs of being wrong deserve different machinery. See
> [docs/01-retrieval.md](docs/01-retrieval.md) for when to add embeddings (and
> when not to).

### 2 · An agent that does weekly work → [docs/02-weekly-agent.md](docs/02-weekly-agent.md)

`scripts/refresh_pipeline.sh` is a real autonomous agent, not a cron one-liner:

```
scrape → rebuild facts → reindex → recompile stale prose → check → eval → draft PR
```

What makes it safe to leave running:

- **It works on a branch, never on `main`.** All its changes land on a dated
  branch a human reviews before anything ships.
- **A quiet night is free.** If your sources didn't change, it makes no edits,
  opens no pull request, and spends **$0** — which is most nights.
- **Failures are easy to read.** If a check fails, it stops and leaves everything
  exactly as it found it, on the branch, for you to inspect.
- **Evals escalate on their own.** The cheap offline check runs every night; the
  full paid check (a live model) runs *only* when the night actually changed
  something.

```bash
./scripts/refresh_pipeline.sh                 # dry run: audit + every check, no writes
./scripts/refresh_pipeline.sh --write --open-pr   # the scheduled invocation
```

Schedule it weekly with `scripts/com.groundtruth.refresh.plist` (macOS) or cron —
see the doc.

### 3 · An eval harness → [docs/03-eval-harness.md](docs/03-eval-harness.md)

Almost nobody builds these, and it's the difference between *hoping* the bot is
right and *knowing*. `evals/golden.yaml` is a list of questions with the behavior
they must produce, in four flavors:

- **facts** — the answer has to come from a tool call, not the model's memory. A
  date that looks right but was never looked up still **fails** — because next
  month it won't be right.
- **explanations** — a second model grades the answer against a short rubric.
- **out of scope** — personal questions ("how many books do I have out?") must
  politely refuse and point elsewhere, *without* burning a tool call on data the
  system can't possibly have.
- **contradictions** — when an old source and a current one disagree, the bot
  must go with the current one and not repeat the retired fact.

```bash
.venv/bin/python scripts/run_evals.py --tier fast      # offline replay, no key
.venv/bin/python scripts/run_evals.py --tier release   # live model + judge; 100% to ship
```

Run the evals on the **same model you use in production**, or you're grading a
different assistant than your users talk to.

---

## Bring your own data

This is the part you hand to Claude. Open the repo in Claude Code (or paste
[CLAUDE.md](CLAUDE.md) into your tool of choice) and say:

> *"Here's my data: \<paste / point at it>. Replace the library example with my
> domain following CLAUDE.md."*

The swap, in order:

1. **Schema** — edit `db/schema.sql` to the things *you* can be harmed by getting
   wrong. Keep a source on every row.
2. **Evidence** — replace `sources/` with your scrape/export/YAML, and update the
   loaders in `scripts/build_db.py`.
3. **Tools** — adjust the lookups in `groundtruth/db.py` and the tool wrappers in
   `groundtruth/mcp.py` to match.
4. **Wiki** — replace `wiki/` with your explanation pages (keep the citations,
   keep facts out of the prose).
5. **Prompt** — rewrite `prompts/system_prompt.md` for your domain's fact rules.
6. **Evals** — rewrite `evals/golden.yaml`. Start with 10 questions you know the
   answers to; grow it from every real miss.

`CLAUDE.md` spells out the contract so an agent can do most of this for you.

---

## Keeping it honest

A pile of cheap, automatic checks run locally and in the weekly agent:

```bash
.venv/bin/python scripts/lint_no_hardfacts.py    # FAILS if a date or $ is in wiki prose
.venv/bin/python scripts/check_wiki.py           # every page is well-formed and links resolve
.venv/bin/python -m unittest discover -s tests   # the unit suite
.venv/bin/python scripts/run_evals.py --tier fast
```

## Deploy

```bash
docker build -t groundtruth .
docker run --rm -p 8765:8765 -e GT_MCP_API_KEY="$GT_MCP_API_KEY" groundtruth
```

The image carries the committed `db/`, `wiki/`, `sources/`, `prompts/`, and code,
and starts the tool server. Put it behind any platform that speaks MCP.

## Layout

```
sources/      captured evidence — never hand-edited; the audit trail
wiki/         compiled, source-cited explanations (no loose facts)
db/           schema.sql + the committed SQLite database
groundtruth/  the Python package: db, search, embeddings, mcp, evals, observability
scripts/      command-line entry points — build, index, serve, evals, checks, the weekly agent
evals/        the Golden Set + recorded runs
prompts/      the system prompt (source of truth; paste into your platform)
docs/         the three-pillar deep dives + the tool contract
```

## What this is (and isn't)

It **is** a way to ground an LLM in a body of knowledge small enough to *keep
honest*, so the facts that matter are correct, cited, and current — and to keep
it that way with an agent and prove it with evals.

It is **not** a million-document search-everything system, and isn't trying to
be. The bet is the opposite: curate the sources *well*, keep them small, look
every dangerous fact up in the database, and let automation keep it fresh. The
quality ceiling is the quality of your sources — so when an answer is wrong, the
fix is usually upstream, not a hand-edit here.

## Work with me

This repo is the open blueprint. **Doing it for real — on your sources, with your
constraints, kept correct over time — is the work, and it's what I do.** If your
organization needs an assistant that won't make up the facts that matter:

- **Done-for-you build** — a grounded assistant on your data in a fixed scope:
  schema, sources, tools, wiki, prompt, evals, and a deployed endpoint.
- **"Is your AI lying to your users?" audit** — I point this methodology at an
  existing bot and report where it invents facts, with an eval set you keep.
- **Governance retainer** — the knowledge and the eval set need care forever. I
  run the weekly agent, grow the Golden Set from real misses, and keep it honest.
- **Training & workshops** — hands-on, with this repo as the lab.

📬 **Get in touch: [joeltcook93@gmail.com](mailto:joeltcook93@gmail.com)**

The framework is free forever. What's free vs. paid — and why open-sourcing the
blueprint *grows* the work rather than giving it away — is spelled out in
**[docs/open-core.md](docs/open-core.md)**.

## License

MIT — see [LICENSE](LICENSE). The shape of this (raw sources compiled into an
LLM-friendly wiki, with a hard-fact database underneath) is inspired by
Andrej Karpathy's "LLM wiki" idea. Built on the Model Context Protocol, SQLite,
and the Anthropic API.
