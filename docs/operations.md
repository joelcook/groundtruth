# Operations

Launch is the start. Sources change, people ask things you didn't expect, and
prices go up. These tools keep the assistant correct without heroics.

## `./gt report`: what's being asked

Reads `logs/tool_calls.jsonl` (every tool call the server handles) and writes
`reports/observability.md`:

- calls, active days, error rate, and p50/p95 latency per tool
- top searches
- **gaps**: lookups and searches that found nothing

Every gap is a to-do: add the fact or write the page, then add the question to
`evals/golden.yaml` so it stays fixed. `./gt report --days 7` narrows the window.

## `./gt watchdog`: is it up and honest

```bash
./gt watchdog                                         # local, over stdio
./gt watchdog --url https://your.host/mcp             # deployed (uses GT_MCP_API_KEY)
./gt watchdog --url https://your.host/mcp --max-age-hours 48
```

Checks `/healthz`, opens a real MCP session, lists tools, calls a fact tool and
the wiki search, and (with `--max-age-hours`) fails if the data is stale. Exit
code 0 means healthy. `.github/workflows/watchdog.yml` runs it daily.

## `./gt refresh`: the weekly agent

```
ingest -> build -> compile -> check -> eval -> pull request
```

```bash
./gt refresh                       # dry run: what would change
./gt refresh --write               # commits on a refresh/<date> branch
./gt refresh --write --open-pr     # and opens a PR (needs the gh CLI)
./gt refresh --write --live        # also run live evals (needs a key)
```

- It never touches main. Changes land on a branch a human reviews.
- A quiet week is free: no source changes means no commits, no PR, and no API spend.
- It stops at the first failed gate and tells you why.
- With `ANTHROPIC_API_KEY`, it drafts pages for new sources and refreshes stale
  ones. Without a key, it writes `build/compile-packet.md` and notes it in the PR.

`.github/workflows/refresh.yml` runs it every Monday. Turn on **Settings > Actions
> General > Allow GitHub Actions to create and approve pull requests**, and add an
`ANTHROPIC_API_KEY` secret if you want Claude drafting pages.

## `./gt bundle`: data without redeploys

See [deploy.md](deploy.md#update-data-without-redeploying).

## Upgrading groundtruth

Your project folder is yours; the code is the repo's. To upgrade:

```bash
git pull                 # or merge from upstream if you forked
./gt doctor              # any ./gt command reinstalls dependencies when pyproject.toml changes
./gt check && ./gt eval
```

Nothing in your project folder is touched by an upgrade.

## Many assistants, one repo

Each project is just a folder. `./gt use PATH` switches the default (stored in
`groundtruth.project`), `-p PATH` targets one for a single command, and the
Dockerfile's `PROJECT` build arg picks which one an image serves.
