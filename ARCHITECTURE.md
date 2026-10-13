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
   lineage table. `./gt check` fails on the common ways of writing dates, times,
   money, durations, limits, and contacts in prose, so facts don't drift into
   pages and go stale there.
4. **Serving is read-only, and gated.** `groundtruth/server.py` generates the MCP
   tool surface from the project. Edits reload live, but only after they pass
   `./gt check`; a failing edit is rejected, the last good version keeps
   serving, and `/healthz` reports the rejection.

## Modules

| Module | Job |
|---|---|
| `project.py` | Find the active project and its paths |
| `facts.py` | Load, validate, build, and query facts; generate tool specs |
| `search.py`, `embeddings.py` | Wiki search: keyword scan, optional embeddings |
| `server.py` | The MCP server, auth, host rules, `/healthz` |
| `chat.py` | `/chat`: a public web chat on the same tools and instructions, rate-limited |
| `checks.py` | `./gt check`: facts, source review, wiki structure, prose lint, golden wiring, prompt |
| `review.py` | `sources.lock.yaml`: detects evidence that changed since facts and pages were reviewed |
| `examples.py` | Finds and removes a template's fictional example content |
| `evals.py`, `llm.py` | The golden-set harness; Claude for live evals |
| `ingest.py` | Website, sitemap, and folder capture |
| `compile.py` | Coverage and staleness; agent work orders; Claude drafting |
| `observability.py`, `report.py` | Tool-call log and the dashboard |
| `watchdog.py` | Outside-in health checks over real MCP |
| `refresh.py` | The weekly agent (a PR), or the refresh host's nightly and quick refresh (a push) |
| `rewrite.py` | `./gt rewrite`: headless Claude Code, confined to the files it updates; rewrites stale pages, then gates and a second model's claim check |
| `reconcile.py` | Updates facts from a changed source; ships only grounded, gated, model-checked rows |
| `cover.py` | Writes a page (and facts) for a source nothing covers yet, under the same checks |
| `grounding.py` | Every exact value in `facts/` must appear in the source its row cites, where it describes that row |
| `repair.py` | Fixes what the live eval got wrong (synonyms, prompt, pages, aliases), then re-asks live |
| `claude_code.py` | Live evals through headless Claude Code and the MCP server, for a Claude plan with no API key |
| `host.py` | The refresh host: GitHub Actions (`autopilot`) or a machine (worktrees at main, locks, the watcher, cron); one named pusher |
| `alerts.py` | What the unattended jobs can't handle, into `ALERTS.md` where the watchdog sees it |
| `bundle.py` | Data bundles and hot-swap for deployed servers |
| `progress.py` | `./gt status`: the road to production and the next step |
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
5. review       changed evidence is reconciled: by the refresh host's checked rewrites, or a person
                (./gt review --done) merging the weekly PR
```

`./gt refresh` runs it; `.github/workflows/refresh.yml` schedules it weekly, and a
person merges the pull request.

A refresh host (`./gt host`: GitHub Actions, or one machine) runs the same loop
with nobody merging. It ingests nightly, and every two hours for the sources
facts cite. `./gt rewrite` then does steps 3 and 5 itself, with headless Claude
Code:

- pages whose sources are all gone are retired (no model involved);
- facts whose source changed are updated, or retired with it;
- new exact values in a source only pages cite become fact rows;
- sources nothing covers get a page;
- stale pages are rewritten.

Every pass is confined to the files it may touch, and it ships only when:

- every exact value appears in the cited source, where it describes its row (`grounding.py`), and the
  file's `rules:` hold;
- the gates find nothing new;
- a second model with read-only tools agrees;
- the golden questions it touches, asked of the production model, don't get
  worse than what's live, and any question it added passes.

What the live eval still gets wrong goes to `./gt repair` (`repair.py`), which
may change only synonyms, the prompt, pages, and aliases, and ships under the
same checks.

Those checks are the review: once everything citing a source has passed them,
the source is recorded as reviewed (`facts_updates: human` keeps changed facts
for a person instead). Exactly one host pushes, named in `groundtruth.yaml`.
What it can't handle lands in `ALERTS.md`, and the daily watchdog fails on it.

## What "ready to ship" means

- `./gt check --production` and `./gt eval` pass (the Docker build, data bundles, and
  publish-data enforce them; CI runs `./gt check` and `./gt eval`);
- `./gt eval --tier release` passes 100% on the production model;
- no facts in wiki prose; every fact has a source;
- the server is deployed with an API key, watched by the watchdog, and the
  weekly refresh (or a refresh host) is on.
