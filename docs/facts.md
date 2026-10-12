# Facts reference

Facts are the things that must be exact: prices, dates, hours, phone numbers,
limits, deadlines, who-to-call. They live in `facts/*.yaml`, one file per kind
of thing. Each file says what the rows are, where they came from, and which
tools the model gets. `./gt check` validates everything below.

## The whole format

```yaml
# facts/fees.yaml (the file name is the collection name unless you set `name:`)
description: Fees and fines in US dollars.        # shown to the model; say what's in here
key: fee_type                                      # unique per row (default: id)
source:                                            # default provenance for every row
  path: sources/pages/fees.md                      #   the evidence file in sources/
  url: https://example.org/fees                    #   where a human can verify it
match: [fee_type, notes]                           # optional: fields a lookup searches (default: all text fields)
date_field: date                                   # optional: ISO YYYY-MM-DD field, enables "upcoming"
tools:
  - name: get_fee                                  # lowercase_snake_case, unique across all files
    kind: lookup                                   # lookup | list
    arg: fee_type                                  # lookup only: the parameter name (default: query)
    description: >                                 # the model reads this to pick tools. Be specific.
      Fees and fines in dollars (printing, replacement card, meeting room).
      Every dollar amount the assistant states MUST come from here.
rows:
  - fee_type: printing-bw
    aliases: [black and white printing, copies]    # extra names people use
    amount_usd: 0.10
    unit: per page
    notes: Black-and-white printing and copies.
  - fee_type: replacement-card
    amount_usd: 2.00
    unit: per card
    source_url: https://example.org/cards          # a row can override the file's source
```

Fields are free-form: use whatever columns your domain needs. Names must be
letters, numbers, and underscores. Values can be text, numbers, booleans, lists,
or `null`.

## Tool kinds

### `lookup`: one forgiving text argument

```yaml
- name: get_fee
  kind: lookup
  arg: fee_type
```

Every result carries a `source`: the row's URL, or its file path when the
evidence is a local document (`source: {path: sources/pages/pricing.md}` with no
URL is fine). The assistant cites it.

`get_fee(fee_type="lost card")` scores every row: an exact key or alias match
first, then the query appearing inside a field, then word overlap ("black and
white printing" finds `printing-bw`; "late fees" finds `overdue-fine` through
its aliases). It returns the best matches, each with its `source`. An empty
argument returns every row. No match returns a `hint` listing the known keys, so
the model can try again instead of guessing.

### `list`: optional filters

```yaml
- name: get_hours
  kind: list
  filters:
    - field: branch
      ref: branches          # resolve "main" -> "central" through branches.yaml keys, names, aliases
    - field: day             # "sat" matches "saturday"
  description: Opening hours for a branch, optionally for one day.
```

Each filter becomes an optional parameter. Values match forgivingly (case, a
prefix, plurals). Options per filter:

| Option | What it does |
|---|---|
| `field` | The row field to filter on (required) |
| `param` | Parameter name the model sees, if different from the field |
| `ref` | Another collection: the value is looked up there first, so aliases work across files |
| `wildcards` | Values that match every filter, e.g. `[all]` so an "all ages" program shows up for "teens" |

A filter can be written as just the field name: `filters: [category, audience]`.

### Dated rows and "upcoming only"

```yaml
# facts/events.yaml
description: Upcoming events.
key: title
source: {path: sources/pages/events.md}
date_field: date
tools:
  - name: list_events
    kind: list
    filters: [category]
    upcoming: true
    description: Events, soonest first. Every event date the assistant states comes from here.
rows:
  - {title: Book Sale, category: sale, date: "2026-07-11", when: "Saturday, July 11, 2026"}
  - {title: Story Time, category: kids, when: Every Wednesday}   # no date = recurring, always included
```

`upcoming: true` adds an `upcoming_only` parameter (default true) and sorts
soonest first. Dates must be ISO `YYYY-MM-DD`; quoting them is safest. Keep a
human-readable `when` field for the model to quote.

### Proposed and parked files

Files whose names start with `_` are ignored. `./gt compile` uses that: it
writes `facts/_proposed-<source>.yaml`, one row per exact statement in a new
source (`{id: ..., statement: "The Team plan costs USD 39 per month."}`) with a
lookup tool. Check the rows, rename it to `facts/<source>.yaml`, and it's live.
Statements are a fine start; real fields (`price_usd: 39`) make sharper tools.

### Default tool

A file with no `tools:` gets one lookup tool named `get_<file name>`.

## Rules `./gt check` enforces

- Every row has its `key`, and keys are unique (case-insensitive).
- Every row has a source: the file's `source:` or the row's own
  `source_url` / `source_path`.
- Tool names are unique across files and aren't `search_wiki` / `read_wiki_page`.
- `limit` and `upcoming_only` are reserved parameter names.
- Filters and `match` name fields that exist; `ref` names a real collection.
- `date_field` values are real dates written `YYYY-MM-DD` (`2026-02-30` fails).

- A `source_path` points at a file that exists. (A typo'd path fails, with the
  closest real file suggested.)

**Grounding.** Every exact value in a row (a number, money, a date, a time, a
phone number, an email, a URL) must appear in the captured source the row cites:
its own `source` / `source_path`, else the file's. `./gt check` warns about any
that don't; `./gt check --production` (what the server, Docker, and data bundles
use) fails on them, and so does every unattended fact update. It compares by
meaning, not spelling: `39` matches "$39" and "USD 39.00", `1200` matches
"1,200", `0.1` matches "$0.10" and "10 cents", `2026-09-01` matches
"September 1, 2026", `"13:00"` matches "1 p.m.", and phone numbers compare by
their digits. Words (a weekday, a branch name) aren't checked here; a model
checks those when the refresh host updates facts. Your `key`, `aliases`, and
`tags` are yours, not the source's, so they're skipped; `notes` are checked,
since the assistant reads them as part of the fact (and an alias the refresh host
adds must be a word the source uses). A row whose only
source is a URL (nothing captured) fails too: there's nothing to check it
against, so `./gt ingest` the page first.

A value you derive (a monthly price from an annual one) isn't in any source and
fails, on purpose. Store what the source says, and let the assistant do the
arithmetic out loud, or cite a source that states the derived value.

Warning (doesn't fail the check): a `ref` value that isn't a key in the
referenced file.

And one more that does fail: if a source a facts file cites has changed since
the last `./gt review --done`, the facts may be stale. `./gt review` names them.

## YAML gotchas

- **Commas inside `{braces}` start a new field.** `{address: 1 Main St, Suite 2}`
  is two fields. Quote it: `{address: "1 Main St, Suite 2"}`. `./gt check`
  points this out when it happens.
- **Quote phone numbers, times, and ZIP codes**: `"555-0100"`, `"09:00"`,
  `"02134"`. Unquoted, YAML may read them as numbers.
- **`yes`, `no`, `on`, and `off` are booleans** in YAML. Quote them if you mean
  the words.

## How it runs

`./gt build` (or any command that needs facts) projects the YAML into
`build/facts.db`, a plain SQLite file you can open with any SQLite tool. The
build is deterministic and tracks a fingerprint of your YAML, so it reruns
itself whenever a file changes, and a running server notices within a second.
Once the change passes `./gt check`, it goes live: new data, new tools, and an
edited prompt. (A local stdio client like Claude Desktop reads the tool list
when it starts, so restart it to see a new tool.)

## Choosing what's a fact

Ask: *if this is wrong, does someone get hurt?* A missed deadline, a wrong bill,
a locked door, a wasted trip. If yes, it's a fact: put it in YAML, and write the
wiki prose around it ("the assistant looks up the current fee with `get_fee`").
If being approximately right is fine ("how do holds work?"), it's wiki.
