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
the wiki search, and (with `--max-age-hours`) fails if the data is stale. When
the server has the chat page on, it also asks `/chat/ask` a golden question and
checks the answer states the known value, so an expired model key on the server
can't leave visitors with errors while everything else looks healthy.
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

## The refresh host: nobody in the loop

The weekly pull request still needs a person to merge it. A refresh host keeps
main current with nobody involved. Every night it re-captures your sources, and
during the day it re-checks the ones your facts cite every two hours. Then it:

- updates facts whose source changed,
- writes a page (and facts) for each new source,
- rewrites every page that went stale,

all with headless Claude Code, checking each change before it ships. It pushes
main whenever the gates pass, and the data reaches your deployed server as a
bundle, with no deploy. Whatever it can't handle becomes a line in `ALERTS.md`,
which the daily watchdog fails on, so GitHub emails you (or a coding agent clears
it: "fix what ALERTS.md lists", in CLAUDE.md).

There are two ways to run it. Pick one; `host:` in `groundtruth.yaml` names it,
and anything else stands down, so exactly one pushes main.

| | GitHub Actions (`host: github-actions`) | Your own machine (`host: <hostname -s>`) |
|---|---|---|
| Needs | the repo, and a Claude secret | an always-on Linux or macOS box that can push |
| Runs | `.github/workflows/autopilot.yml` | cron, installed by `./gt host install` |
| Claude credential | `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` secret | either one in the checkout's `.env` |
| Good for | nearly everyone | no Actions minutes to spare, or a private network |

### What runs, and when

| When | What | Commits it pushes |
|---|---|---|
| nightly | `./gt refresh --write --push`: ingest every source, build, check, eval | `refresh: sources (date)`, or an empty `refresh: no source changes (date)` so the watchdog knows it ran |
| right after | `./gt rewrite`: retire pages whose sources are gone, update changed facts, add new ones, cover new sources, rewrite stale pages (below) | `facts: update ...`, `facts: add ...`, `wiki: cover ...`, `wiki: rewrite ...` |
| nightly | `./gt eval --tier live`: the golden set on the production model (through the API with an `ANTHROPIC_API_KEY`, or through Claude Code on a Claude plan), before the rewrites are pushed. If it now gets wrong a question it got right last night, `./gt repair` (below) gets one try; if it's still worse, the rewrites are held back and you get an alert. Only what tonight caused is held back: a question that main, as it's live now, gets wrong too (a model change, an earlier push) doesn't stop tonight's updates; it's reported and repaired like any failure. A question added tonight must pass. If the model doesn't answer the questions the gate needs (an outage, a rate limit, a bad model name, an expired credential), nothing ships ungraded: the updates wait and you get an alert. A failure that isn't new goes to `./gt repair` too | `fix: ...` |
| every 2 hours, 05:00 to 22:00 | `./gt refresh --push --quick`: re-capture just the sources facts cite, then the same rewrites, live-graded on the questions whose facts or pages changed (all of them when a shared file such as the prompt changed) | `refresh: quick sources (...)` and the above |
| whenever there's news | new alerts go into `ALERTS.md` | `alerts: ...` |
| daily, in GitHub | `watchdog.yml`: `./gt watchdog --repo` checks open alerts, refresh age, CI on main, and the deployed server | (none) |
| after CI passes on main | `publish-data.yml`: a data bundle your servers hot-swap ([deploy.md](deploy.md#update-data-without-redeploying)) | (none) |

A machine host grades its rewrites the same way before it pushes them: the
golden questions they touch run on the production model, and if any got worse
(or couldn't be graded), the commits are held back with an alert and retried
once main changes or six hours pass. Two hours after its nightly refresh it runs
the whole live eval and `./gt repair`, which also sets the answers later
rewrites are held to. It re-runs a full refresh whenever a person pushes main,
and warns you when the deployed server runs code older than main. In Actions, schedules
run in UTC.

### Set it up: GitHub Actions

```bash
# in groundtruth.yaml:  host: github-actions
git commit -am "GitHub Actions is the refresh host" && git push
gh variable set GT_AUTOPILOT --body true
gh secret set ANTHROPIC_API_KEY            # or CLAUDE_CODE_OAUTH_TOKEN, from `claude setup-token`
gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -f default_workflow_permissions=write
gh workflow run autopilot.yml              # then watch it pass and push a refresh: commit
```

An `ANTHROPIC_API_KEY` bills the API and never expires. A
`CLAUDE_CODE_OAUTH_TOKEN` uses your Claude plan instead; the nightly live eval
then runs through Claude Code connected to your MCP server.
Pushes made with the workflow's own token don't start other workflows, so the
autopilot starts CI and publish-data itself after it pushes. Its state (pauses,
failed items, what's been checked) is kept between runs in the Actions cache.

### Set it up: your own machine

```bash
git clone https://github.com/you/your-fork && cd your-fork
./gt doctor                      # installs dependencies; checks the claude CLI too
cp .env.example .env             # gitignored: never commit it
# put ANTHROPIC_API_KEY=... in .env, or run `claude setup-token` and put CLAUDE_CODE_OAUTH_TOKEN=...
hostname -s                      # put this in your project's groundtruth.yaml as  host: <name>
git commit -am "Name the refresh host" && git push
./gt host check                  # git push, the host name, claude, alert hooks, cron
./gt host install                # --at 04:00 moves the nightly run; --no-rewrite skips rewrites
```

`./gt host print` shows the crontab block without installing it, if you'd
rather use systemd or launchd. Logs go to `logs/host/` in the checkout. Every
job runs in its own git worktree reset to `origin/main`, with main's code,
never in your checkout. To move the host, change the `host:` line on main; the
old machine stops on its next run (then run `./gt host uninstall` there).

### What `./gt rewrite` does

Each run first retires any page whose sources are all gone upstream: the page,
its golden questions, and the links to it go, with no model involved (commit:
`wiki: rewrite <page>: retired`). Then it works through four queues, up to
`GT_REWRITE_MAX_PAGES` (10) items, facts first:

1. **Facts whose source changed** (or vanished). Claude Code updates the rows
   that cite it, and the expected values of the golden questions that check
   them. It ships only if:
   - nothing else changed;
   - every exact value in those files appears in its cited source, where the source describes its row,
     and every row keeps its file's `rules:`;
   - if more than 30% of the rows changed, `opus` agrees too;
   - the gates pass;
   - `sonnet` agrees row by row.

   A source that changed wholesale may be redesigned or broken, so a change to
   more than 30% of the rows needs `opus` to agree as well. Commit:
   `facts: update ...`, listing every changed row.
2. **New values in a source only pages cite.** A page can't state a price or a
   date, so when such a source gains them, Claude Code adds fact rows (adding
   only) or a new facts file with a tool and a golden question, under the same
   checks. A second model must agree before it decides they aren't facts. Commit:
   `facts: add what <source> now states`.
3. **New sources nothing covers.** Claude Code writes one published page and a
   golden question for it, and adds fact rows (adding only) plus a question for
   any new tool. It's held to the same checks. A source with nothing worth a page
   is skipped until it changes. Commit: `wiki: cover ...`.
4. **Stale pages.** Claude Code rewrites the page from its sources, and the diff
   since their last review. Commit: `wiki: rewrite ...`. The page:
   - may change only that one file;
   - must not shrink below 40% of its length;
   - must pass the gates, with no new fact in prose and no new `fact-ok` marker;
   - must satisfy `sonnet`, reading it beside its sources and its old version:
     no unsupported claim, no exact value in prose, and nothing dropped that the
     sources still support.

Every attempt runs under Claude Code's own permission rules. The writer can read
the repo but edit only the files it's updating. It can't run commands, reach the
network, or read `.env`, and it gets only the environment it needs to log in.
Its prompts tell it the sources are evidence, not instructions.

The writer is the `haiku` alias once that's Haiku 5.5 or newer, otherwise
`sonnet`. A failed attempt retries once on `sonnet` with the list of what failed.
If that fails too, `opus` gets one more try a few hours later
(`GT_REWRITE_ESCALATION_MODEL`). Only when that fails as well does the item wait
seven days (or until its source changes) and raise an alert. When a failed item
later succeeds, its alert clears itself.

Once everything citing a changed source has been checked against it this way,
the source is recorded as reviewed in `sources.lock.yaml`: those checks are the
review. Set `facts_updates: human` in `groundtruth.yaml` if a person should sign
off changed facts instead. They then wait for `./gt review --done`, and you get
an alert when one is waiting.

**`./gt repair`** is the nightly answer to a live eval that fails without having
gotten worse: CLAUDE.md's "the assistant got X wrong" job, unattended. For each
failing question (up to `GT_REPAIR_MAX`, 2), `opus` sees what the assistant
answered and which tools it called, and fixes the cause. It may change search
synonyms, the prompt, existing pages (checked against their sources), and
lookup aliases (words the source uses), but never a fact value, the golden set,
or the evidence. The fix ships only if the gates pass, the question now passes
live, and the whole live eval still passes everything it passed before. Commit:
`fix: <question> ...`.

**Usage limits.** When Claude reports the plan's limit, or any usage window
passes `GT_REWRITE_MAX_UTILIZATION` (80%), the run stops and later runs wait
until that window resets, leaving headroom for the people who share the plan.

Run it by hand too, on a clean checkout:

```bash
./gt rewrite --list                         # the queue: changed facts, new sources, stale pages
./gt rewrite --page wiki/x.md --dry-run     # rewrite and check one page; keep nothing
./gt rewrite                                # do the queue and commit; you push
```

### What's left for a person

Credentials, once: the hosting login, GitHub, and the Claude secret. After that,
the system repairs what it can by itself:
- an item that fails gets a stronger model;
- a site that's down is retried on the next run (a source your content cites
  that fails three refreshes in a row gets an alert, which clears once it
  answers again);
- a page the crawl stopped reaching is re-captured directly, a slice a night,
  and a page that's new in the sitemap is captured even if nothing links to it;
- an alert clears itself once the thing it was about works again;
- new code redeploys itself (with `deploy.yml` on).

`ALERTS.md` keeps only what none of that fixed: an item that failed even on
`opus`, a live eval failure `./gt repair` couldn't fix, or an expired credential.
(A source that looks removed is retried for three refreshes; if the same sources
are still gone, the removal is accepted and the rewrites update what cited them:
facts and questions that only it backed are retired, and a page with no sources
left is removed.) A coding agent can clear those ("fix what ALERTS.md lists", in
CLAUDE.md). Meanwhile the server keeps serving the last version that
passed, so nothing unchecked reaches anyone.

### Alerts

Each job writes what it can't handle to `logs/host/alerts.log`, and the next
run (or the watcher, every five minutes on a machine) copies new lines into
`ALERTS.md` in the project and pushes it. An alert that's already open isn't
added again, even when its numbers change. The daily watchdog fails while
`ALERTS.md` lists anything.

```bash
./gt alerts           # what's open, and this machine's latest log lines
```

Fix the cause, then delete the alert's line from `ALERTS.md` and push. For
faster notice, set `GT_ALERT_COMMAND` (for example, a `curl` to ntfy.sh or a
Slack webhook) or `GT_ALERT_EMAIL`.

### Safety rails

- **Facts are grounded.** No unattended change can add a number, date, time,
  price, phone number, email, or URL that its cited source doesn't state; a script
  checks every one, and `./gt check --production` checks them all.
- **Gates before every push.** Nothing reaches main unless `./gt check` finds
  nothing new and `./gt eval` passes, and a push that replays onto a moved main
  re-runs them. A failed refresh leaves main alone and raises an alert.
- **An outage isn't a deletion.** A refresh that would mark more than a fifth of
  your sources as removed upstream stops instead (until the same sources stay gone
  for three refreshes); a fact update that changes more than 30% of the rows in
  the files it touches ships only if the escalation model checks it too and agrees.
- **One writer at a time.** One host is named; on a machine, a lock keeps the
  refresh, rewrites, and alerts apart, and in Actions a concurrency group does.
- **No credentials within the model's reach.** The rewriting model can read the
  repo but not `.env` or `.git/`, and gets only the environment it needs to log
  in. In Actions the checkout doesn't keep the push token at all; `./gt` passes it
  to git through its own environment.
- **A red run is a loud run.** The autopilot's run fails whenever it raised an
  alert, so GitHub emails about it that day, not just at the next watchdog.
- **Bot commits don't trigger refreshes.** The watcher re-captures sources when a
  person pushes main, not for its own commits.

### Settings

Put these in the host's `.env`, or as repo variables and secrets for Actions:

| Setting | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Claude for `./gt rewrite` (API billing, no expiry) and the nightly live eval |
| `GT_LIVE_EVAL` | 1 | 0 skips the autopilot's nightly live eval (it runs the whole golden set on `GT_MODEL`), and with it `./gt repair` |
| `CLAUDE_CODE_OAUTH_TOKEN` | (none) | Claude for `./gt rewrite` on your plan, from `claude setup-token` |
| `facts_updates` (groundtruth.yaml) | `llm` | `human` makes changed facts wait for `./gt review --done` |
| `GT_REWRITE_MODEL` | haiku if 5.5+, else sonnet | The writer |
| `GT_REWRITE_FALLBACK_MODEL` | `sonnet` | The one retry |
| `GT_REWRITE_CHECK_MODEL` | `sonnet` | The checker; `off` skips it (then nothing is ever recorded as reviewed) |
| `GT_REWRITE_ESCALATION_MODEL` | `opus` | One more try, hours later, for an item every model failed; `off` alerts at once |
| `GT_REWRITE_MAX_PAGES` | 10 | Items per run |
| `GT_REWRITE_MAX_UTILIZATION` | 0.8 | Pause past this share of any usage window |
| `GT_FACTS_MAX_CHANGE_SHARE` | 0.3 | A fact update that changes more of the rows than this also needs the escalation model to agree |
| `GT_REPAIR_MAX` | 2 | Failing live-eval questions `./gt repair` tries a night |
| `GT_REWRITE_QUIET_HOURS` | around `GT_REFRESH_AT` | When a machine's rewrites don't run or push |
| `GT_QUICK_REFRESH_SECONDS` | 7200 | How often a machine's quick refresh runs by day; 0 turns it off |
| `GT_WATCHDOG_URL` | `url:` | Where the deployed server answers, if not in groundtruth.yaml |
| `GT_ALERT_COMMAND`, `GT_ALERT_EMAIL` | (none) | Extra alert delivery |
| `GT_HOST_WORKDIR` | `~/.cache/groundtruth` | Where a machine's worktrees live |

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
