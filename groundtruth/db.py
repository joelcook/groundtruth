"""The hard-fact query layer — the half of the system that is not allowed to be wrong.

Every function here answers a *volatile* or *harm-if-wrong* question — a date, a
dollar amount, an opening time, a phone number — by reading a row out of
``db/groundtruth.db``. The language model never states these values from memory
or from wiki prose; it calls one of these functions (exposed as MCP tools in
``groundtruth.mcp``) and quotes the result, including the source.

Design notes that travel to any domain:
  * Read-only. Nothing here writes. The database is a *projection*, rebuilt from
    frozen evidence by ``scripts/build_db.py``.
  * Provenance rides along. Every row carries ``source_path``/``source_url`` so
    the answer can cite where the fact came from.
  * Forgiving inputs, exact outputs. Callers pass fuzzy strings ("central",
    "Saturday", "printing"); we normalize and match, but the *values* returned
    are exact.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "db" / "groundtruth.db"

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_ALIASES = {
    "mon": "monday", "tue": "tuesday", "tues": "tuesday", "wed": "wednesday",
    "thu": "thursday", "thur": "thursday", "thurs": "thursday", "fri": "friday",
    "sat": "saturday", "sun": "sunday", "today": None, "tomorrow": None,
}


def db_path() -> Path:
    """Resolve the database path, allowing an env override for tests/containers."""
    override = os.environ.get("GT_DB_PATH", "").strip()
    return Path(override) if override else DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"database not found at {path}. Build it with: "
            f"python scripts/build_db.py"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _clamp(limit: int | None, default: int, cap: int) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), cap))


def _normalize_day(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    if text in DAYS:
        return text
    return DAY_ALIASES.get(text, None) if text in DAY_ALIASES else None


# ---------------------------------------------------------------- branches

def list_branches() -> list[dict]:
    """All library branches with address, phone, and region."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT id, name, address, phone, region, source_url "
            "FROM branches ORDER BY name"
        )
        return _rows(cur)


def _resolve_branch(conn: sqlite3.Connection, needle: str | None) -> str | None:
    """Map a fuzzy branch string ('central', 'main', 'eastside') to a branch id."""
    if not needle:
        return None
    text = needle.strip().lower()
    cur = conn.execute("SELECT id, name FROM branches")
    branches = _rows(cur)
    for b in branches:
        if text == b["id"] or text == b["name"].lower():
            return b["id"]
    if text in {"main", "downtown", "central library"}:
        for b in branches:
            if b["id"] == "central":
                return b["id"]
    for b in branches:
        if text in b["name"].lower() or text in b["id"]:
            return b["id"]
    return None


# ---------------------------------------------------------------- hours

def get_hours(branch: str = "central", day: str | None = None) -> dict:
    """Opening hours for a branch. Optionally narrow to a single day of the week.

    Returns the resolved branch plus a day->{opens, closes, closed} map. Times
    are 24-hour 'HH:MM'. This is a hard fact: never guess a library's hours.
    """
    with _connect() as conn:
        branch_id = _resolve_branch(conn, branch) or "central"
        binfo = _rows(conn.execute(
            "SELECT id, name, source_url FROM branches WHERE id = ?", (branch_id,)
        ))
        wanted = _normalize_day(day)
        rows = _rows(conn.execute(
            "SELECT day, opens, closes, source_url FROM hours WHERE branch_id = ?",
            (branch_id,),
        ))
    by_day = {r["day"]: r for r in rows}
    days = [wanted] if wanted else DAYS
    out = {}
    for d in days:
        r = by_day.get(d)
        if r is None or (r["opens"] is None and r["closes"] is None):
            out[d] = {"closed": True}
        else:
            out[d] = {"closed": False, "opens": r["opens"], "closes": r["closes"]}
    return {
        "branch": binfo[0]["name"] if binfo else branch_id,
        "branch_id": branch_id,
        "hours": out,
        "source_url": (binfo[0]["source_url"] if binfo else None),
    }


# ---------------------------------------------------------------- events

def list_events(category: str | None = None, upcoming_only: bool = False,
                limit: int | None = None) -> list[dict]:
    """List events (story times, workshops, closures, sales). Newest dates first.

    ``upcoming_only`` drops events whose ``date_iso`` is in the past. Recurring
    events (no fixed date) are always included.
    """
    import datetime as _dt
    n = _clamp(limit, default=25, cap=100)
    sql = (
        "SELECT e.title, e.category, e.date_iso, e.date_text, e.start_time, "
        "e.registration_required, e.notes, e.source_url, b.name AS branch "
        "FROM events e LEFT JOIN branches b ON e.branch_id = b.id"
    )
    params: list[Any] = []
    if category:
        sql += " WHERE e.category = ?"
        params.append(category.strip().lower())
    sql += " ORDER BY (e.date_iso IS NULL) DESC, e.date_iso ASC, e.title ASC"
    with _connect() as conn:
        rows = _rows(conn.execute(sql, params))
    if upcoming_only:
        today = _dt.date.today().isoformat()
        rows = [r for r in rows if r["date_iso"] is None or r["date_iso"] >= today]
    for r in rows:
        r["registration_required"] = bool(r["registration_required"])
    return rows[:n]


def get_event(query: str) -> list[dict]:
    """Find events by a fuzzy title/category/notes match (e.g. 'story time', 'book sale')."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    rows = list_events(limit=100)
    hits = [
        r for r in rows
        if needle in r["title"].lower()
        or needle in (r["category"] or "")
        or needle in (r["notes"] or "").lower()
    ]
    return hits or [
        r for r in rows
        if any(tok in r["title"].lower() for tok in needle.split())
    ]


# ---------------------------------------------------------------- programs

def list_programs(audience: str | None = None) -> list[dict]:
    """Ongoing programs/services, optionally filtered by audience (kids/teens/adults/all)."""
    sql = (
        "SELECT id, name, audience, schedule_text, registration_required, "
        "description, source_url FROM programs"
    )
    params: list[Any] = []
    if audience:
        sql += " WHERE audience = ? OR audience = 'all'"
        params.append(audience.strip().lower())
    sql += " ORDER BY name"
    with _connect() as conn:
        rows = _rows(conn.execute(sql, params))
    for r in rows:
        r["registration_required"] = bool(r["registration_required"])
    return rows


def get_program(name_or_id: str) -> dict | None:
    """Look up one program by id or fuzzy name ('summer reading', 'tech help')."""
    needle = (name_or_id or "").strip().lower()
    if not needle:
        return None
    for r in list_programs():
        if needle == r["id"] or needle in r["name"].lower():
            return r
    for r in list_programs():
        if any(tok in r["name"].lower() for tok in needle.split() if len(tok) > 2):
            return r
    return None


# ---------------------------------------------------------------- fees

def get_fee(fee_type: str | None = None) -> list[dict]:
    """Fees and fines in dollars. Pass a fee_type ('printing', 'meeting room',
    'replacement card', 'overdue') to filter, or omit for the full schedule.

    Dollar amounts are hard facts: they come from here, never from prose.
    """
    with _connect() as conn:
        rows = _rows(conn.execute(
            "SELECT fee_type, amount_usd, unit, refundable, notes, source_url "
            "FROM fees ORDER BY fee_type"
        ))
    if fee_type:
        needle = fee_type.strip().lower().replace(" ", "-")
        loose = needle.replace("-", " ")
        rows = [
            r for r in rows
            if needle in r["fee_type"]
            or loose in (r["notes"] or "").lower()
            or any(tok in r["fee_type"] for tok in loose.split())
        ]
    return rows


# ---------------------------------------------------------------- contacts

def get_contact(department: str | None = None) -> list[dict]:
    """Phone numbers and emails by department ('reference', 'youth', 'circulation',
    'accessibility'). Contact details are hard facts and must cite a source."""
    with _connect() as conn:
        rows = _rows(conn.execute(
            "SELECT c.department, c.role, c.name, c.phone, c.email, c.notes, "
            "c.source_url, b.name AS branch "
            "FROM contacts c LEFT JOIN branches b ON c.branch_id = b.id "
            "ORDER BY c.department"
        ))
    if department:
        needle = department.strip().lower()
        rows = [
            r for r in rows
            if needle in r["department"]
            or needle in (r["role"] or "").lower()
            or needle in (r["notes"] or "").lower()
        ]
    return rows
