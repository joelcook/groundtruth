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

This creates `my-assistant/` and points `./gt` at it (`./gt status` shows which
project is active; `./gt use PATH` switches). The new project passes `./gt
check` and `./gt eval` from the start.

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

This writes `build/compile-packet.md`, a work order listing the sources nothing
covers yet, pages whose sources changed, the rules, and how to verify. Open the
repo in Claude Code, Codex, or Cursor and say:

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

```bash
./gt check     # facts valid, no dates or $ in wiki prose, links resolve, evals wired up
./gt eval      # free: every fact question answerable from your data, every how-to findable
```

Then replace the template's example questions in `evals/golden.yaml` with ten
you know the answers to. See [evals.md](evals.md).

## 6. Talk to it

```bash
./gt connect                            # every client, with your paths filled in
./gt connect claude-desktop --install   # also: cursor, windsurf (writes config),
                                        #       claude-code, codex (runs their CLI)
```

Restart the client and ask it something. Then run `./gt report` to see what was
asked and what came up empty.

## 7. Put it online

`./gt serve` runs the HTTP server locally. For a public endpoint (Copilot
Studio, ChatGPT, claude.ai, your own app), see [deploy.md](deploy.md). Set
`GT_MCP_API_KEY` before you expose it.

## Keep it honest

- `.github/workflows/ci.yml` runs the tests, `./gt check`, and `./gt eval` on
  every push.
- `.github/workflows/refresh.yml` re-ingests weekly and opens a PR when
  something changed.
- `./gt report` turns real questions into your next eval cases.

More in [operations.md](operations.md).
