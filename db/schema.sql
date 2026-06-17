-- groundtruth — hard-fact schema (example domain: a public library system)
--
-- This is the half of the knowledge base that is NOT allowed to be wrong.
-- Every volatile or harm-if-wrong value lives here as a row, never as prose:
-- dates, dollar amounts, hours, phone numbers, registration requirements.
--
-- Two rules make this a *grounded* store rather than just a database:
--   1. Every table carries provenance — source_path (the frozen evidence the
--      row was projected from) and source_url (where a human can verify it).
--      That is the audit trail; a fact with no source should never exist.
--   2. The wiki prose is forbidden from restating these values (see
--      scripts/lint_no_hardfacts.py). If a date or a "$" appears in a wiki
--      page, the build fails. Facts come from here, through a tool, every time.
--
-- Swap-your-data note: replace these tables with the entities YOUR domain can
-- be harmed by getting wrong, then point scripts/build_db.py at your sources.

PRAGMA foreign_keys = ON;

-- Physical locations. Also the home for "where / how do I reach" facts.
CREATE TABLE branches (
  id          TEXT PRIMARY KEY,          -- stable slug: 'central', 'eastside'
  name        TEXT NOT NULL,
  address     TEXT,
  phone       TEXT,
  region      TEXT,
  source_path TEXT NOT NULL,
  source_url  TEXT,
  scraped_at  TEXT
);

-- Opening hours, one row per branch per day. The "what time does X open?" facts.
CREATE TABLE hours (
  id          INTEGER PRIMARY KEY,
  branch_id   TEXT NOT NULL REFERENCES branches(id),
  day         TEXT NOT NULL,             -- 'monday' .. 'sunday'
  opens       TEXT,                      -- 'HH:MM' (24h), or NULL when closed
  closes      TEXT,                      -- 'HH:MM' (24h), or NULL when closed
  source_path TEXT NOT NULL,
  source_url  TEXT
);
CREATE INDEX idx_hours_branch ON hours(branch_id);

-- Dated things. The "when is X?" facts — the single most dangerous category to
-- paraphrase, because a wrong date is a missed event.
CREATE TABLE events (
  id                    INTEGER PRIMARY KEY,
  title                 TEXT NOT NULL,
  branch_id             TEXT REFERENCES branches(id),
  category              TEXT,            -- 'story-time' | 'book-sale' | 'closure' | 'workshop' | 'program'
  date_iso              TEXT,            -- 'YYYY-MM-DD' (sortable); NULL for recurring
  date_text             TEXT NOT NULL,   -- human label, e.g. 'Every Wednesday'
  start_time            TEXT,            -- 'HH:MM' (24h)
  registration_required INTEGER NOT NULL DEFAULT 0,
  notes                 TEXT,
  source_path           TEXT NOT NULL,
  source_url            TEXT
);
CREATE INDEX idx_events_category ON events(category);
CREATE INDEX idx_events_date ON events(date_iso);

-- Ongoing programs/services. Mostly narrative, but the registration flag and
-- schedule are facts a patron can be harmed by getting wrong.
CREATE TABLE programs (
  id                    TEXT PRIMARY KEY,
  name                  TEXT NOT NULL,
  audience              TEXT,            -- 'kids' | 'teens' | 'adults' | 'all'
  schedule_text         TEXT,
  registration_required INTEGER NOT NULL DEFAULT 0,
  description           TEXT,
  source_path           TEXT NOT NULL,
  source_url            TEXT
);
CREATE INDEX idx_programs_audience ON programs(audience);

-- Money. The "$" facts. amount_usd is the canonical number; prose may never
-- restate it.
CREATE TABLE fees (
  id          INTEGER PRIMARY KEY,
  fee_type    TEXT NOT NULL,             -- 'printing-bw' | 'replacement-card' | 'meeting-room' | 'overdue-fine'
  amount_usd  REAL NOT NULL,
  unit        TEXT,                      -- 'per_page' | 'per_card' | 'per_hour' | 'per_day' | 'flat'
  refundable  TEXT,
  notes       TEXT,
  source_path TEXT NOT NULL,
  source_url  TEXT
);
CREATE INDEX idx_fees_type ON fees(fee_type);

-- People & desks. Phone numbers and emails must come from here, with a source.
CREATE TABLE contacts (
  id          INTEGER PRIMARY KEY,
  department  TEXT NOT NULL,             -- 'reference' | 'youth-services' | 'circulation' | 'accessibility'
  role        TEXT,
  name        TEXT,
  phone       TEXT,
  email       TEXT,
  branch_id   TEXT REFERENCES branches(id),
  notes       TEXT,
  source_path TEXT NOT NULL,
  source_url  TEXT
);
CREATE INDEX idx_contacts_department ON contacts(department);
