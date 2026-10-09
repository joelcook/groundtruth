# Retrieval

The most important decision in a grounded assistant is *what kind of wrong you
can tolerate for each question*. groundtruth splits retrieval along exactly that
line.

| | Explanations | Facts |
|---|---|---|
| Examples | "how do I get a card?", "how do refunds work?" | dates, dollars, hours, phone numbers |
| Tolerance | approximately right is fine | must be exact |
| Stored as | source-cited markdown in `wiki/` | rows in `facts/*.yaml` |
| Reached through | `search_wiki`, `read_wiki_page` | your generated tools (`get_fee`, ...) |
| If it's wrong | mildly unhelpful | real-world harm |

Mixing them is the classic "chatbot on our docs" mistake: a vector store fetches
a paragraph that *says* the fine is 25 cents a day, and the model repeats it with
total confidence a year after the policy changed. Here the price lives in a row,
the row has a source, and the prose that used to hold the number is forbidden
from holding it.

## Wiki search: three backends, one interface

Choose with `GT_SEARCH_BACKEND`.

### `scan` (default, and probably all you need)

A dependency-free keyword scorer holds the whole wiki in memory and ranks it in
about a millisecond: phrase hits, title, heading, tag, and path weighting, light
stemming, and your synonyms. No model, no index, no server.

For a few hundred curated pages, good keyword search routinely beats a mediocre
embedding setup, with none of the cost. Start here.

**Tune it with synonyms**, in `groundtruth.yaml`:

```yaml
search:
  synonyms:
    password: [login, sign, reset, locked]
    refund: [refunds, money, cancel]
```

Earn each entry from a real miss: `./gt report` shows searches that found
nothing, and `./gt eval` fails when a question stops finding its page.

### `semantic` and `auto` (when you've earned them)

```bash
./gt build --index                          # embed every page into build/search-index.db
GT_SEARCH_BACKEND=auto ./gt serve           # keyword + embeddings, blended by rank
```

The embeddings live in a plain SQLite file next to your facts: no vector database
to run. The default embedder is an offline hashing stand-in, there to exercise
the pipeline without downloads. For real semantic recall:

```bash
./gt setup --voyage
GT_EMBEDDER=voyage VOYAGE_API_KEY=... ./gt build --index
```

Or implement `embed(texts) -> vectors` for any provider in
`groundtruth/embeddings.py`; nothing downstream changes. Reach for embeddings
when the eval shows keyword recall is the bottleneck, not before.

## Fact lookups

Fact tools don't use the wiki search at all. A `lookup` tool scores rows by
exact key or alias, then substring, then word overlap; a `list` tool filters by
field values. See [facts.md](facts.md). Aliases are the facts layer's synonyms:
add one each time a lookup misses.
