"""Hard facts: the half of the system that is not allowed to be wrong.

You describe your facts in YAML, one file per collection, in ``facts/``. Each
file says what the rows are, where they came from, and which tools the model
gets. No Python, no SQL:

    # facts/fees.yaml
    description: Fees and fines in US dollars.
    key: fee_type
    source: {path: sources/pages/fees.md, url: https://example.org/fees}
    tools:
      - name: get_fee
        kind: lookup          # get_fee(fee_type="printing") -> best matches
        arg: fee_type
        description: Every dollar amount the assistant says comes from here.
    rows:
      - {fee_type: printing-bw, amount_usd: 0.10, unit: per page}

Two tool kinds cover nearly everything:
  * ``lookup`` - one text argument, forgiving match ("printing", "lost card").
  * ``list``   - optional filters (``branch``, ``day``), plus "upcoming only"
                 when the collection has a ``date_field``.

``./gt build`` projects the YAML into ``build/facts.db`` (SQLite). It is
deterministic and reruns itself whenever the YAML changes, so the running
server always answers from what's on disk.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import keyword
import operator
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from groundtruth.project import Project

RESERVED_TOOLS = {"search_wiki", "read_wiki_page"}
RESERVED_PARAMS = {"limit", "upcoming_only"}
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RULE_RE = re.compile(r"\s*([A-Za-z_]\w*)\s*(<=|>=|==|!=|<|>)\s*([A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*")
RULE_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq,
            "!=": operator.ne}
INTERNAL_FIELDS = {"aliases", "source", "source_url", "source_path"}
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
SCHEMA_VERSION = "2"

STOPWORDS = {
    "a", "an", "and", "are", "at", "be", "by", "can", "do", "does", "for", "how",
    "i", "in", "is", "it", "me", "much", "my", "of", "on", "or", "the", "to",
    "what", "when", "where", "which", "who", "with", "you", "your", "cost",
}


class FactsError(Exception):
    """Your facts YAML has problems. ``problems`` lists every one of them."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


# =============================================================== the model


@dataclass
class Filter:
    field: str
    param: str
    ref: str | None = None
    wildcards: list[str] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    kind: str  # "lookup" | "list"
    collection: str
    description: str
    arg: str = "query"
    filters: list[Filter] = field(default_factory=list)
    upcoming: bool = False


@dataclass
class Collection:
    name: str
    description: str
    key: str
    rows: list[dict[str, Any]]
    tools: list[ToolSpec]
    source_path: str | None = None
    source_url: str | None = None
    date_field: str | None = None
    match: list[str] = field(default_factory=list)
    file: str = ""


# =============================================================== loading + validation


def fact_files(project: Project) -> list[Path]:
    if not project.facts_dir.exists():
        return []
    return sorted(
        p for p in project.facts_dir.iterdir()
        if p.suffix in {".yaml", ".yml"} and not p.name.startswith(("_", "."))
    )


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _row_source(row: dict, coll_source: dict) -> tuple[str | None, str | None]:
    nested = row.get("source") if isinstance(row.get("source"), dict) else {}
    path = row.get("source_path") or nested.get("path") or coll_source.get("path")
    url = row.get("source_url") or nested.get("url") or coll_source.get("url")
    return (str(path) if path else None, str(url) if url else None)


def _parse_collection(path: Path, data: Any, problems: list[str]) -> Collection | None:
    where = path.name
    if not isinstance(data, dict):
        problems.append(f"{where}: must be a YAML mapping with `rows:` and `tools:`")
        return None
    name = str(data.get("name") or re.sub(r"[^A-Za-z0-9_]", "_", path.stem))  # self-pay-prices.yaml works
    if not FIELD_RE.match(name):
        problems.append(f"{where}: collection name {name!r} must be letters, numbers, underscores")
        return None
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        problems.append(f"{where}: needs a non-empty `rows:` list")
        rows = []
    key = str(data.get("key") or "id")
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    date_field = data.get("date_field")

    clean_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            problems.append(f"{where}: row {i} must be a mapping (field: value)")
            continue
        for fname in row:
            if not FIELD_RE.match(str(fname)):
                hint = (" (a comma inside {braces} starts a new field: put that value in quotes)"
                        if row.get(fname) is None else "")
                problems.append(f"{where}: row {i} field {fname!r} must be letters, numbers, underscores{hint}")
        if row.get(key) in (None, ""):
            problems.append(f"{where}: row {i} is missing its key field `{key}`")
        else:
            k = str(row[key])
            if k.lower() in seen_keys:
                problems.append(f"{where}: duplicate {key} {k!r} (row {i})")
            seen_keys.add(k.lower())
        spath, surl = _row_source(row, source)
        if not (spath or surl):
            problems.append(
                f"{where}: row {i} has no source. Add `source: {{path: ..., url: ...}}` "
                f"at the top of the file, or `source_url:` on the row"
            )
        if date_field and row.get(date_field) not in (None, ""):
            value = row[date_field]
            text = value.isoformat() if isinstance(value, dt.date) else str(value)
            if not ISO_DATE_RE.match(text):
                problems.append(f"{where}: row {i} {date_field}={value!r} must be YYYY-MM-DD")
            else:
                try:
                    dt.date.fromisoformat(text[:10])
                except ValueError:
                    problems.append(f"{where}: row {i} {date_field}={value!r} isn't a real date")
        clean = {}
        for k2, v in row.items():
            if isinstance(v, dt.date):
                v = v.isoformat()
            clean[str(k2)] = v
        if "aliases" in clean:
            clean["aliases"] = [str(a) for a in _as_list(clean["aliases"])]
        clean_rows.append(clean)

    all_fields = {f for r in clean_rows for f in r}
    for rule in _as_list(data.get("rules")):
        problems += _rule_problems(where, str(rule), clean_rows, key, all_fields)
    tools: list[ToolSpec] = []
    raw_tools = data.get("tools")
    if raw_tools is None:
        raw_tools = [{"name": f"get_{name}", "kind": "lookup"}]
    for t in _as_list(raw_tools):
        if not isinstance(t, dict) or not t.get("name"):
            problems.append(f"{where}: every tool needs a `name:`")
            continue
        tname = str(t["name"])
        kind = str(t.get("kind") or "lookup")
        if not TOOL_NAME_RE.match(tname):
            problems.append(f"{where}: tool name {tname!r} must be lowercase snake_case")
        if kind not in {"lookup", "list"}:
            problems.append(f"{where}: tool {tname} kind must be `lookup` or `list`, not {kind!r}")
        filters: list[Filter] = []
        for f in _as_list(t.get("filters")):
            if isinstance(f, str):
                f = {"field": f}
            if not isinstance(f, dict) or not f.get("field"):
                problems.append(f"{where}: tool {tname} filter needs a `field:`")
                continue
            fname = str(f["field"])
            if fname not in all_fields:
                problems.append(f"{where}: tool {tname} filters on `{fname}`, but no row has that field")
            filters.append(Filter(
                field=fname,
                param=str(f.get("param") or fname),
                ref=str(f["ref"]) if f.get("ref") else None,
                wildcards=[str(w).lower() for w in _as_list(f.get("wildcards"))],
            ))
        arg = str(t.get("arg") or "query")
        params = [arg] if kind == "lookup" else [f.param for f in filters]
        for dup in sorted({x for x in params if params.count(x) > 1}):
            problems.append(f"{where}: tool {tname} has two parameters named {dup!r}; give one a different "
                            f"`param:`")
        for pname in params:
            if keyword.iskeyword(pname):
                problems.append(f"{where}: tool {tname} parameter {pname!r} is a reserved word in the server's "
                                f"language. Rename it with `param:`, e.g. `param: {pname}_name`")
            elif pname in RESERVED_PARAMS:
                problems.append(f"{where}: tool {tname} can't use {pname!r} as a parameter name "
                                f"(reserved). Rename it, e.g. `arg: name`")
            elif not FIELD_RE.match(pname):
                problems.append(f"{where}: tool {tname} parameter {pname!r} must be letters, numbers, underscores")
        upcoming = bool(t.get("upcoming"))
        if upcoming and not date_field:
            problems.append(f"{where}: tool {tname} sets `upcoming: true` but the file has no `date_field:`")
        description = " ".join(str(t.get("description") or data.get("description") or "").split())
        if not description:
            problems.append(f"{where}: tool {tname} needs a `description:` (the model reads it to pick tools)")
        tools.append(ToolSpec(
            name=tname, kind=kind, collection=name, description=description,
            arg=arg, filters=filters, upcoming=upcoming,
        ))

    match = [str(m) for m in _as_list(data.get("match"))]
    for m in match:
        if m not in all_fields:
            problems.append(f"{where}: `match:` lists `{m}`, but no row has that field")
    return Collection(
        name=name,
        description=" ".join(str(data.get("description") or "").split()),
        key=key, rows=clean_rows, tools=tools,
        source_path=str(source["path"]) if source.get("path") else None,
        source_url=str(source["url"]) if source.get("url") else None,
        date_field=str(date_field) if date_field else None,
        match=match, file=str(path),
    )


def _comparable(value: Any) -> tuple[str, Any] | None:
    """A value as something a rule can compare: a time of day (HH:MM), a date (YYYY-MM-DD), or a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return ("number", Decimal(str(value)))
    text = str(value).strip()
    if m := re.fullmatch(r"([01]?\d|2[0-4]):([0-5]\d)", text):
        return ("time", int(m[1]) * 60 + int(m[2]))
    if ISO_DATE_RE.match(text):
        try:
            return ("date", dt.date.fromisoformat(text))
        except ValueError:
            return None
    try:
        n = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return ("number", n) if n.is_finite() else None  # "NaN" and "Infinity" parse, but compare as nothing


def _rule_problems(where: str, rule: str, rows: list[dict], key: str, all_fields: set[str]) -> list[str]:
    """A rule says how a row's fields relate (`opens < closes`). Grounding can't see that: 18:00 can be the
    source's real closing time and still be wrong as the opening time. A row lacking either field is skipped."""
    m = RULE_RE.fullmatch(rule)
    if not m:
        return [f"{where}: rule {rule!r} must compare two fields, or a field and a number, e.g. `opens < closes` "
                f"or `price_usd >= 0`"]
    left, op, right = m.groups()
    literal = right[0].isdigit() or right[0] == "-"
    for f in [left] + ([] if literal else [right]):
        if f not in all_fields:
            return [f"{where}: rule `{rule.strip()}` names `{f}`, but no row has that field"]
    out = []
    for row in rows:
        lv, rv = row.get(left), right if literal else row.get(right)
        if lv in (None, "") or rv in (None, ""):
            continue  # a closed day has no opening time to compare
        a, b = _comparable(lv), _comparable(rv)
        shown = f"{left} is {lv!r}" + ("" if literal else f", {right} is {rv!r}")
        if a is None or b is None or a[0] != b[0]:
            out.append(f"{where}: row {row.get(key)!s} can't be checked against the rule `{left} {op} {right}` "
                       f"({shown}): write both as HH:MM times, YYYY-MM-DD dates, or numbers")
        elif not RULE_OPS[op](a[1], b[1]):
            out.append(f"{where}: row {row.get(key)!s} breaks the rule `{left} {op} {right}` ({shown})")
    return out


def friendly_yaml_error(name: str, exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    return (f"{name}: YAML can't be read{where} ({problem}). Usual causes: indentation that doesn't line up, "
            f"a missing ':' after a field name, a value containing ': ' that needs quotes, or a list item "
            f"missing its '- '")


def load(project: Project) -> list[Collection]:
    """Read and validate every facts file. Raises FactsError listing all problems."""
    problems: list[str] = []
    collections: list[Collection] = []
    for path in fact_files(project):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(friendly_yaml_error(path.name, exc))
            continue
        coll = _parse_collection(path, data, problems)
        if coll is not None:
            collections.append(coll)

    names = [c.name for c in collections]
    for n in {n for n in names if names.count(n) > 1}:
        problems.append(f"two facts files define the collection {n!r}")
    tool_names = [t.name for c in collections for t in c.tools]
    for n in {n for n in tool_names if tool_names.count(n) > 1}:
        problems.append(f"two collections define the tool {n!r}")
    for n in set(tool_names) & RESERVED_TOOLS:
        problems.append(f"tool name {n!r} is reserved for the wiki tools")
    by_name = {c.name: c for c in collections}
    for c in collections:
        for t in c.tools:
            for f in t.filters:
                if f.ref and f.ref not in by_name:
                    problems.append(f"{Path(c.file).name}: tool {t.name} filter `{f.field}` refers to "
                                    f"unknown collection {f.ref!r}")
    if problems:
        raise FactsError(problems)
    return collections


def warnings(project: Project, collections: list[Collection]) -> list[str]:
    """Non-fatal issues worth fixing (missing source files, ref values that don't resolve)."""
    out: list[str] = []
    by_name = {c.name: c for c in collections}
    for c in collections:
        for row in c.rows:
            spath, _ = _row_source(row, {"path": c.source_path, "url": c.source_url})
            if spath and not (project.root / spath).exists():
                import difflib
                known = [project.rel(p) for p in project.pages_dir.rglob("*.md")] if project.pages_dir.exists() else []
                close = difflib.get_close_matches(str(spath), known, n=1, cutoff=0.6)
                out.append(f"{Path(c.file).name}: source file {spath} does not exist"
                           + (f" (did you mean {close[0]}?)" if close else ""))
                break
        for t in c.tools:
            for f in t.filters:
                if not f.ref:
                    continue
                keys = {str(r.get(by_name[f.ref].key)).lower() for r in by_name[f.ref].rows}
                bad = {str(r[f.field]) for r in c.rows if r.get(f.field) not in (None, "")
                       and str(r[f.field]).lower() not in keys}
                if bad:
                    out.append(f"{Path(c.file).name}: `{f.field}` values {sorted(bad)} are not keys in {f.ref}")
    return out


# =============================================================== build (YAML -> SQLite)


def fingerprint(project: Project) -> str:
    """Hash of every facts file's content. Changes exactly when the facts change."""
    h = hashlib.sha256(SCHEMA_VERSION.encode())
    for path in fact_files(project):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _sql_type(values: list[Any]) -> str:
    kinds = {type(v) for v in values if v is not None}
    if kinds and kinds <= {bool, int}:
        return "INTEGER"
    if kinds and kinds <= {bool, int, float}:
        return "REAL"
    return "TEXT"


def _sql_value(v: Any) -> Any:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True)
    return v


def build(project: Project) -> dict[str, int]:
    """Project facts/*.yaml into build/facts.db. Returns row counts per collection."""
    collections = load(project)
    db_path = project.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE _collections (name TEXT PRIMARY KEY, spec TEXT NOT NULL)"
        )
        counts: dict[str, int] = {}
        for c in collections:
            fields = sorted({f for r in c.rows for f in r if f not in {"source"}})
            cols = ", ".join(f'"{f}" {_sql_type([r.get(f) for r in c.rows])}' for f in fields)
            table = f"facts_{c.name}"
            conn.execute(
                f'CREATE TABLE "{table}" (_pos INTEGER PRIMARY KEY, _json TEXT NOT NULL,'
                f' _source_path TEXT, _source_url TEXT{", " + cols if cols else ""})'
            )
            for pos, row in enumerate(c.rows):
                spath, surl = _row_source(row, {"path": c.source_path, "url": c.source_url})
                values = [pos, json.dumps(row, sort_keys=True, default=str), spath, surl]
                values += [_sql_value(row.get(f)) for f in fields]
                marks = ", ".join("?" for _ in values)
                conn.execute(f'INSERT INTO "{table}" VALUES ({marks})', values)
            spec = {
                "name": c.name, "description": c.description, "key": c.key,
                "date_field": c.date_field, "match": c.match,
                "source_path": c.source_path, "source_url": c.source_url,
                "tools": [
                    {"name": t.name, "kind": t.kind, "description": t.description, "arg": t.arg,
                     "upcoming": t.upcoming,
                     "filters": [vars(f) for f in t.filters]}
                    for t in c.tools
                ],
            }
            conn.execute("INSERT INTO _collections VALUES (?, ?)",
                         (c.name, json.dumps(spec, sort_keys=True)))
            counts[c.name] = len(c.rows)
        conn.execute("INSERT INTO _meta VALUES ('fingerprint', ?)", (fingerprint(project),))
        conn.execute("INSERT INTO _meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        conn.execute("INSERT INTO _meta VALUES ('counts', ?)", (json.dumps(counts, sort_keys=True),))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, db_path)
    return counts


def _db_fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM _meta WHERE key='fingerprint'").fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def is_stale(project: Project) -> bool:
    if project.data_root is not None:
        return False  # a published bundle is already built
    return _db_fingerprint(project.db_path) != fingerprint(project)


def ensure_built(project: Project) -> None:
    if is_stale(project):
        build(project)


# =============================================================== matching


def norm(text: Any) -> str:
    s = str(text if text is not None else "").lower()
    s = re.sub(r"[-_/.,:;()'\"!?&]+", " ", s)
    return " ".join(s.split())


def _tokens(text: Any) -> list[str]:
    return [t for t in norm(text).split() if t not in STOPWORDS and len(t) > 1]


def _token_hit(q: str, words: list[str]) -> bool:
    for w in words:
        if q == w:
            return True
        if len(q) >= 3 and (w.startswith(q) or q.startswith(w) and len(w) >= 3):
            return True
        if q.endswith("s") and q[:-1] == w or w.endswith("s") and w[:-1] == q:
            return True
    return False


def score_row(query: str, row: dict, coll: dict) -> float:
    """How well a free-text query matches a row. 0 means no match."""
    q = norm(query)
    if not q:
        return 0.0
    key = norm(row.get(coll["key"]))
    aliases = [norm(a) for a in row.get("aliases") or []]
    match_fields = coll.get("match") or [
        f for f, v in row.items()
        if isinstance(v, str) and f not in {"source_url", "source_path"}
    ]
    texts = [norm(row.get(f)) for f in match_fields if row.get(f) not in (None, "")]
    if q == key or q in aliases:
        return 100.0
    if q in texts:
        return 90.0
    best = 0.0
    for t in [key, *aliases, *texts]:
        if not t:
            continue
        if q in t:
            best = max(best, 60.0 + 10.0 * len(q) / max(len(t), 1))
        elif len(t) >= 3 and f" {t} " in f" {q} ":
            best = max(best, 45.0)
    q_tokens = _tokens(query)
    if q_tokens:
        words = [w for t in [key, *aliases, *texts] for w in t.split()]
        hits = sum(1 for qt in q_tokens if _token_hit(qt, words))
        if hits:
            best = max(best, 10.0 * hits + 20.0 * hits / len(q_tokens))
    return best


def value_matches(wanted: str, actual: Any, wildcards: list[str]) -> bool:
    a = norm(actual)
    w = norm(wanted)
    if not w:
        return True
    if a in wildcards:
        return True
    if not a:
        return False
    return a == w or (len(w) >= 3 and (a.startswith(w) or w in a.split())) or w == a.rstrip("s")


# =============================================================== the store (runtime reads)


class FactStore:
    """Reads build/facts.db into memory and answers tool calls.

    Reloads by itself when the database changes, and rebuilds the database when
    the YAML changes, so editing facts never requires a restart.
    """

    def __init__(self, project: Project):
        self.project = project
        self._lock = threading.Lock()
        self._loaded_mtime: float | None = None
        self._checked_at = 0.0
        self.collections: dict[str, dict] = {}
        self.rows: dict[str, list[dict]] = {}
        self.tools: dict[str, dict] = {}
        self.meta: dict[str, str] = {}
        self.auto_reload = True  # the server turns this off and reloads only validated data

    def refresh(self, force: bool = False) -> None:
        import time
        now = time.monotonic()
        if not force and self._loaded_mtime is not None and not self.auto_reload:
            return
        if not force and now - self._checked_at < 1.0 and self._loaded_mtime is not None:
            return
        with self._lock:
            self._checked_at = now
            ensure_built(self.project)
            path = self.project.db_path
            mtime = path.stat().st_mtime if path.exists() else None
            if mtime is None:
                raise FileNotFoundError(f"no facts database at {path}. Run: ./gt build")
            if not force and mtime == self._loaded_mtime:
                return
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                specs = [json.loads(s) for (s,) in conn.execute(
                    "SELECT spec FROM _collections ORDER BY name")]
                meta = dict(conn.execute("SELECT key, value FROM _meta").fetchall())
                rows: dict[str, list[dict]] = {}
                for spec in specs:
                    out = []
                    for js, spath, surl in conn.execute(
                        f'SELECT _json, _source_path, _source_url FROM "facts_{spec["name"]}" ORDER BY _pos'
                    ):
                        r = json.loads(js)
                        r["source_path"] = spath
                        r["source_url"] = surl
                        out.append(r)
                    rows[spec["name"]] = out
            finally:
                conn.close()
            self.collections = {s["name"]: s for s in specs}
            self.rows = rows
            self.tools = {t["name"]: {**t, "collection": s["name"]} for s in specs for t in s["tools"]}
            self.meta = meta
            self._loaded_mtime = mtime

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _public(row: dict) -> dict:
        out = {k: v for k, v in row.items()
               if k not in {"aliases", "source", "source_url", "source_path"} and not k.startswith("_")}
        out["source"] = row.get("source_url") or row.get("source_path")
        if row.get("source_url") and row.get("source_path"):
            out["source_file"] = row["source_path"]
        return out

    def _lookup(self, coll_name: str, query: str) -> list[tuple[float, dict]]:
        coll = self.collections[coll_name]
        scored = [(score_row(query, r, coll), i, r) for i, r in enumerate(self.rows[coll_name])]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: (-s[0], s[1]))
        if scored:
            top = scored[0][0]
            floor = top - 10 if top >= 90 else top * 0.6
            scored = [s for s in scored if s[0] >= floor]
        return [(s, r) for s, _, r in scored]

    def _resolve_ref(self, ref: str, value: str) -> str | None:
        hits = self._lookup(ref, value)
        if not hits:
            return None
        return str(hits[0][1].get(self.collections[ref]["key"]))

    # ------------------------------------------------------------ the tool call
    def call(self, tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.refresh()
        if tool_name not in self.tools:
            raise KeyError(f"unknown tool {tool_name!r}")
        spec = self.tools[tool_name]
        coll_name = spec["collection"]
        coll = self.collections[coll_name]
        args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        limit = max(1, min(int(args.pop("limit", DEFAULT_LIMIT) or DEFAULT_LIMIT), MAX_LIMIT))
        all_rows = self.rows[coll_name]
        hint = None

        if spec["kind"] == "lookup":
            query = str(args.get(spec["arg"], "") or "")
            if query.strip():
                results = [r for _, r in self._lookup(coll_name, query)]
                if not results:
                    keys = [str(r.get(coll["key"])) for r in all_rows][:40]
                    hint = (f"Nothing in {coll_name} matched {query!r}. "
                            f"Known {coll['key']} values: {', '.join(keys)}")
            else:
                results = list(all_rows)
            payload = {"collection": coll_name, "query": query or None}
        else:
            results = list(all_rows)
            applied: dict[str, str] = {}
            payload_ambiguous: dict[str, list[str]] = {}
            for f in spec["filters"]:
                wanted = args.get(f["param"])
                if wanted in (None, ""):
                    continue
                wanted = str(wanted)
                applied[f["param"]] = wanted
                target = wanted
                if f.get("ref"):
                    target = self._resolve_ref(f["ref"], wanted) or wanted
                matched = [r for r in results if value_matches(target, r.get(f["field"]), f.get("wildcards") or [])]
                exact = [r for r in matched if norm(r.get(f["field"])) == norm(target)
                         or norm(r.get(f["field"])) in (f.get("wildcards") or [])]
                if exact:  # "central" means central, not also "centerville"
                    matched = exact
                else:
                    values = sorted({str(r.get(f["field"])) for r in matched})
                    if len(values) > 1:
                        payload_ambiguous[f["param"]] = values
                results = matched
            date_field = coll.get("date_field")
            if spec.get("upcoming") and date_field:
                if args.get("upcoming_only", True) not in (False, "false", "False", 0):
                    today = dt.date.today().isoformat()
                    results = [r for r in results if not r.get(date_field) or str(r[date_field]) >= today]
                # soonest dated rows first; undated (recurring) rows after, so they can't crowd them out
                results.sort(key=lambda r: (not r.get(date_field), str(r.get(date_field) or "")))
            if not results and applied:
                hint = f"No {coll_name} rows matched {applied}."
            payload = {"collection": coll_name, "filters": applied or None}
            if payload_ambiguous:
                payload["ambiguous"] = payload_ambiguous
                hint = ("Several values matched: " + "; ".join(f"{k} could be {', '.join(v)}"
                        for k, v in payload_ambiguous.items()) + ". Ask which one, or pass the exact value.")

        payload["count"] = len(results)
        payload["results"] = [self._public(r) for r in results[:limit]]
        if len(results) > limit:
            payload["truncated"] = True
        if hint:
            payload["hint"] = hint
        return payload

    def suggest(self, question: str, top: int = 3) -> list[dict[str, Any]]:
        """Best-guess tool calls for a plain-English question, without a model.

        Powers ``./gt ask`` when there's no API key: it pulls filter values out of
        the question ("central" -> branch, "saturday" -> day) and keeps only
        strong lookup matches, so you see roughly what the model would retrieve.
        """
        self.refresh()
        q = f" {norm(question)} "
        found: list[dict[str, Any]] = []
        for name, spec in self.tools.items():
            coll = self.collections[spec["collection"]]
            if spec["kind"] == "lookup":
                rows = self._lookup(spec["collection"], question)
                best = rows[0][0] if rows else 0.0
                if best >= 28:
                    found.append({"tool": name, "args": {spec["arg"]: question}, "score": best})
                continue
            args: dict[str, str] = {}
            for f in spec["filters"]:
                choices: dict[str, str] = {}
                for r in self.rows[spec["collection"]]:
                    v = r.get(f["field"])
                    if v not in (None, "") and norm(v) not in (f.get("wildcards") or []):
                        choices[norm(v)] = str(v)
                if f.get("ref"):
                    ref = self.collections[f["ref"]]
                    for r in self.rows[f["ref"]]:
                        key = str(r.get(ref["key"]))
                        for label in [key, r.get("name"), *(r.get("aliases") or [])]:
                            if label:
                                choices[norm(label)] = key
                hits = [lab for lab in choices if len(lab) >= 3 and f" {lab} " in q]
                if hits:
                    args[f["param"]] = choices[max(hits, key=len)]
            if args:
                found.append({"tool": name, "args": args, "score": 50.0 + 10 * len(args)})
            elif any(w in q for w in (f" {norm(coll['name'])} ", f" {norm(coll['name']).rstrip('s')} ")):
                found.append({"tool": name, "args": {}, "score": 30.0})
        found.sort(key=lambda c: -c["score"])
        if found:
            best = found[0]["score"]
            found = [c for c in found if c["score"] >= 0.75 * best]
        return found[:top]

    def tool_param_schema(self, tool_name: str) -> dict[str, Any]:
        """JSON-schema style description of a tool's parameters (for docs and checks)."""
        self.refresh()
        spec = self.tools[tool_name]
        props: dict[str, Any] = {}
        if spec["kind"] == "lookup":
            props[spec["arg"]] = {"type": "string", "description": "What to look up. Leave empty to list everything."}
        else:
            for f in spec["filters"]:
                props[f["param"]] = {"type": "string", "description": f"Only rows where {f['field']} matches."}
            if spec.get("upcoming"):
                props["upcoming_only"] = {"type": "boolean", "description": "Skip dates in the past (default true)."}
        props["limit"] = {"type": "integer", "description": f"Max rows (default {DEFAULT_LIMIT})."}
        return {"type": "object", "properties": props}


_stores: dict[str, FactStore] = {}


def store(project: Project) -> FactStore:
    key = f"{project.root}|{project.data_root}|{project.db_path}"
    if key not in _stores:
        _stores[key] = FactStore(project)
    return _stores[key]
