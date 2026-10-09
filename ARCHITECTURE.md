# Architecture

groundtruth makes a body of knowledge *AI-ready at the data layer*. It turns
evidence into two maintained assets, facts and explanations, and serves them to
a model over MCP. The model uses the wiki to explain and the facts to be exact.

## The core contract

The model never answers a hard fact from memory or from prose. It calls a tool,
and the tool's answer carries its source. When a wiki page and a fact tool
disagree, the tool wins and the page is fixed. Explanations may be approximate;
facts may not be wrong.

## Layers, each derived from the one before

```
evidence      sources/pages/*.md          ./gt ingest (web, sitemap, folder)
                  │
                  ├── facts/*.yaml ──build──▶ build/facts.db    deterministic, no model in the loop
                  │     (human or agent)
                  └── wiki/*.md                                  human, agent, or ./gt compile --write
                        (no loose facts)
                  │
serving       MCP server: one tool per facts entry + search_wiki + read_wiki_page   (read-only)
                  │
clients       Claude Desktop, Claude Code, Codex, Cursor, VS Code, Copilot Studio, your app
```

1. **Evidence is captured, not edited.** `./gt ingest` writes pages with their
   source URL, capture date, and content hash. You change facts by changing (or
   re-capturing) evidence and re-deriving.
2. **Facts are declared, not coded.** Each `facts/*.yaml` file declares rows and
   the tools that serve them. `groundtruth/facts.py` validates them, projects them
   into SQLite deterministically, and answers tool calls with forgiving matching
   and exact values. No per-domain Python.
3. **Explanations are linted.** The wiki is markdown with frontmatter and a
   lineage table. `./gt check` fails on dates or dollar amounts in prose, so facts
   can't drift into pages and go stale there.
4. **Serving is read-only.** `groundtruth/server.py` generates the MCP tool
   surface from the project. Data and pages reload live.

## Modules

| Module | Job |
|---|---|
| `project.py` | Find the active project and its paths |
| `facts.py` | Load, validate, build, and query facts; generate tool specs |
| `search.py`, `embeddings.py` | Wiki search: keyword scan, optional embeddings |
| `server.py` | The MCP server, auth, host rules, `/healthz` |
| `checks.py` | `./gt check`: facts, source review, wiki structure, prose lint, golden wiring, prompt |
| `review.py` | `sources.lock.yaml`: detects evidence that changed since facts and pages were reviewed |
| `examples.py` | Finds and removes a template's fictional example content |
| `evals.py`, `llm.py` | The golden-set harness; Claude for live evals |
| `ingest.py` | Website, sitemap, and folder capture |
| `compile.py` | Coverage and staleness; agent work orders; Claude drafting |
| `observability.py`, `report.py` | Tool-call log and the dashboard |
| `watchdog.py` | Outside-in health checks over real MCP |
| `refresh.py` | The weekly agent |
| `bundle.py` | Data bundles and hot-swap for deployed servers |
| `connect.py`, `scaffold.py`, `cli.py` | Client setup, `init`/`use`, the `./gt` command |

## Why a small, governed corpus beats big RAG

The usual pattern (embed everything, retrieve top-k, hope) optimizes for
*coverage* and quietly sacrifices *correctness*. groundtruth makes the opposite
bet: a corpus small enough that a person can govern it, with the dangerous facts
pulled into a typed store that can't drift into prose. You trade recall you
don't need for correctness you can't live without.

## The governance loop

```
1. ingest       re-capture evidence (only changed pages are rewritten)
2. build        re-derive facts (deterministic)
3. compile      flag new, changed, and stale coverage; draft pages (agent or Claude)
4. check, eval  the gates; check stays red while cited sources are unreviewed
5. review       a human reconciles changed evidence (./gt review --done) and merges the PR
```

`./gt refresh` runs it; `.github/workflows/refresh.yml` schedules it weekly.

## What "ready to ship" means

- `./gt check` and `./gt eval` pass (CI enforces both);
- `./gt eval --tier release` passes 100% on the production model;
- no facts in wiki prose; every fact has a source;
- the server is deployed with an API key, watched by the watchdog, and the
  weekly refresh is on.
