"""Ship data without redeploying: build a bundle, let running servers pick it up.

    ./gt bundle                  # build/groundtruth-data.tar.gz (+ .sha256)

A bundle is the built facts database, the wiki, and the search index, plus a
manifest with a checksum for every file. ``.github/workflows/publish-data.yml``
builds one after CI passes on main and attaches it to a ``data-latest`` release.

A server started with ``GT_DATA_URL`` set polls that URL (every
``GT_DATA_POLL_SECONDS``, default 600). For each new bundle it downloads,
verifies every checksum, sanity-checks it against the live data, and switches
over atomically. Anything wrong (bad checksum, a collection that suddenly lost
half its rows) keeps the current data and logs why. Set ``GT_DATA_TOKEN`` for a
private URL (sent as a Bearer token). Code changes still need a deploy.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import threading
import time
from pathlib import Path

from groundtruth import facts, search
from groundtruth.project import Project

BUNDLE_NAME = "groundtruth-data.tar.gz"
FORMAT = 2
KEEP = 2
SERVED_CONFIG = ("name", "about", "search")  # what a running server reads from groundtruth.yaml
_lock = threading.Lock()
_state: dict[str, str | None] = {"applied_sha": None, "applied_created": None, "last_error": None,
                                 "last_checked": None, "last_applied": None}


def status() -> dict[str, str | None]:
    """For /healthz: what the data updater last did."""
    return dict(_state)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(project: Project, *, verified: bool = False) -> Path:
    """``verified``: ./gt check --production and ./gt eval passed on this content. Servers then accept a
    collection that shrank (a fact retired on purpose), which they otherwise refuse as a likely broken build."""
    facts.build(project)
    try:
        search.build_search_index(project)
    except Exception:  # noqa: BLE001 - the index is optional
        pass
    from groundtruth import checks
    members: dict[str, bytes] = {"facts.db": project.db_path.read_bytes()}
    if project.index_path.exists():
        members["search-index.db"] = project.index_path.read_bytes()
    for page in sorted(project.wiki_dir.rglob("*.md")):
        rel = page.relative_to(project.wiki_dir)
        meta, _ = checks.split_frontmatter(page.read_text(encoding="utf-8"))
        staff = "internal" in rel.parts or str((meta or {}).get("page_type", "")).lower() == "internal" \
            or str((meta or {}).get("audience", "")).lower() == "internal"
        if staff or (page.name not in checks.SKIP and checks.is_draft(page)):
            continue  # a release asset is downloadable: staff-only and unfinished pages stay home
        members["wiki/" + rel.as_posix()] = page.read_bytes()
    if project.prompt_path.exists():
        members["prompts/system_prompt.md"] = project.prompt_path.read_bytes()
    members["config.json"] = json.dumps({k: project.config[k] for k in SERVED_CONFIG if k in project.config},
                                        default=str).encode()
    store = facts.store(project)
    store.refresh(force=True)
    manifest = {
        "format": FORMAT,
        "verified": verified,
        "name": project.name,
        "data_version": store.meta.get("fingerprint"),
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "counts": json.loads(store.meta.get("counts", "{}")),
        "wiki_pages": sum(1 for k in members if k.startswith("wiki/")),
        "files": {name: _sha(data) for name, data in sorted(members.items())},
    }
    out = project.build_dir / BUNDLE_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("manifest.json", json.dumps(manifest, indent=2).encode()), *sorted(members.items())]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    out.write_bytes(buf.getvalue())
    out.with_suffix("").with_suffix(".sha256").write_text(_sha(buf.getvalue()) + f"  {BUNDLE_NAME}\n")
    return out


# ===================================================================== applying


def _counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM _meta WHERE key='counts'").fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def _sane(new_counts: dict[str, int], new_pages: int, project: Project) -> str | None:
    """Reason to reject the bundle, or None if it looks fine."""
    if os.environ.get("GT_DATA_ALLOW_SHRINK") == "1":
        return None
    try:
        live = _counts(project.db_path)
    except Exception:  # noqa: BLE001 - no live data to compare against
        return None
    for name, n in live.items():
        if n >= 4 and new_counts.get(name, 0) < n / 2:
            return f"{name} would drop from {n} to {new_counts.get(name, 0)} rows"
    live_pages = len(search.load_pages(project))
    if live_pages >= 4 and new_pages < live_pages / 2:
        return f"wiki would drop from {live_pages} to {new_pages} pages"
    return None


def apply_bytes(project: Project, data: bytes) -> str:
    """Verify and switch to a bundle. Returns a short status message."""
    sha = _sha(data)
    if sha == _state["applied_sha"]:
        return "unchanged"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        if "manifest.json" not in names:
            raise ValueError("bundle has no manifest.json")
        for n in names:
            if n.startswith("/") or ".." in Path(n).parts:
                raise ValueError(f"unsafe path in bundle: {n}")
        manifest = json.loads(tar.extractfile("manifest.json").read())
        files = {n: tar.extractfile(n).read() for n in names if n != "manifest.json"}
    if manifest.get("format") not in (None, 1, FORMAT):
        raise ValueError(f"bundle format {manifest.get('format')} is newer than this server: upgrade it first")
    listed = manifest.get("files", {})
    if "facts.db" not in listed:
        raise ValueError("bundle has no facts.db")
    extra = sorted(set(files) - set(listed))
    if extra:
        raise ValueError(f"bundle has files its manifest doesn't list: {', '.join(extra[:3])}")
    for name, digest in listed.items():
        if name not in files or _sha(files[name]) != digest:
            raise ValueError(f"checksum mismatch for {name}")
    created = str(manifest.get("created") or "")
    if _state["applied_created"] and created and created < str(_state["applied_created"]):
        raise ValueError(f"bundle from {created} is older than the one serving ({_state['applied_created']})")
    root = project.build_dir / "bundles"
    target = root / sha[:12]
    tmp = root / f".{sha[:12]}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    for name, blob in files.items():
        dest = tmp / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
    try:  # trust what's in it, not what the manifest claims, and make sure it opens before switching
        actual = _counts(tmp / "facts.db")
        conn = sqlite3.connect(f"file:{tmp / 'facts.db'}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("facts.db is corrupt")
            conn.execute("SELECT spec FROM _collections").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, KeyError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"facts.db in the bundle can't be read: {exc}") from exc
    if actual != manifest.get("counts", actual):
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("facts.db row counts don't match the manifest")
    pages = sum(1 for n in files if n.startswith("wiki/"))
    reason = None if manifest.get("verified") is True else _sane(actual, pages, project)
    if reason:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"rejected: {reason} (set GT_DATA_ALLOW_SHRINK=1 if that's intended)")
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    served = {}
    if "config.json" in files:
        served = {k: v for k, v in json.loads(files["config.json"]).items() if k in SERVED_CONFIG}
    with _lock:
        project.config.update(served)
        project.data_root = target
        _state["applied_sha"] = sha
        _state["applied_created"] = created or None
    for old in sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime)[:-KEEP]:
        if old != target:
            shutil.rmtree(old, ignore_errors=True)
    return f"applied data {manifest.get('data_version')} ({sha[:12]})"


def fetch_and_apply(project: Project) -> str:
    import httpx

    url = os.environ["GT_DATA_URL"].strip()
    headers = {"Accept": "application/octet-stream"}
    token = os.environ.get("GT_DATA_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(url, headers=headers, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return apply_bytes(project, r.content)


def start_polling(project: Project) -> threading.Thread:
    interval = max(30, int(os.environ.get("GT_DATA_POLL_SECONDS", "600")))

    def loop() -> None:
        while True:
            _state["last_checked"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            try:
                msg = fetch_and_apply(project)
                _state["last_error"] = None
                if msg != "unchanged":
                    _state["last_applied"] = _state["last_checked"]
                    print(f"groundtruth: {msg}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - keep serving the live data, and say so in /healthz
                _state["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                print(f"groundtruth: data bundle not applied: {exc}", file=sys.stderr)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="groundtruth-data", daemon=True)
    thread.start()
    return thread
