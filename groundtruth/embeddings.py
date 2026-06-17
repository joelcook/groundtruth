"""The optional semantic backend behind ``search`` — an embedding store you can
actually understand, end to end.

The architecture is the point, not the model:

    text ──▶ Embedder ──▶ vector ──▶ VectorStore (SQLite) ──▶ cosine top-k

Three swappable parts:
  * **Embedder** — turns text into a vector. Ships with a dependency-free
    ``HashingEmbedder`` so the whole pipeline runs offline on a fresh clone.
    It captures *lexical* overlap, not deep meaning — it exists to exercise the
    vector path honestly, not to be good. Swap in a real model (one method) and
    everything downstream is unchanged.
  * **VectorStore** — persists vectors next to your facts, in plain SQLite
    (``db/embeddings.db``). No vector server to run. At scale, swap the cosine
    loop for the ``sqlite-vec`` extension and keep the same table.
  * **query()** — embeds the query and returns the nearest page ids.

Why this, and not "just use a vector DB"? Because for a few hundred curated
pages a brute-force cosine over SQLite is milliseconds, has zero operational
surface, and keeps your embeddings versioned in the same place as your facts.
You add infrastructure when the corpus forces you to — see docs/01-retrieval.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "db" / "embeddings.db"
TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------- Embedder

@runtime_checkable
class Embedder(Protocol):
    """Anything that turns a batch of strings into fixed-length vectors."""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class HashingEmbedder:
    """Deterministic, dependency-free hashed bag-of-words vectors.

    Each token is hashed into one of ``dim`` buckets and accumulated with a
    sublinear term weight, then the vector is L2-normalized. Two texts that
    share vocabulary end up with a high cosine. This is a *stand-in*: it gives
    you a real vector pipeline with no downloads and no API calls, which is
    perfect for a demo and for tests, and useless for true semantic search.

    To go real, delete this and use the provider stub below (or sentence-
    transformers). Nothing else in the codebase changes.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            counts: dict[str, int] = {}
            for tok in TOKEN_RE.findall(text.lower()):
                if len(tok) < 2:
                    continue
                counts[tok] = counts.get(tok, 0) + 1
            for tok, c in counts.items():
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                bucket = h % self.dim
                sign = 1.0 if (h >> 8) & 1 else -1.0
                vec[bucket] += sign * (1.0 + math.log(c))
            out.append(_l2_normalize(vec))
        return out


class ProviderEmbedder:
    """Reference adapter for a real embeddings API (here: Voyage AI, what the
    Anthropic ecosystem recommends). Not wired by default so the demo stays
    offline. Set GT_EMBEDDER=voyage and VOYAGE_API_KEY to use it.

    The same shape works for any provider — implement ``embed`` and set ``dim``.
    """

    def __init__(self, model: str = "voyage-3-lite") -> None:
        self.name = model
        self.dim = 512  # set to your model's dimensionality
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        import voyageai  # type: ignore

        client = voyageai.Client()  # reads VOYAGE_API_KEY
        resp = client.embed(texts, model=self._model, input_type="document")
        self.dim = len(resp.embeddings[0])
        return [list(v) for v in resp.embeddings]


def get_embedder() -> Embedder:
    """Pick the embedder from GT_EMBEDDER (default: the offline hashing stand-in)."""
    choice = os.environ.get("GT_EMBEDDER", "hashing").strip().lower()
    if choice in ("voyage", "provider"):
        return ProviderEmbedder(os.environ.get("GT_EMBED_MODEL", "voyage-3-lite"))
    return HashingEmbedder(int(os.environ.get("GT_EMBED_DIM", "256")))


# ---------------------------------------------------------------- vector math

def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    # Vectors are stored already L2-normalized, so this is just a dot product.
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------- VectorStore

def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or INDEX_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings ("
        "  id TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vector TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    return conn


def build_index(items: list[tuple[str, str]], embedder: Embedder | None = None) -> int:
    """Embed ``items`` (id, text) and persist them. Returns the number indexed."""
    embedder = embedder or get_embedder()
    if INDEX_PATH.exists():
        INDEX_PATH.unlink()
    conn = _connect()
    texts = [text for _, text in items]
    vectors = embedder.embed(texts) if texts else []
    for (item_id, _), vec in zip(items, vectors):
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (id, model, dim, vector) VALUES (?,?,?,?)",
            (item_id, embedder.name, embedder.dim, json.dumps(vec)),
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)", (embedder.name,)
    )
    conn.commit()
    conn.close()
    return len(items)


def query(text: str, limit: int = 5, embedder: Embedder | None = None) -> list[tuple[str, float]]:
    """Return up to ``limit`` (id, cosine_score) nearest neighbors, or [] if no index."""
    if not INDEX_PATH.exists():
        return []
    embedder = embedder or get_embedder()
    qvec = embedder.embed([text])[0]
    conn = _connect()
    rows = conn.execute("SELECT id, vector FROM embeddings").fetchall()
    conn.close()
    scored = [(rid, _cosine(qvec, json.loads(vec))) for rid, vec in rows]
    scored.sort(key=lambda t: (-t[1], t[0]))
    return [(rid, score) for rid, score in scored[:limit] if score > 0]


def index_status() -> dict:
    """Diagnostics: is there an index, how big, which model built it."""
    if not INDEX_PATH.exists():
        return {"exists": False, "count": 0, "model": None}
    conn = _connect()
    count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    conn.close()
    return {"exists": True, "count": count, "model": (row[0] if row else None)}
