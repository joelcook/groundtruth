"""Narrative retrieval over the curated wiki.

This answers the *how/why/what* questions — the ones where being approximately
right is fine. (The *exact* questions go to ``groundtruth.db``.) It returns
source-cited snippets the model can ground an explanation in.

Two backends, one interface:
  * **scan** (default) — a dependency-free lexical scorer that holds the whole
    wiki in memory and ranks it in ~1 ms. For a few hundred curated pages this
    is genuinely hard to beat, and it has zero moving parts. Start here.
  * **semantic** — embeddings via ``groundtruth.embeddings``. Selected when you
    set ``GT_SEARCH_BACKEND=semantic`` or ``=auto`` and an index exists.

``auto`` blends both: semantic recall for "I phrased it differently than the
page did" queries, with the lexical score as a stabilizer. Build the index with
``python scripts/build_index.py``; without it, ``auto`` degrades to ``scan``.

See docs/01-retrieval.md for when each is the right call.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = REPO_ROOT / "wiki"

PUBLIC_PAGE_TYPES = {"journey", "process", "policy", "entity", "comparison"}
SKIP_FILENAMES = {"index.md", "log.md", "gaps.md"}
MAX_SNIPPET_CHARS = 600

TOKEN_RE = re.compile(r"[a-z0-9]+")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does",
    "for", "from", "get", "how", "i", "if", "in", "is", "it", "my", "of", "on",
    "or", "the", "this", "to", "what", "when", "where", "who", "why", "with", "you",
}

# Domain-specific query expansion. THIS IS THE ONE PART YOU TUNE PER CORPUS:
# map the words your users type to the words your pages actually use. Keep it
# small and earn each entry from a real miss. (Example: library domain.)
SYNONYMS = {
    "card": {"card", "registration", "signup", "register"},
    "fine": {"fine", "fines", "overdue", "late", "fee"},
    "fines": {"fine", "fines", "overdue", "late", "fee"},
    "print": {"print", "printing", "copy", "copies"},
    "ebook": {"ebook", "ebooks", "audiobook", "digital", "app", "elibrary"},
    "ebooks": {"ebook", "ebooks", "audiobook", "digital", "app", "elibrary"},
    "hold": {"hold", "holds", "reserve", "request"},
    "renew": {"renew", "renewal", "renewals"},
    "wifi": {"wifi", "internet", "computer", "computers"},
    "pin": {"pin", "password", "login", "sign"},
}


@dataclass
class WikiPage:
    rel_path: str
    meta: dict[str, Any]
    body: str
    title: str
    page_type: str
    tags: list[str]
    search_text: str
    title_tokens: Counter = field(default_factory=Counter)
    heading_tokens: Counter = field(default_factory=Counter)
    tag_tokens: Counter = field(default_factory=Counter)
    path_tokens: Counter = field(default_factory=Counter)
    body_tokens: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------- parsing

def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def _strip_lineage(body: str) -> str:
    return re.split(r"\n##\s+Sources\s+&\s+Lineage\b", body, maxsplit=1, flags=re.I)[0].strip()


def _plain(markdown: str) -> str:
    text = WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1).split("/")[-1], markdown)
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[*_>#|~`-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return [
        t for t in TOKEN_RE.findall(text.lower())
        if t not in STOPWORDS and (len(t) >= 3 or any(c.isdigit() for c in t))
    ]


def _stem(token: str) -> str:
    for suf in ("ing", "ions", "tion", "ies", "ed", "es", "s"):
        if token.endswith(suf) and len(token) > len(suf) + 2:
            return token[: -len(suf)] + ("y" if suf == "ies" else "")
    return token


def _query_terms(query: str) -> list[str]:
    terms = set(_tokens(query))
    for t in list(terms):
        terms |= SYNONYMS.get(t, set())
        terms.add(_stem(t))
    return sorted(t for t in terms if t and t not in STOPWORDS)


def _load_public_pages() -> list[WikiPage]:
    pages: list[WikiPage] = []
    for path in sorted(WIKI_DIR.rglob("*.md")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.name in SKIP_FILENAMES or "internal" in path.relative_to(WIKI_DIR).parts:
            continue
        meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        page_type = str(meta.get("page_type") or "").strip().lower()
        if page_type not in PUBLIC_PAGE_TYPES or str(meta.get("audience", "public")) == "internal":
            continue
        body = _strip_lineage(body)
        title = str(meta.get("title") or path.stem.replace("-", " ").title())
        tags = [str(t) for t in (meta.get("tags") or [])]
        headings = " ".join(HEADING_RE.findall(body))
        plain_body = _plain(body)
        search_text = " ".join([title, page_type, " ".join(tags),
                                 path.stem.replace("-", " "), headings, plain_body])
        pages.append(WikiPage(
            rel_path=rel, meta=meta, body=body, title=title, page_type=page_type,
            tags=tags, search_text=search_text,
            title_tokens=Counter(_tokens(title)),
            heading_tokens=Counter(_tokens(headings)),
            tag_tokens=Counter(_tokens(" ".join(tags))),
            path_tokens=Counter(_tokens(path.stem.replace("-", " "))),
            body_tokens=Counter(_tokens(plain_body)),
        ))
    return pages


# ---------------------------------------------------------------- scoring

def _score(page: WikiPage, query: str, terms: list[str]) -> float:
    phrase = re.sub(r"\s+", " ", query.lower()).strip()
    score = 0.0
    if len(phrase) >= 4:
        if phrase in page.title.lower():
            score += 80.0
        if phrase in page.search_text.lower():
            score += 20.0
    for term in terms:
        stem = _stem(term)
        score += page.title_tokens.get(term, 0) * 18.0
        score += page.tag_tokens.get(term, 0) * 14.0
        score += page.path_tokens.get(term, 0) * 12.0
        score += page.heading_tokens.get(term, 0) * 8.0
        body = page.body_tokens.get(term, 0)
        if body:
            score += min(12.0, 1.2 * body) + math.log(body + 1.0)
        for token in page.title_tokens:
            if _stem(token) == stem:
                score += 12.0
    return score


def _first_paragraph(body: str) -> str:
    for para in re.split(r"\n\s*\n", body):
        raw = para.strip()
        if raw and not raw.startswith("#"):
            clean = _plain(para)
            if clean:
                return clean[:MAX_SNIPPET_CHARS]
    return ""


def _snippet(page: WikiPage, terms: list[str]) -> str:
    for para in re.split(r"\n\s*\n", page.body):
        raw = para.strip()
        if not raw or raw.startswith("#"):
            continue
        clean = _plain(para)
        if any(t in clean.lower() for t in terms):
            return clean[:MAX_SNIPPET_CHARS]
    return _first_paragraph(page.body)


def _result(page: WikiPage, query: str, terms: list[str], backend: str,
            score: float | None) -> dict:
    sources = page.meta.get("sources") or []
    if not isinstance(sources, list):
        sources = [sources]
    out = {
        "path": page.rel_path,
        "title": page.title,
        "page_type": page.page_type,
        "status": page.meta.get("status"),
        "tags": page.tags,
        "snippet": _snippet(page, terms) if terms else _first_paragraph(page.body),
        "sources": [str(s) for s in sources],
        "backend": backend,
    }
    if score is not None:
        out["score"] = round(score, 3)
    return out


# ---------------------------------------------------------------- backends

def _search_scan(query: str, limit: int, pages: list[WikiPage]) -> list[dict]:
    terms = _query_terms(query)
    if not terms:
        return []
    scored = [(_score(p, query, terms), p) for p in pages]
    scored = [(s, p) for s, p in scored if s > 0]
    scored.sort(key=lambda t: (-t[0], t[1].rel_path))
    return [_result(p, query, terms, "scan", s) for s, p in scored[:limit]]


def _search_semantic(query: str, limit: int, pages: list[WikiPage]) -> list[dict]:
    from groundtruth import embeddings
    hits = embeddings.query(query, limit=limit)
    if not hits:
        return []
    by_path = {p.rel_path: p for p in pages}
    terms = _query_terms(query)
    out = []
    for rel_path, score in hits:
        page = by_path.get(rel_path)
        if page is not None:
            out.append(_result(page, query, terms, "semantic", score))
    return out


def _search_auto(query: str, limit: int, pages: list[WikiPage]) -> list[dict]:
    """Blend lexical + semantic by reciprocal rank; falls back to scan with no index."""
    from groundtruth import embeddings
    sem = embeddings.query(query, limit=max(limit * 2, 6))
    if not sem:
        return _search_scan(query, limit, pages)
    by_path = {p.rel_path: p for p in pages}
    terms = _query_terms(query)
    ranks: dict[str, float] = {}
    for rank, row in enumerate(_search_scan(query, max(limit * 2, 6), pages), start=1):
        ranks[row["path"]] = ranks.get(row["path"], 0.0) + 1.0 / rank
    for rank, (rel_path, _score_val) in enumerate(sem, start=1):
        ranks[rel_path] = ranks.get(rel_path, 0.0) + 1.5 / rank
    ordered = sorted(ranks, key=lambda p: (-ranks[p], p))
    results = []
    for rel_path in ordered[:limit]:
        page = by_path.get(rel_path)
        if page is not None:
            results.append(_result(page, query, terms, "auto", round(ranks[rel_path], 3)))
    return results


# ---------------------------------------------------------------- public API

def search_wiki(query: str, limit: int = 5) -> list[dict]:
    """Search the curated wiki for narrative/process/policy context.

    Use ``groundtruth.db`` tools instead for any exact value (dates, dollars,
    hours, phone numbers). Backend is chosen by ``GT_SEARCH_BACKEND``
    (scan | semantic | auto); default scan.
    """
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 10))
    pages = _load_public_pages()
    backend = os.environ.get("GT_SEARCH_BACKEND", "scan").strip().lower()
    if backend == "semantic":
        return _search_semantic(query, limit, pages) or _search_scan(query, limit, pages)
    if backend == "auto":
        return _search_auto(query, limit, pages)
    return _search_scan(query, limit, pages)


def _normalize(value: str) -> str:
    return (value or "").strip().lower().removeprefix("wiki/").removesuffix(".md").replace("_", "-")


def read_wiki_page(path_or_slug: str, max_chars: int = 10000) -> dict | None:
    """Read one wiki page in full by repo path, wiki-relative path, slug, or title."""
    needle = _normalize(path_or_slug)
    if not needle:
        return None
    for page in _load_public_pages():
        candidates = {
            _normalize(page.rel_path),
            _normalize(Path(page.rel_path).name),
            _normalize(page.title),
        }
        if needle in candidates:
            content = page.body.strip()
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars].rstrip() + "\n\n[truncated]"
            sources = page.meta.get("sources") or []
            return {
                "path": page.rel_path,
                "title": page.title,
                "page_type": page.page_type,
                "tags": page.tags,
                "sources": [str(s) for s in (sources if isinstance(sources, list) else [sources])],
                "content": content,
                "truncated": truncated,
            }
    return None


def build_search_index() -> int:
    """Embed every public page's search text into the vector store. Returns count."""
    from groundtruth import embeddings
    pages = _load_public_pages()
    return embeddings.build_index([(p.rel_path, p.search_text) for p in pages])


def search_status() -> dict:
    """Diagnostics for the retrieval layer."""
    from groundtruth import embeddings
    backend = os.environ.get("GT_SEARCH_BACKEND", "scan").strip().lower()
    return {
        "configured_backend": backend,
        "public_page_count": len(_load_public_pages()),
        "index": embeddings.index_status(),
    }
