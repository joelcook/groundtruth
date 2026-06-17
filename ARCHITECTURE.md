# Architecture

groundtruth makes a corpus *AI-ready at the data layer*. It converts sources into
two maintained assets and serves them to a model over MCP. The model uses the
wiki for explanation and the database for volatile or exact facts.

## The core contract

The runtime must not answer a hard fact from memory or from compiled prose. It
calls a tool. In the example domain:

- dates/times of events → `get_event` / `list_events`
- opening hours → `get_hours`
- dollar amounts → `get_fee`
- contacts → `get_contact`
- program registration → `get_program` / `list_programs`

If a wiki page disagrees with a tool result, the tool wins and the page is
recompiled or corrected. Narrative may be approximate; facts may not be wrong.

## Three layers, each derived from the last

```
sources/ ──project──▶ db/groundtruth.db   (deterministic; scripts/build_db.py)
sources/ ──compile──▶ wiki/                (LLM + human review; scripts/recompile.py)
db + wiki ──serve──▶ MCP server            (groundtruth/mcp.py)
```

1. **Evidence (`sources/`)** is immutable. You never hand-tune the database or
   write a fact straight into a page; you change the evidence and re-derive. This
   is what makes every answer traceable and every refresh safe.
2. **Projection (`db/`)** is mechanical and deterministic — no model in the loop,
   so it can't hallucinate. Rebuilt from scratch every time.
3. **Compilation (`wiki/`)** is the only place an LLM authors text, and it is
   fenced: dry-run by default, review branch on write, linted for stray facts.
4. **Serving (MCP)** is read-only and is the single interface the model sees.

## Why a small, governed corpus beats big RAG

The dominant pattern — embed everything, retrieve top-k, hope — optimizes for
*coverage* and quietly sacrifices *correctness*. groundtruth makes the opposite
bet: a curated corpus small enough that a human can govern it, with the
dangerous facts pulled out into a typed store that cannot drift into prose. You
trade recall you don't need for correctness you can't live without. When an
answer is wrong, the fix is usually upstream in the source — not a patch here.

## The governance loop

```
1. refresh evidence in sources/         (your scraper, or hand-authored)
2. rebuild db/ and the search index     (deterministic)
3. recompile stale wiki pages           (LLM, onto a review branch)
4. run the gates + the Golden Set        (deterministic + judged)
5. open a draft PR for human review
```

`scripts/refresh_pipeline.sh` is the wrapper;
`scripts/com.groundtruth.refresh.plist` schedules it. See
[docs/02-weekly-agent.md](docs/02-weekly-agent.md).

## Readiness bar

Ship when:

- the Golden Set passes 100% on the production model (`run_evals.py --tier release`);
- `check_wiki.py`, `lint_no_hardfacts.py`, and the unit tests are green;
- the MCP runtime contract passes against the deployed endpoint
  (`check_mcp_runtime.py`);
- the system prompt in your chat platform matches `prompts/system_prompt.md`.

## Module map

| module | role |
|---|---|
| `groundtruth/db.py` | read-only hard-fact queries over SQLite |
| `groundtruth/search.py` | lexical + semantic narrative retrieval |
| `groundtruth/embeddings.py` | the pluggable embedding store |
| `groundtruth/mcp.py` | the read-only MCP tool server + auth |
| `groundtruth/evals.py` | the eval harness (graders, tiers, runner) |
| `groundtruth/observability.py` | per-tool-call logging |

See [docs/01-retrieval.md](docs/01-retrieval.md),
[docs/02-weekly-agent.md](docs/02-weekly-agent.md), and
[docs/03-eval-harness.md](docs/03-eval-harness.md) for the three pillars in depth,
and [docs/mcp-tool-contract.md](docs/mcp-tool-contract.md) for the tool contract.
