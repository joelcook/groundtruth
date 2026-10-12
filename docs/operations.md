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
./gt watchdog --repo --url https://your.host/mcp      # plus the repo checks below
```

Checks `/healthz`, opens a real MCP session, lists tools, calls a fact tool and
the wiki search, and (with `--max-age-hours`) fails if the data is stale.
`--repo` adds what only the repo knows, so it still fires when the refresh host
is off:

- `ALERTS.md` lists no open alert;
- a `refresh:` commit landed on main in the last 30 hours (`--refresh-max-hours`;
  only when `groundtruth.yaml` names a refresh host);
- CI passed on main's head commit (in GitHub Actions, which sets `GITHUB_TOKEN`);
- the deployed server's code is at most 72 hours behind main
  (`--code-max-lag-hours`). That needs the image built with
  `--build-arg GT_BUILD_COMMIT=$(git rev-parse HEAD)`, which `/healthz` reports.

Exit code 0 means healthy. `.github/workflows/watchdog.yml` runs it daily with
`--repo`, plus `--url` when you set the repo variable `GT_WATCHDOG_URL`.

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
- Changed sources that facts cite go into the PR as a **Needs your review** list.
  CI stays red on that PR until someone re-checks the facts and runs
  `./gt review --done`, so stale facts can't merge quietly.

`.github/workflows/refresh.yml` runs it every Monday. Turn on **Settings > Actions
> General > Allow GitHub Actions to create and approve pull requests**, and add an
`ANTHROPIC_API_KEY` secret if you want Claude drafting pages.

That's the right start. Its cost is a person merging a PR every week. The
refresh host below removes that.

## The refresh host: nobody has to merge anything

One always-on machine (a spare Linux box, a Mac mini, a small VPS) keeps main
current by itself. Every night it re-captures your sources. During the day it
re-checks the sources your facts cite every two hours. Every half hour it has
headless Claude Code rewrite the pages those changes made stale, with a second
model checking each claim against the sources. It pushes main whenever the gates
pass, and the data reaches your deployed server as a bundle, with no deploy.
Whatever it can't handle becomes a line in `ALERTS.md`, which the daily
watchdog fails on, so GitHub emails you.

### What runs, and when

| When | Job | What it does | Commits it pushes |
|---|---|---|---|
| nightly (03:00) | `./gt host run refresh` | `./gt refresh --write --push`: ingest every source, build, check, eval | `refresh: sources (date)`, or an empty `refresh: no source changes (date)` so the watchdog knows it ran |
| every 5 minutes | `./gt host watch` | publishes new alerts; every 2 hours from 05:00 to 22:00, a quick refresh of just the sources facts cite; a full refresh when a person pushes main; alerts about stale pages (if rewrites aren't scheduled) and about a deployed server running old code (with `GT_WATCHDOG_URL`) | `alerts: ...`, `refresh: quick sources (...)` |
| every 30 minutes | `./gt host run rewrite` | `./gt rewrite` on stale pages (below) | `wiki: rewrite <page> from current sources`, `wiki: rewrite log: ...` |
| daily, in GitHub | `watchdog.yml` | `./gt watchdog --repo`: open alerts, refresh age, CI on main, deployed code lag | (none) |
| on every push to main | `ci.yml`, `publish-data.yml` | the gates; then a data bundle your servers hot-swap ([deploy.md](deploy.md#update-data-without-redeploying)) | (none) |

Every job runs in its own git worktree reset to `origin/main`, with main's
code, never in your checkout, so a stale or dirty checkout can't leak into what
gets pushed.

### Set it up

On the machine (Linux or macOS, always on, able to push to your repo):

```bash
git clone https://github.com/you/your-fork && cd your-fork
./gt doctor                      # installs dependencies; checks the claude CLI too
cp .env.example .env             # gitignored: never commit it
claude setup-token               # then put the token in .env: CLAUDE_CODE_OAUTH_TOKEN=...
hostname -s                      # put this in your project's groundtruth.yaml as  host: <name>
git commit -am "Name the refresh host" && git push
./gt host check                  # git push, the host name, claude, alert hooks, cron
./gt host install                # --at 04:00 moves the nightly run; --no-rewrite skips rewrites
```

`./gt host print` shows the crontab block without installing it, if you'd
rather use systemd or launchd. Logs go to `logs/host/` in the checkout.

**One pusher.** Every job reads the `host:` line from `origin/main`, not your
checkout, and stands down on any other machine. To move the host, change that
line on main; the old machine stops on its next run (then run
`./gt host uninstall` there).

**Claude on a subscription.** `./gt rewrite` runs `claude -p`, Claude Code's
print mode, on your Claude plan instead of a metered API key. A token from
`claude setup-token` in `.env` outlasts the interactive login. If it expires
anyway, rewrites stop and you get an alert telling you how to fix it.

### How a stale page gets rewritten (`./gt rewrite`)

A page is stale when a source it cites changed since the last review, was
re-captured after the page's `last_compiled`, or the page has no
`last_compiled`. For each one, up to `GT_REWRITE_MAX_PAGES` (10) per run:

1. **Write.** Claude Code gets one page, its sources, the diff of each changed
   source since its last review, the fact tools to name instead of exact
   values, and the rules from [wiki.md](wiki.md). The writer is the `haiku`
   alias once that's Haiku 5.5 or newer, otherwise `sonnet`. It can read the
   repo and edit files, but can't run commands or reach the network.
2. **Contain.** Any file besides the page is reverted. The page's
   `last_compiled` and `compiled_by` (the exact model) are stamped. A page that
   shrank below 40% of its length is sent back.
3. **Gate.** `./gt check` must find nothing that wasn't already failing, and
   `./gt eval` must still pass. These catch facts in prose, broken links, and
   frontmatter problems.
4. **Check the claims.** `sonnet`, with read-only tools, reads the page beside
   its sources and lists, as JSON, every claim the sources don't support. Any
   unsupported claim sends the page back, the same as a failed gate.
5. **Retry once.** A failed attempt goes to `sonnet` with the list of what
   failed. If that fails too, the page waits seven days, or until its sources
   change again, and you get an alert.
6. **Commit.** `wiki: rewrite <page> from current sources`, with why it was
   stale, the models, and a one-line summary in the body. If the wiki has a
   `wiki/log.md`, the run adds an entry to it.
7. **Record the review.** Once every page citing a changed source has passed
   the claim check, and no facts file cites that source, the source is recorded
   as reviewed in `sources.lock.yaml`. That claim check is the review: a model
   compared every claim with the current source. A source that facts cite is
   never recorded this way (see below).

**Usage limits.** When Claude reports the plan's limit, or any usage window
passes `GT_REWRITE_MAX_UTILIZATION` (80%), the run stops and later runs wait
until that window resets. This leaves headroom for the people who share the
plan. Rewrites also stay out of the way of the nightly refresh (quiet hours
around `GT_REFRESH_AT`).

Run it by hand too, on a clean checkout (it reverts anything but the page):

```bash
./gt rewrite --list                         # what's stale, and why
./gt rewrite --page wiki/x.md --dry-run     # rewrite and check one page; keep nothing
./gt rewrite                                # rewrite and commit; you push
```

### What still needs a person

- **Facts.** When a source that facts cite changes, you get an alert, and
  `./gt check` on main stays red until someone runs `./gt review --diff`, fixes
  the facts, and runs `./gt review --done`. Meanwhile the deployed server keeps
  serving the last version that passed, and no bundle is published. Facts are
  where a wrong answer costs someone, so a model never signs off on them.
- **A page that failed on every model** (an alert names the claim or check).
- **New sources nothing covers yet:** `./gt compile`, then the packet.
- **Code changes:** data ships in bundles, but code needs a new image. The
  watcher and the watchdog tell you when the deployed code falls behind main.
- **An expired Claude login** (an alert says how to fix it).

### Alerts

Each job writes what it can't handle to `logs/host/alerts.log`. Every five
minutes the watcher copies new lines into `ALERTS.md` in the project and pushes
it. An alert that's already open isn't added again, even when its numbers
change. The daily watchdog fails while `ALERTS.md` lists anything.

```bash
./gt alerts           # what's open, and this machine's latest log lines
```

Fix the cause, then delete the alert's line from `ALERTS.md` and push. For
faster notice, set `GT_ALERT_COMMAND` (for example, a `curl` to ntfy.sh or a
Slack webhook) or `GT_ALERT_EMAIL` in `.env`.

### Safety rails

- **Gates before every push.** Nothing reaches main unless `./gt check` finds
  nothing new and `./gt eval` passes. A failed refresh leaves main alone and
  raises an alert.
- **An outage isn't a deletion.** A refresh that would mark more than a fifth
  of your sources as removed upstream stops instead, so a site that's down
  can't empty your evidence.
- **One writer at a time.** The refresh holds a lock for its whole run, and
  rewrites and alerts take it to push, so nothing lands on main mid-refresh. A
  push that finds main has moved replays its commits on top. Leftovers that no
  longer apply are dropped, and the next run redoes them.
- **Bot commits don't trigger refreshes.** The watcher re-captures sources when
  a person pushes main, not for `refresh:`, `wiki: rewrite`, or `alerts:`
  commits.

### Settings

Put these in the host's `.env`:

| Setting | Default | What it does |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | (none) | From `claude setup-token`; keeps rewrites working past the interactive login |
| `GT_REWRITE_MODEL` | haiku if 5.5+, else sonnet | The writer |
| `GT_REWRITE_FALLBACK_MODEL` | `sonnet` | The one retry |
| `GT_REWRITE_CHECK_MODEL` | `sonnet` | The claim checker; `off` skips it (then no source is ever recorded as reviewed) |
| `GT_REWRITE_MAX_PAGES` | 10 | Pages per run |
| `GT_REWRITE_MAX_UTILIZATION` | 0.8 | Pause past this share of any usage window |
| `GT_REWRITE_QUIET_HOURS` | around `GT_REFRESH_AT` | When rewrites don't run or push |
| `GT_QUICK_REFRESH_SECONDS` | 7200 | How often the quick refresh runs by day; 0 turns it off |
| `GT_WATCHDOG_URL` | (none) | Your `/mcp` URL: alert when the deployed code is behind main |
| `GT_ALERT_COMMAND`, `GT_ALERT_EMAIL` | (none) | Extra alert delivery |
| `GT_HOST_WORKDIR` | `~/.cache/groundtruth` | Where the jobs' worktrees live |

## `./gt bundle`: data without redeploys

See [deploy.md](deploy.md#update-data-without-redeploying).

## Upgrading groundtruth

Your project folder is yours; the code is the repo's. To upgrade:

```bash
git pull                 # or merge from upstream if you forked
./gt doctor              # any ./gt command reinstalls dependencies when pyproject.toml changes
./gt check && ./gt eval
```

Nothing in your project folder is touched by an upgrade. A refresh host's jobs
run main's code from their worktrees, so they pick up most changes on their
next run. Still `git pull` the host's checkout after an upgrade, so the
scheduler and the installed dependencies keep up.

## Many assistants, one repo

Each project is just a folder. `./gt use PATH` switches the default (stored in
`groundtruth.project`), `-p PATH` targets one for a single command, and the
Dockerfile's `PROJECT` build arg picks which one an image serves.
