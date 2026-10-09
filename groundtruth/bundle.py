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
KEEP = 2
_lock = threading.Lock()
_state: dict[str, str | None] = {"applied_sha": None}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(project: Project) -> Path:
    facts.build(project)
    try:
        search.build_search_index(project)
    except Exception:  # noqa: BLE001 - the index is optional
        pass
    members: dict[str, bytes] = {"facts.db": project.db_path.read_bytes()}
    if project.index_path.exists():
        members["search-index.db"] = project.index_path.read_bytes()
    for page in sorted(project.wiki_dir.rglob("*.md")):
        members["wiki/" + page.relative_to(project.wiki_dir).as_posix()] = page.read_bytes()
    store = facts.store(project)
    store.refresh(force=True)
    manifest = {
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
    for name, digest in manifest.get("files", {}).items():
        if name not in files or _sha(files[name]) != digest:
            raise ValueError(f"checksum mismatch for {name}")
    reason = _sane(manifest.get("counts", {}), int(manifest.get("wiki_pages", 0)), project)
    if reason:
        raise ValueError(f"rejected: {reason} (set GT_DATA_ALLOW_SHRINK=1 if that's intended)")
    root = project.build_dir / "bundles"
    target = root / sha[:12]
    tmp = root / f".{sha[:12]}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    for name, blob in files.items():
        dest = tmp / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    with _lock:
        project.data_root = target
        _state["applied_sha"] = sha
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
            try:
                msg = fetch_and_apply(project)
                if msg != "unchanged":
                    print(f"groundtruth: {msg}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - keep serving the live data
                print(f"groundtruth: data bundle not applied: {exc}", file=sys.stderr)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="groundtruth-data", daemon=True)
    thread.start()
    return thread
