"""groundtruth — give your LLM a single source of truth.

A small framework for grounding a language model in a domain you care about:

    db          hard facts (dates, dollars, hours, contacts) — never paraphrased
    search      narrative retrieval over a curated, source-cited wiki
    embeddings  the optional semantic backend behind `search`
    mcp         the read-only tool server the model actually talks to
    evals       the harness that proves the whole thing still behaves

The bet: keep the corpus small enough to govern, route every harm-if-wrong fact
through a tool, and gate every change with evals. See README.md and docs/.
"""

__version__ = "0.1.0"
