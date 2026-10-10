# Getting started

From a fresh clone to an assistant you can talk to. Nothing here needs an API
key except where it says so.

## 0. Requirements

Python 3.10 or newer. That's it. `./gt` creates its own `.venv/` on first run
and installs everything into it. On Windows, run `py gt` instead of `./gt`.

Check your setup any time with `./gt doctor`.

## 1. See it work

```bash
./gt demo
./gt ask "how much does color printing cost?"
```

`./gt demo` builds and tests `examples/library`: six facts files, six wiki
pages, eighteen eval questions (fifteen run free; three need a model). Without
an API key, `./gt ask` shows a heuristic preview of which tools the model might
call and what they return; with `ANTHROPIC_API_KEY`, Claude answers for real.

## 2. Start your own

```bash
./gt init --list                          # see the templates
./gt init my-assistant --template saas    # blank, saas, college, city, clinic, library
```

This creates `my-assistant/` and points `./gt` at it (`./gt use PATH`
switches). The new project passes `./gt check` and `./gt eval` from the start,
on fictional example data.

**Lost at any point? Run `./gt status`.** It shows the road from here to
production, what's done, what's left, and the exact next command:

```
Road to production (2 of 7 done):
  ✓ 1. Capture your evidence           3 source(s) of your own
  ✗ 2. Turn it into facts and pages    3 source(s) not covered, 3 draft page(s) unfinished
  ✗ 3. Replace the template examples   17 fictional item(s) left
  ✗ 4. Write eval questions            0 question(s) of your own
  ✓ 5. Review changed sources          all reviewed
  ✗ 6. Pass the gates                  1 problem(s), first: examples: template examples are still live...
  ✗ 7. Try it in a client              0 tool call(s) logged

next: ./gt compile, then tell a coding agent: "do the work in my-assistant/build/compile-packet.md"
```

Open `my-assistant/README.md`. It's a one-page map of the folder.

## 3. Bring your evidence

```bash
./gt ingest https://help.example.com                  # crawls (sitemap first, robots.txt respected)
./gt ingest https://help.example.com/sitemap.xml      # exactly the pages in a sitemap
./gt ingest ~/Downloads/policies/                     # .md .txt .html (and .pdf with ./gt setup --pdf)
```

Pages land in `sources/pages/` as markdown, each with where it came from (the
URL for web pages, the original path for local files), a capture date, and a
content hash. The targets are saved in `groundtruth.yaml`, so plain `./gt
ingest` (and the weekly refresh) re-runs them. Unchanged pages are left alone.

If a page that your facts or wiki cite comes back changed, or its document is
gone upstream, `./gt check` fails and `./gt review` lists exactly which facts
files and pages to re-check. `./gt review --diff` shows what changed. Fix what's
out of date, then `./gt review --done`. Two documents with the same name (say,
`policy.md` from two folders) are kept as separate pages.

## 4. Turn evidence into facts and pages

```bash
./gt compile
```

This does three things, no API key needed:

- **Proposed facts.** One file per new source, `facts/_proposed-<source>.yaml`:
  every line with an exact value becomes a row holding that exact statement,
  served by a lookup tool, with the source wired in. Files starting with `_`
  are ignored, so it isn't live yet. Check each row against the source, delete
  what isn't a fact, rename the file without the `_`, and the tool is live.
  (Reshaping rows into real fields, like `price_usd: 39`, makes better tools;
  [facts.md](facts.md) shows how.)
- **Draft pages.** One skeleton per new source in `wiki/drafts/`, with the
  frontmatter and lineage table filled in. They're `status: draft`: not served,
  and their problems show as warnings, not failures, until you write them and
  set `status: published`.
- **A work order**, `build/compile-packet.md`: the sources nothing covers yet,
  every exact value found in each one (a price, a date, a phone number: each is
  a fact row waiting to happen; up to 60 lines per source in the packet, all of
  them in `build/fact-candidates.md`), starter eval questions to fill in, the
  rules, and the exact commands to verify.

Open the repo in Claude Code, Codex, or Cursor and say:

> Do the work in my-assistant/build/compile-packet.md

The agent writes `facts/*.yaml` rows and wiki pages, replaces the template's
fictional examples and eval questions, and runs `./gt check` and `./gt eval`
until they pass. (By hand: `./gt examples --remove` clears every example at once.)

The prompt's routing table (`{{FACT_TOOLS}}` in `prompts/system_prompt.md`) is
generated from your facts, so new tools show up there on their own. Edit the
rest of the prompt for your organization: scope, tone, and what to refuse.

With `ANTHROPIC_API_KEY` set, `./gt compile --write` drafts the wiki pages
itself (facts stay a human or agent call). Review with `git diff`.

Doing it by hand is fine too: [facts.md](facts.md) and [wiki.md](wiki.md) are
the whole format.

## 5. Prove it

First, make sure `evals/golden.yaml` holds questions you know the answers to
(the packet has starters; aim for ten or more) instead of the template's. See
[evals.md](evals.md). Then:

```bash
./gt check                # facts valid, no dates or $ in wiki prose, links resolve, evals wired up
./gt eval                 # free: your data answers each fact question, search finds each how-to
./gt check --production   # what the server and Docker require: also no template examples left
```

`./gt eval` says what it proved: `data` (the tools return the expected values)
and `retrieval` (search finds the expected page). Whether a model picks the
right tool for the question is what `./gt eval --tier live` tests.

## 6. Talk to it

```bash
./gt connect                            # every client, with your paths filled in
./gt connect claude-desktop --install   # also: cursor, windsurf (writes config),
                                        #       claude-code, codex (runs their CLI)
```

Restart the client and ask it something. Then run `./gt report` to see what was
asked and what came up empty. If the client says the server failed to start,
run `./gt check`: the server won't serve a project that fails it.

## 7. Put it online

`./gt serve` runs the HTTP server locally. It needs `./gt check --production`
to pass (`--force` skips that for local testing), and from then on it only
switches to an edited version once that version passes the checks. For a public endpoint (Copilot
Studio, ChatGPT, claude.ai, your own app), see [deploy.md](deploy.md). Set
`GT_MCP_API_KEY` before you expose it.

## Keep it honest

- `.github/workflows/ci.yml` runs the tests, `./gt check`, and `./gt eval` on
  every push.
- `.github/workflows/refresh.yml` re-ingests weekly and opens a PR when
  something changed.
- `./gt report` turns real questions into your next eval cases.

More in [operations.md](operations.md).
