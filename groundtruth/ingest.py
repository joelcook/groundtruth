"""``./gt ingest``: pull your evidence into ``sources/pages/`` as clean markdown.

    ./gt ingest https://example.org              # sitemap if there is one, else crawl
    ./gt ingest https://example.org/sitemap.xml  # exactly the pages in a sitemap
    ./gt ingest ./exports/                       # a folder of .md / .txt / .html / .pdf
    ./gt ingest                                  # re-run everything in groundtruth.yaml

Every page gets frontmatter recording where it came from, when, and a content
hash. A page whose content hasn't changed is left alone, so re-running is cheap
and your git diff only shows real changes. Crawls stay on the starting site,
respect robots.txt, and stop at ``--max-pages`` (default 200).

Ingest never touches facts/ or wiki/. It refreshes the evidence; ``./gt compile``
helps turn new evidence into wiki pages and fact rows.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import yaml

from groundtruth import __version__
from groundtruth.project import Project

USER_AGENT = f"groundtruth-ingest/{__version__} (+https://github.com/joelcook/groundtruth)"
SKIP_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".mp4", ".mp3", ".css", ".js",
            ".ico", ".xml", ".json", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".woff", ".woff2"}
DROP_TAGS = ["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe", "button"]
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_)")


@dataclass
class IngestResult:
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.new or self.updated or self.removed)

    def summary(self) -> str:
        return (f"{len(self.new)} new, {len(self.updated)} updated, {len(self.unchanged)} unchanged"
                + (f", {len(self.removed)} removed upstream" if self.removed else "")
                + (f", {len(self.skipped)} skipped" if self.skipped else ""))


# ===================================================================== html -> markdown


def html_to_markdown(html: str, url: str = "") -> tuple[str, str]:
    """Return (title, markdown body) for an HTML page, minus the navigation chrome."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    for tag in soup(DROP_TAGS):
        tag.decompose()
    for el in soup.select('[role="navigation"], [aria-hidden="true"], .cookie, #cookie, .skip-link'):
        el.decompose()
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup
    md = markdownify(str(main), heading_style="ATX", bullets="-", strip=["img"])
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    if url:
        md = re.sub(r"\]\((/[^)]*)\)", lambda m: f"]({urljoin(url, m.group(1))})", md)
    return (title or urlparse(url).path.strip("/").replace("-", " ").title() or "Untitled"), md


# ===================================================================== writing pages


def slug_for(url_or_name: str) -> str:
    parsed = urlparse(url_or_name)
    raw = (parsed.path if parsed.scheme else url_or_name).strip("/")
    raw = re.sub(r"\.(html?|php|aspx?)$", "", raw)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if parsed.query:
        slug += "-" + hashlib.sha1(parsed.query.encode()).hexdigest()[:6]
    return (slug or "index")[:120]


def page_meta(path: Path) -> dict:
    if not path.exists():
        return {}
    m = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        return {}
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _origin(meta: dict) -> str | None:
    value = meta.get("source_url") or meta.get("source_path")
    return str(value) if value else None


def local_path(project: Project, value: str) -> Path:
    """Where a capture's source_path points. New captures store it relative to the project, so a
    commit never carries someone's home directory; older ones may be absolute."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else project.root / path).resolve()


def portable_path(project: Project, path: Path) -> str:
    """A local document's path as stored in its capture: relative to the project."""
    import os
    try:
        return Path(os.path.relpath(path.resolve(), project.root.resolve())).as_posix()
    except ValueError:  # another drive on Windows
        return str(path.resolve())


def _origin_key(project: Project, meta: dict) -> str | None:
    """An origin comparable however it was stored (URL, or a relative or absolute path)."""
    if meta.get("source_url"):
        return str(meta["source_url"])
    return str(local_path(project, str(meta["source_path"]))) if meta.get("source_path") else None


def history_dir(project: Project) -> Path:
    return project.sources_dir / ".history"


def save_history(project: Project, path: Path) -> None:
    """Keep the version being replaced, so ./gt review --diff can show what changed."""
    from groundtruth import review
    if not path.exists():
        return
    rel = path.relative_to(project.pages_dir).as_posix()
    dest = history_dir(project) / f"{rel}@{review.file_hash(path)}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(path.read_bytes())


def capture_for(project: Project, origin: str) -> Path | None:
    """The existing capture of this URL or file (an origin key), whatever it's named."""
    for path, meta in _existing_pages(project):
        if _origin_key(project, meta) == origin:
            return path
    return None


def write_page(project: Project, slug: str, title: str, body: str, origin: dict, result: IngestResult,
               dry_run: bool = False) -> None:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    me = _origin_key(project, origin)
    existing = capture_for(project, me) if me else None
    path = existing or project.pages_dir / f"{slug}.md"
    meta = page_meta(path)
    if existing is None and path.exists() and _origin_key(project, meta) not in (None, me):
        # Two different documents want the same name: keep both, deterministically.
        path = project.pages_dir / f"{slug}-{hashlib.sha1(str(me).encode()).hexdigest()[:6]}.md"
        meta = page_meta(path)
    rel = project.rel(path)
    if path.exists() and meta.get("sha256") == digest and not meta.get("removed_upstream"):
        result.unchanged.append(rel)
        return
    (result.updated if path.exists() else result.new).append(rel)
    if dry_run:
        return
    save_history(project, path)
    new_meta = {"title": title, **origin, "captured": dt.date.today().isoformat(), "sha256": digest}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(new_meta, sort_keys=False, allow_unicode=True) + "---\n\n"
                    + body.strip() + "\n", encoding="utf-8")


def mark_removed(project: Project, path: Path, result: IngestResult, dry_run: bool = False) -> None:
    """The document is gone upstream. Keep the evidence, but flag it so anything citing it fails ./gt check."""
    meta = page_meta(path)
    if meta.get("removed_upstream"):
        return
    result.removed.append(project.rel(path))
    if dry_run:
        return
    save_history(project, path)
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    body = text[m.end():] if m else text
    meta["removed_upstream"] = dt.date.today().isoformat()
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n"
                    + body.lstrip("\n"), encoding="utf-8")


def _existing_pages(project: Project) -> list[tuple[Path, dict]]:
    if not project.pages_dir.exists():
        return []
    return [(p, page_meta(p)) for p in sorted(project.pages_dir.rglob("*.md"))]


# ===================================================================== web


class Fetcher:
    def __init__(self, delay: float = 0.25):
        import httpx

        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
        self.delay = delay
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last = 0.0

    def allowed(self, url: str) -> bool:
        base = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if base not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:  # noqa: BLE001 - no robots.txt means no rules
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def get(self, url: str):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        return self.client.get(url)


def _clean_url(url: str) -> str:
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    if parsed.query:
        kept = "&".join(p for p in parsed.query.split("&") if not TRACKING_PARAMS.match(p.split("=")[0]))
        parsed = parsed._replace(query=kept)
    return parsed._replace(path=parsed.path or "/").geturl()


def sitemap_urls(fetcher: Fetcher, url: str, limit: int, _depth: int = 0) -> list[str]:
    try:
        r = fetcher.get(url)
    except Exception:  # noqa: BLE001
        return []
    if r.status_code != 200:
        return []
    content = r.content
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError:
            return []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]
    if root.tag.endswith("sitemapindex") and _depth < 3:
        out: list[str] = []
        for child in locs:
            out += sitemap_urls(fetcher, child, limit - len(out), _depth + 1)
            if len(out) >= limit:
                break
        return out[:limit]
    del ns
    return locs[:limit]


def _links(html: str, base: str) -> list[str]:
    from bs4 import BeautifulSoup

    out = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        out.append(_clean_url(urljoin(base, href)))
    return out


def ingest_url(project: Project, start: str, max_pages: int, result: IngestResult, dry_run: bool = False,
               delay: float = 0.25) -> None:
    fetcher = Fetcher(delay=delay)
    start = _clean_url(start)
    host = urlparse(start).netloc
    if start.endswith((".xml", ".xml.gz")):
        queue = deque(sitemap_urls(fetcher, start, max_pages))
        crawl = False
    else:
        root = "{0.scheme}://{0.netloc}".format(urlparse(start))
        listed = sitemap_urls(fetcher, root + "/sitemap.xml", max_pages)
        prefix = urlparse(start).path.rstrip("/")
        if prefix:
            listed = [u for u in listed if urlparse(u).path.startswith(prefix)]
        queue = deque(listed or [start])
        crawl = not listed
    seen: set[str] = set()
    fetched = 0
    while queue and fetched < max_pages:
        url = _clean_url(queue.popleft())
        if url in seen or urlparse(url).netloc != host:
            continue
        seen.add(url)
        if Path(urlparse(url).path).suffix.lower() in SKIP_EXT:
            continue
        if not fetcher.allowed(url):
            result.skipped.append(f"{url} (robots.txt)")
            continue
        try:
            r = fetcher.get(url)
        except Exception as exc:  # noqa: BLE001
            result.skipped.append(f"{url} ({type(exc).__name__})")
            continue
        if r.status_code in (404, 410):
            gone = capture_for(project, url)
            if gone is not None:
                mark_removed(project, gone, result, dry_run)
            else:
                result.skipped.append(f"{url} (HTTP {r.status_code})")
            continue
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            result.skipped.append(f"{url} (HTTP {r.status_code}, {r.headers.get('content-type', '?')})")
            continue
        final = _clean_url(str(r.url))
        if urlparse(final).netloc != host:  # never capture another site because a page redirected there
            result.skipped.append(f"{url} (redirects off the site, to {urlparse(final).netloc})")
            continue
        fetched += 1
        title, body = html_to_markdown(r.text, final)
        if len(body) < 80:
            result.skipped.append(f"{url} (almost no text)")
        else:
            write_page(project, slug_for(final), title, body, {"source_url": final}, result, dry_run)
        if crawl:
            for link in _links(r.text, final):
                if link not in seen and urlparse(link).netloc == host:
                    queue.append(link)
    # Pages captured before that this run didn't reach: are they gone (404/410), or just not visited?
    checked = 0
    for path, meta in _existing_pages(project):
        url = meta.get("source_url")
        if not url or urlparse(str(url)).netloc != host or _clean_url(str(url)) in seen or meta.get("removed_upstream"):
            continue
        if checked >= 50:
            break
        checked += 1
        try:
            status = fetcher.get(str(url)).status_code
        except Exception:  # noqa: BLE001 - unreachable isn't proof of removal
            continue
        if status in (404, 410):
            mark_removed(project, path, result, dry_run)


# ===================================================================== files


def _pdf_text(path: Path) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()


def ingest_folder(project: Project, folder: Path, result: IngestResult, dry_run: bool = False) -> None:
    files = [folder] if folder.is_file() else sorted(p for p in folder.rglob("*") if p.is_file())
    base = folder.parent if folder.is_file() else folder
    for path in files:
        ext = path.suffix.lower()
        rel_name = path.relative_to(base).with_suffix("").as_posix()
        origin = {"source_path": portable_path(project, path)}
        if ext in {".md", ".markdown", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            m = FRONTMATTER_RE.match(text)
            body = text[m.end():] if m else text
            title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("#")),
                         path.stem.replace("-", " ").replace("_", " ").title())
            write_page(project, slug_for(rel_name), title, body, origin, result, dry_run)
        elif ext in {".html", ".htm"}:
            title, body = html_to_markdown(path.read_text(encoding="utf-8", errors="replace"))
            write_page(project, slug_for(rel_name), title, body, origin, result, dry_run)
        elif ext == ".pdf":
            text = _pdf_text(path)
            if text is None:
                result.skipped.append(f"{path} (install pypdf to read PDFs: ./gt setup --pdf)")
            elif len(text) < 40:
                result.skipped.append(f"{path} (no extractable text; scanned PDF?)")
            else:
                write_page(project, slug_for(rel_name), path.stem.replace("-", " ").title(), text, origin,
                           result, dry_run)
        else:
            result.skipped.append(f"{path} (unsupported type {ext or 'none'})")
    if folder.is_dir():
        root = folder.resolve()
        for page, meta in _existing_pages(project):
            src = meta.get("source_path")
            if src and local_path(project, str(src)).is_relative_to(root) and not local_path(project, str(src)).exists():
                mark_removed(project, page, result, dry_run)


def recapture(project: Project, rels: list[str], *, dry_run: bool = False, delay: float = 0.25) -> IngestResult:
    """Re-capture exactly these source pages, from wherever each came from.

    The quick refresh uses this on the pages your facts cite, so a changed price
    or date shows up within hours without a full crawl. A page that's gone (404,
    410, or a deleted file) is flagged removed upstream, as a full ingest would.
    """
    result = IngestResult()
    fetcher: Fetcher | None = None
    for rel in rels:
        path = project.root / rel
        meta = page_meta(path)
        if not meta or meta.get("removed_upstream"):
            continue
        url, local = meta.get("source_url"), meta.get("source_path")
        if local:
            target = local_path(project, str(local))
            if target.exists():
                ingest_folder(project, target, result, dry_run)
            else:
                mark_removed(project, path, result, dry_run)
            continue
        if not url:
            continue
        fetcher = fetcher or Fetcher(delay=delay)
        if not fetcher.allowed(str(url)):
            result.skipped.append(f"{url} (robots.txt)")
            continue
        try:
            r = fetcher.get(str(url))
        except Exception as exc:  # noqa: BLE001 - unreachable isn't proof of removal
            result.skipped.append(f"{url} ({type(exc).__name__})")
            continue
        if r.status_code in (404, 410):
            mark_removed(project, path, result, dry_run)
        elif r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            result.skipped.append(f"{url} (HTTP {r.status_code}, {r.headers.get('content-type', '?')})")
        elif urlparse(_clean_url(str(r.url))).netloc != urlparse(str(url)).netloc:
            result.skipped.append(f"{url} (redirects off the site, to {urlparse(str(r.url)).netloc})")
        else:
            final = _clean_url(str(r.url))
            title, body = html_to_markdown(r.text, final)
            if len(body) < 80:
                result.skipped.append(f"{url} (almost no text)")
            else:
                write_page(project, slug_for(final), title, body, {"source_url": final}, result, dry_run)
    return result


# ===================================================================== entry point


def run(project: Project, targets: list[str], *, max_pages: int | None = None, dry_run: bool = False,
        save: bool = True, delay: float = 0.25) -> IngestResult:
    result = IngestResult()
    from_config = not targets
    targets = targets or project.ingest_sources
    if not targets:
        raise ValueError("nothing to ingest: pass a URL or folder, or list them under ingest.sources "
                         "in groundtruth.yaml")
    for target in targets:
        if target.startswith(("http://", "https://")):
            ingest_url(project, target, max_pages or project.ingest_max_pages, result, dry_run, delay)
        else:
            path = Path(target).expanduser()
            if not path.is_absolute():
                path = ((project.root if from_config else Path.cwd()) / path).resolve()
            if not path.exists():
                raise ValueError(f"{target}: no such file or folder")
            ingest_folder(project, path, result, dry_run)
    if save and not dry_run and not from_config:
        _remember(project, [_portable(project, t) for t in targets])
    return result


def _portable(project: Project, target: str) -> str:
    """URLs and absolute paths as-is; relative folders become relative to the project,
    so the saved config works no matter where you run ./gt from."""
    if target.startswith(("http://", "https://")):
        return target
    path = Path(target).expanduser()
    if path.is_absolute():
        return str(path)
    path = (Path.cwd() / path).resolve()
    try:
        import os
        return os.path.relpath(path, project.root)
    except ValueError:  # different drive on Windows
        return str(path)


def _remember(project: Project, targets: list[str]) -> None:
    """Add new targets to ingest.sources so `./gt ingest` and the weekly refresh re-run them."""
    known = set(project.ingest_sources)
    new = [t for t in targets if t not in known]
    if not new:
        return
    cfg_path = project.root / "groundtruth.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    config = yaml.safe_load(text) or {}
    ingest = dict(config.get("ingest") or {})
    ingest["sources"] = [*project.ingest_sources, *new]
    block = yaml.safe_dump({"ingest": ingest}, sort_keys=False, allow_unicode=True)
    pattern = re.compile(r"(?m)^ingest:.*\n(?:(?:[ \t]+.*|[ \t]*)\n)*")
    if pattern.search(text):
        text = pattern.sub(lambda _: block, text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block
    cfg_path.write_text(text, encoding="utf-8")
    project.config["ingest"] = ingest
