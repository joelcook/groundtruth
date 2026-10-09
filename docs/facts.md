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

`get_fee(fee_type="lost card")` scores every row: an exact key or alias match
first, then the query appearing inside a field, then word overlap ("black and
white printing" finds `printing-bw`; "late fees" finds `overdue-fine` through
its aliases). It returns the best matches, each with its `source_url`. An empty
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
date_field: date
tools:
  - name: list_events
    kind: list
    filters: [category]
    upcoming: true
rows:
  - {title: Book Sale, date: "2026-07-11", when: "Saturday, July 11, 2026"}
  - {title: Story Time, when: Every Wednesday}      # no date = recurring, always included
```

`upcoming: true` adds an `upcoming_only` parameter (default true) and sorts
soonest first. Dates must be ISO `YYYY-MM-DD`; quoting them is safest. Keep a
human-readable `when` field for the model to quote.

### Default tool

A file with no `tools:` gets one lookup tool named `get_<file name>`.

## Rules `./gt check` enforces

- Every row has its `key`, and keys are unique (case-insensitive).
- Every row has a source: the file's `source:` or the row's own
  `source_url` / `source_path`.
- Tool names are unique across files and aren't `search_wiki` / `read_wiki_page`.
- `limit` and `upcoming_only` are reserved parameter names.
- Filters and `match` name fields that exist; `ref` names a real collection.
- `date_field` values are `YYYY-MM-DD`.

Warnings (they don't fail the check): a source file that doesn't exist, or a
`ref` value that isn't a key in the referenced file.

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
Adding a **new tool** needs a server restart; changing data doesn't.

## Choosing what's a fact

Ask: *if this is wrong, does someone get hurt?* A missed deadline, a wrong bill,
a locked door, a wasted trip. If yes, it's a fact: put it in YAML, and write the
wiki prose around it ("the assistant looks up the current fee with `get_fee`").
If being approximately right is fine ("how do holds work?"), it's wiki.
