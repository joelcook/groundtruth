# Troubleshooting

Start with `./gt doctor`. It checks Python, dependencies, your project, and
optional tools, and tells you what to run.

## Setup

**`./gt: Permission denied`**: run `chmod +x gt`, or use `python3 gt ...`.

**"needs Python 3.10 or newer"**: install a newer Python from python.org and run
it explicitly: `python3.12 gt demo`. The `.venv/` it creates remembers it.

**Windows**: use `py gt demo`. Everything else is the same.

**Something's half-installed**: `rm -rf .venv && ./gt demo` rebuilds from scratch.

**"No module named 'mcp.server.fastmcp'"**: you have the `mcp` 2.x SDK.
groundtruth uses 1.x (`mcp>=1.27,<2`). Run `./gt setup`.

## Facts

**"field 'Suite 200' must be letters, numbers, underscores (a comma inside
{braces}...)"**: an unquoted comma in a `{...}` row. Quote the value:
`{address: "1 Main St, Suite 200"}`.

**A phone number shows up as a number, or loses its leading zero**: quote it:
`phone: "555-0100"`, `zip: "02134"`.

**"can't use 'limit' as a parameter name"**: `limit` and `upcoming_only` are
reserved. Use `arg: name` (or anything else).

**I added a tool and the client doesn't see it**: run `./gt check`; the server
only picks up changes that pass it (a failing edit is rejected and shows in
`/healthz`). Over HTTP, new tools and prompt edits then reach the next session
on their own. A stdio client (Claude Desktop, Cursor...) reads the tool list
when it starts: restart the client.

**A lookup returns the wrong row**: add `aliases:` to the right row, or narrow
`match:` to the fields that should be searched. `./gt ask --offline "..."` shows
what each tool returns.

**"sources/pages/x.md changed since the last review"**: the evidence behind
some facts or pages changed. `./gt review` lists them; `./gt review --diff` shows the change;
fix what's stale, then `./gt review --done`.

**"was removed upstream but is still cited"**: `./gt ingest` noticed the document
is gone (deleted from its folder, or a 404). The evidence is kept and flagged.
Update or remove the facts and pages that cite it, then `./gt review --done`.

**"Refusing to serve content that hasn't passed the checks"**: fix what `./gt
check` reports. `./gt serve --force` serves anyway, for local testing only.

**"template examples are still live"**: the template's fictional rows and pages
are still being served. Replace them, or `./gt examples --remove`.

## Wiki and evals

**"date 'June 20, 2026' in prose"** (or duration, percentage, phone number...):
move the value to `facts/` and refer to the tool, or justify it with
`<!-- fact-ok: why -->` on the line above.

**"no sources.lock.yaml"**: nothing records which version of each source your
facts and pages were checked against, so a changed source couldn't be caught.
Check your facts against `sources/pages/`, then run `./gt review --done`.
(`./gt init` creates the lock for you; you'll only see this on a project made by
hand.)

**"missing or invalid YAML frontmatter"**: usually a value with a colon in it.
Quote it: `title: "Pricing: plans and limits"`.

**"the prompt mentions `get_x`, which isn't a tool"**: rename or remove it in
`prompts/system_prompt.md`. Using `{{FACT_TOOLS}}` there keeps the routing table
in sync automatically.

**"search_wiki doesn't surface 'x' for this question"**: put the words people use
in the page's `title` and `tags`, or add a synonym in `groundtruth.yaml`.

**Live evals fail with "no Anthropic credentials"**: set `ANTHROPIC_API_KEY` or run
`ant auth login`. The free `./gt eval` needs neither.

## Server

**401 unauthorized**: the client isn't sending the key. Use
`Authorization: Bearer <GT_MCP_API_KEY>` (or `x-api-key: <key>`).

**421 / "Invalid Host header" after deploying**: the server has no API key, so it
only accepts localhost by default. Set `GT_ALLOWED_HOSTS=your.host`, or set
`GT_MCP_API_KEY`.

**Port 8765 is busy**: `./gt serve --port 8790`.

**Claude Desktop shows the server as failed**: first run `./gt check`; the
server refuses to start on a project that fails it. Still failing? Run the exact
command from `./gt connect claude-desktop` in a terminal. It should sit silently
waiting for input (that's stdio). Any error it prints is the problem.

## The refresh host

**"standing down: main names X as the refresh host"**: on purpose. Only the
machine named by `host:` in main's `groundtruth.yaml` refreshes and pushes. To
move it, change that line on main, then `./gt host install` on the new machine
and `./gt host uninstall` on the old one.

**"Claude Code needs a login on the refresh host"**: the login behind
`./gt rewrite` expired. On the host, run `claude setup-token` and put the token
in the checkout's `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`. Rewrites resume on
the next run. Never commit `.env`.

**"a wiki page would not rewrite"**: the page failed its checks on every model.
The alert names the first problem: an unsupported claim, a fact in prose, a
broken link. `logs/host/rewrite/` on the host has each attempt. Fix the page by
hand (or the source upstream), or wait: it retries in seven days, or sooner if
its sources change.

**"facts cite sources that changed"**: a source behind your facts was
re-captured with new content. The host never reviews facts on its own, so
`./gt check` stays red on main until you do: `./gt review --diff`, fix the
facts, `./gt review --done`. Then delete the alert's line from `ALERTS.md`.

**Rewrites have stopped, with no alert**: they may be waiting out a usage
window. `logs/host/rewrite.log` says "paused until ..." when that's it. Lower
`GT_REWRITE_MAX_UTILIZATION` to leave more headroom, or raise it to use more.

**Nothing has happened since I installed it**: check the crontab with
`crontab -l`, and that cron can run `./gt` (`./gt host check`). Each job writes
its log to `logs/host/` in the checkout.
