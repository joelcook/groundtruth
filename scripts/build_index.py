#!/usr/bin/env python3
"""Build the semantic search index (optional Pillar-1 upgrade).

    wiki/*.md  ──embed──▶  db/embeddings.db

You do NOT need this to run groundtruth — the default lexical scanner needs no
index. Build it only when you want the semantic / auto backends:

    python scripts/build_index.py
    GT_SEARCH_BACKEND=auto python scripts/serve_mcp.py

By default it uses the offline HashingEmbedder (no downloads, no API key). Point
it at a real model with GT_EMBEDDER (see groundtruth/embeddings.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundtruth import embeddings, search  # noqa: E402


def main() -> int:
    count = search.build_search_index()
    status = embeddings.index_status()
    print(f"indexed {count} page(s) with embedder '{status['model']}'")
    print(f"  → {embeddings.INDEX_PATH.relative_to(embeddings.REPO_ROOT)}")
    print("set GT_SEARCH_BACKEND=auto (or =semantic) to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
