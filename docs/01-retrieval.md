# Pillar 1 — Retrieval over something you care about

The single most important decision in a grounded system is *what kind of wrong
you can tolerate for each question*. groundtruth splits retrieval along exactly
that line.

## Facts vs. narrative

| | Narrative | Hard facts |
|---|---|---|
| examples | "how do I get a card?", "how do holds work?" | dates, dollars, hours, phone numbers |
| tolerance | approximate is fine | must be exact |
| stored as | source-cited markdown (`wiki/`) | typed rows (`db/groundtruth.db`) |
| reached via | `search_wiki`, `read_wiki_page` | `get_fee`, `get_event`, `get_hours`, … |
| if wrong | mildly unhelpful | real-world harm |

Mixing these is the original sin of "chatbot on our docs." A vector store
retrieves a paragraph that *says* the fine is 25¢/day, and the model repeats it
with total confidence a year after the policy changed. groundtruth makes that
structurally impossible: the price lives in a row, the row has a source, and the
prose that used to hold the number is forbidden from holding it
(`scripts/lint_no_hardfacts.py`).

## 1.5 · Storing it efficiently: a wiki + SQLite

**SQLite for facts.** `db/schema.sql` defines a handful of tables; every row
carries `source_path` + `source_url`. `groundtruth/db.py` exposes read-only
query functions with forgiving inputs and exact outputs. The database is a
*projection* — `scripts/build_db.py` rebuilds it from `sources/` deterministically,
so there is no model in the loop to hallucinate a fact. It's read-only at runtime
(`mode=ro`), tiny, and committed to the repo so a fresh clone works instantly.

**A wiki for narrative.** `wiki/` is markdown with YAML frontmatter and a
`Sources & Lineage` table on every page. The corpus is deliberately small — tens
of pages, not millions — because a corpus you can read is a corpus you can
govern. The wiki is for *meaning*; it points at tools for *values*.

Why this combo and not a document store or a knowledge graph? Because it's the
least machinery that gives you typed, queryable, provenanced facts **and**
flexible prose, both in plain files you can diff in a PR.

## 1.8 · The embedding store, made useful

Retrieval is two tiers behind one interface (`groundtruth/search.py`), selected
by `GT_SEARCH_BACKEND`:

### Tier 1 — `scan` (the default, and probably all you need)

A dependency-free lexical scorer holds the whole wiki in memory and ranks it in
about a millisecond: phrase hits, title/heading/tag/path weighting, light
stemming, and a small per-domain `SYNONYMS` map you tune from real misses. No
model, no index, no server, no network.

**This is a feature, not a placeholder.** For a few hundred curated pages, good
lexical search routinely beats a mediocre embedding setup, with none of the
operational cost. Start here. Reach for vectors when you can *measure* (via the
eval harness) that lexical recall is the bottleneck — not before.

### Tier 2 — `semantic` / `auto` (embeddings, when you've earned them)

`groundtruth/embeddings.py` is a complete, legible embedding store:

```
text ──▶ Embedder ──▶ vector ──▶ VectorStore (SQLite) ──▶ cosine top-k
```

- **Embedder** — a `Protocol` with one method, `embed(texts) -> vectors`. Ships
  with `HashingEmbedder`: deterministic, offline, zero-dependency, so the vector
  path runs on a fresh clone and in tests. It captures lexical overlap, *not*
  deep meaning — it exists to be honest, not good. To go real, set
  `GT_EMBEDDER=voyage` (or implement `embed` for any provider / a local
  sentence-transformers model). **Nothing downstream changes.**
- **VectorStore** — vectors persist in plain SQLite (`db/embeddings.db`), L2-
  normalized, scored with a dot-product loop. No vector database to operate; your
  embeddings are versioned alongside your facts.
- **`auto`** blends lexical + semantic by reciprocal rank, so you get semantic
  recall ("I phrased it differently than the page did") with the lexical score as
  a stabilizer. With no index built, `auto` degrades gracefully to `scan`.

```bash
python scripts/build_index.py                          # wiki/*.md → db/embeddings.db
GT_SEARCH_BACKEND=auto python scripts/serve_mcp.py
```

### Scaling past the example

The brute-force cosine is O(n) per query — perfect to tens of thousands of
chunks. When you outgrow it, swap the scoring loop for the
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension and keep the same
table and the same `search_wiki` interface. The point of the design is that the
*interface* is stable; the *backend* is a detail you upgrade when measurements —
not vibes — tell you to.

## Where to look

- `db/schema.sql`, `groundtruth/db.py`, `scripts/build_db.py` — facts
- `wiki/`, `groundtruth/search.py` — narrative + lexical retrieval
- `groundtruth/embeddings.py`, `scripts/build_index.py` — the embedding store
