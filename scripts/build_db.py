#!/usr/bin/env python3
"""Project captured fact evidence into the hard-fact database.

    sources/facts/*.yaml   →   db/groundtruth.db

This is the deterministic "projection" step in the pipeline. It never invents
data: it reads the evidence files (the immutable output of your scraper) and
writes them into SQLite, stamping every row with the source it came from.

Rebuild any time with:
    python scripts/build_db.py

When you bring your own data, replace the loaders below with ones that match
your schema. Keep the contract: every row gets a source_path.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "db" / "schema.sql"
FACTS = REPO_ROOT / "sources" / "facts" / "library-facts.yaml"
DB = REPO_ROOT / "db" / "groundtruth.db"


def _section_source(data: dict, key: str) -> dict:
    src = data.get(key, {}) or {}
    return {"source_path": src.get("source_path", "sources/"), "source_url": src.get("source_url")}


def build() -> int:
    if not FACTS.exists():
        print(f"missing evidence file: {FACTS}", file=sys.stderr)
        return 2
    data = yaml.safe_load(FACTS.read_text(encoding="utf-8"))
    now = dt.datetime.now().isoformat(timespec="seconds")

    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))

    # branches (carry their own source inline)
    for b in data.get("branches", []):
        conn.execute(
            "INSERT INTO branches (id, name, address, phone, region, source_path, source_url, scraped_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (b["id"], b["name"], b.get("address"), b.get("phone"), b.get("region"),
             b["source_path"], b.get("source_url"), now),
        )

    hsrc = _section_source(data, "_hours_source")
    for h in data.get("hours", []):
        conn.execute(
            "INSERT INTO hours (branch_id, day, opens, closes, source_path, source_url)"
            " VALUES (?,?,?,?,?,?)",
            (h["branch_id"], h["day"], h.get("opens"), h.get("closes"),
             hsrc["source_path"], hsrc["source_url"]),
        )

    esrc = _section_source(data, "_events_source")
    for e in data.get("events", []):
        conn.execute(
            "INSERT INTO events (title, branch_id, category, date_iso, date_text, start_time,"
            " registration_required, notes, source_path, source_url)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e["title"], e.get("branch_id"), e.get("category"), e.get("date_iso"),
             e["date_text"], e.get("start_time"), int(bool(e.get("registration_required"))),
             e.get("notes"), esrc["source_path"], esrc["source_url"]),
        )

    psrc = _section_source(data, "_programs_source")
    for p in data.get("programs", []):
        conn.execute(
            "INSERT INTO programs (id, name, audience, schedule_text, registration_required,"
            " description, source_path, source_url) VALUES (?,?,?,?,?,?,?,?)",
            (p["id"], p["name"], p.get("audience"), p.get("schedule_text"),
             int(bool(p.get("registration_required"))), p.get("description"),
             psrc["source_path"], psrc["source_url"]),
        )

    fsrc = _section_source(data, "_fees_source")
    for f in data.get("fees", []):
        conn.execute(
            "INSERT INTO fees (fee_type, amount_usd, unit, refundable, notes, source_path, source_url)"
            " VALUES (?,?,?,?,?,?,?)",
            (f["fee_type"], float(f["amount_usd"]), f.get("unit"), f.get("refundable"),
             f.get("notes"), fsrc["source_path"], fsrc["source_url"]),
        )

    csrc = _section_source(data, "_contacts_source")
    for c in data.get("contacts", []):
        conn.execute(
            "INSERT INTO contacts (department, role, name, phone, email, branch_id, notes,"
            " source_path, source_url) VALUES (?,?,?,?,?,?,?,?,?)",
            (c["department"], c.get("role"), c.get("name"), c.get("phone"), c.get("email"),
             c.get("branch_id"), c.get("notes"), csrc["source_path"], csrc["source_url"]),
        )

    conn.commit()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("branches", "hours", "events", "programs", "fees", "contacts")
    }
    conn.close()
    print(f"built {DB.relative_to(REPO_ROOT)}")
    for t, n in counts.items():
        print(f"  {t:10} {n:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
