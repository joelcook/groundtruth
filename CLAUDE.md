# Working in groundtruth (for coding agents)

This file tells an agent (Claude Code, Codex, Cursor...) how to work in this
repo. `AGENTS.md` is the same file. Follow it exactly.

The goal is an assistant for an organization that a coding agent builds and
launches, and that then keeps itself current with no person in the loop. Your
job is to get there. Decide what you can decide; ask the person only for
credentials (logins and secrets), and ask for all of them at once.

## What this repo is

A framework for grounding an LLM assistant in a body of knowledge:

- **Code** lives in `groundtruth/` (the Python package) and `./gt` (the CLI).
  You rarely need to touch it to build an assistant.
- **Projects** are folders with a `groundtruth.yaml`. Each one is a separate
  assistant. `examples/library/` is the demo; `templates/` are starting points;
  the active project is named in `groundtruth.project` (`./gt status` shows it).

Inside a project:

| Path | Holds | Rules |
|---|---|---|
| `facts/*.yaml` | Exact facts (prices, dates, hours, contacts, limits) and the tools that serve them | Every row cites a captured source that states its values. Format: `docs/facts.md` |
| `wiki/**/*.md` | Explanations (how-to, policy, "what is") | Frontmatter + `## Sources & Lineage`. **No exact facts in prose.** Format: `docs/wiki.md` |
| `sources/pages/*.md` | Evidence, written by `./gt ingest` | Never edit by hand |
| `sources.lock.yaml` | Which version of each source the facts/wiki were last checked against | Only via `./gt review --done` or `./gt rewrite` |
| `prompts/system_prompt.md` | The assistant's instructions (what MCP clients receive) | Keep `{{FACT_TOOLS}}` in it: the routing table is generated from `facts/` |
| `evals/golden.yaml` | Questions with known answers | Format: `docs/evals.md` |
| `ALERTS.md` | What the unattended jobs couldn't handle | Delete a line only after you've fixed its cause |
| `build/`, `logs/`, `reports/` | Generated | Never edit, never commit |

## The one rule: facts come from tools

The assistant must never state a date, time, dollar amount, phone number, email,
hours, deadline, or limit from memory or from wiki prose. Those live in
`facts/*.yaml` and are served by tools. In wiki prose, name the tool instead:
"the assistant looks up the current fee with `get_fee`." If a wiki page and a
fact tool disagree, the tool is right and the page is stale.

**And every fact is grounded.** Every exact value in `facts/` (number, money,
date, time, phone number, email, URL) must appear in the captured source its row
cites. `./gt check` warns about any that don't; `./gt check --production` (what
the server, Docker, and data bundles use) fails on them. Treat a grounding
warning as an error: fix the value, or cite the source that states it.

## The gates: run before you say you're done

```bash
./gt check                # facts valid and grounded, sources reviewed, wiki structure, no facts in prose, golden set wired
./gt eval                 # free, no model: tools return the expected facts, search finds the pages
./gt check --production   # before shipping: also fails on template examples, ungrounded facts, untested tools
python -m unittest discover -s tests    # only if you changed code in groundtruth/
```

If `./gt check` says a source changed since the last review, run `./gt review
--diff`, compare the listed facts and pages with the change, fix what's stale,
and only then run `./gt review --done`. Never run `--done` without actually
re-checking. A source "removed upstream" means the document is gone: update or
remove whatever still cites it. (Once the project runs on a refresh host,
`./gt rewrite` does this for you; see below.)

Both `./gt` commands must pass. `./gt -p PATH ...` targets a specific project.
If you changed the prompt, tools, or content and an `ANTHROPIC_API_KEY` is
available, also run `./gt eval --tier live` and report the result.

## Common jobs

### "Here's my site / my documents. Build the assistant."

1. `./gt init NAME --template TEMPLATE` (pick the closest: `./gt init --list`).
2. `./gt ingest URL_OR_FOLDER` to capture the evidence. Put local documents in a
   folder inside the repo (e.g. `NAME/documents/`), so the refresh host can
   re-read them later.
3. `./gt compile` writes proposed facts per new source (`facts/_proposed-*.yaml`:
   one row per exact statement; the `_` keeps them offline), a draft page per
   new source in `wiki/drafts/` (`status: draft`: not served, not review-gated),
   and `NAME/build/compile-packet.md`,
   which lists the exact values it found in each source (all of them:
   `build/fact-candidates.md`). Do what the packet says:
   - Exact things become rows in `facts/*.yaml`. Start from the proposals:
     verify every row against its source, and reshape statements into real
     fields where you can (`price_usd: 39`; reuse a template file's shape), then
     rename `_proposed-x.yaml` to `x.yaml`. Give every tool a sharp `description`.
     Every value must appear in the cited source (`./gt check` says if one doesn't).
   - Explanations become wiki pages: finish the drafts (or write new pages), one
     topic per page, the words people use in `title` and `tags`, sources listed,
     lineage table at the bottom. Set `status: published` when a page is done.
   - Rewrite `prompts/system_prompt.md` for the organization (scope, tone, what
     to refuse and where to redirect). Take these from the organization's own
     pages; don't ask. Keep `{{FACT_TOOLS}}`: it lists the tools.
   - Replace the template's example questions in `evals/golden.yaml`: a
     `hard_fact` item (with `must_call_tool`, `tool_args`, and `must_match` or
     `must_contain`) per tool, a `narrative` item (`must_cite_page`) per page,
     `out_of_scope` items for the personal or account questions it must refuse,
     and a `contradiction` item wherever an old source and a current one disagree.
     Every tool needs at least one question (`--production` fails otherwise).
   - Remove the template's fictional examples (`example: true`) with
     `./gt examples --remove` once real content replaces them.
   - `./gt review --done` once facts and pages match the sources.
4. Run the gates. Fix everything. Then `./gt ask "..."` a few real questions.
   `./gt status` lists anything still unfinished, in order, with the command for each.
5. When `./gt status` says "Ready to launch", go on to the next job.

### "Take it to production" / "Keep it current without us"

Done looks like this, and `./gt status` checks what it can:
- the server runs in a container behind HTTPS, with an API key and a stamped
  build, and `url:` in `groundtruth.yaml` says where;
- every green push to main publishes a data bundle that the server installs
  within minutes, with no deploy;
- GitHub Actions is the refresh host (`host: github-actions`, `autopilot.yml`):
  it re-captures sources nightly (the ones facts cite, every two hours by day),
  updates changed facts, writes pages for new sources, rewrites stale pages, runs
  the live eval, and pushes main, all checked before anything ships;
- a daily watchdog fails, and GitHub emails the repo's owner, when any of that
  breaks or `ALERTS.md` lists something.

**Decide; don't ask.** Use these defaults unless the person already said
otherwise:
- **Hosting:** whichever platform's CLI is already logged in (check
  `fly auth whoami`, `gcloud config get-value account`, `az account show`,
  `railway whoami`). Recipes are in docs/deploy.md. If none is, ask for a Fly.io
  login (`fly auth login`): it's the shortest recipe.
- **Refresh host:** GitHub Actions. No machine to choose or keep running.
- **Client:** the server speaks MCP, so it works in Claude, ChatGPT, Copilot
  Studio, Cursor, and anything else that does. `./gt connect CLIENT` prints the
  setup for each; you don't need to pick one to launch.

**Ask once, for credentials only,** and all of them in one message: the hosting
login (if none is logged in), a GitHub login with admin rights on the repo (if
`gh auth status` shows none), and a Claude credential for the refresh host. Ask
for an `ANTHROPIC_API_KEY` (it doesn't expire, and it also runs the nightly live
eval), or a `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (the person runs
it; it's interactive). The person can type `! command` to run something here.
Never write a credential into a file, a commit, or your reply.

1. **Gates.** `./gt status` shows "Ready to launch"; `./gt check --production`
   and `./gt eval` pass.
2. **Their own repo.** The project lives in a repo they control (a fork or a
   copy of this one), pushed, with CI (`ci.yml`) green.
3. **Deploy** (docs/deploy.md). `docker build` builds the active project; pass
   `--build-arg GT_BUILD_COMMIT=$(git rev-parse HEAD)`. Make the server's key
   without ever showing it, and give it to the platform and to GitHub in the same
   step, e.g.:
   `KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))"); fly secrets set GT_MCP_API_KEY="$KEY"; gh secret set GT_MCP_API_KEY --body "$KEY"`.
   Add `url: https://HOST/mcp` to `groundtruth.yaml`, commit, and push.
   Verify: `GT_MCP_API_KEY=... ./gt watchdog --repo` passes, or have GitHub run it
   (`gh workflow run watchdog.yml`).
4. **Data without deploys.** `gh variable set GT_PUBLISH_DATA --body true`; set
   `GT_DATA_URL` on the server (docs/deploy.md; a private repo also needs
   `GT_DATA_TOKEN`, a read-only token for its releases). Verify: after the next
   push, `publish-data` succeeds and `./gt status` says the server has this
   checkout's data.
5. **Autopilot.** Put `host: github-actions` in `groundtruth.yaml`, commit, and
   push. Then:
   `gh variable set GT_AUTOPILOT --body true`,
   `gh secret set ANTHROPIC_API_KEY` (or `CLAUDE_CODE_OAUTH_TOKEN`), and let
   workflows push:
   `gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -f default_workflow_permissions=write`.
   Verify: `gh workflow run autopilot.yml`, wait, and check it passed and pushed a
   `refresh:` commit.
6. **Watchdog.** `gh workflow run watchdog.yml` passes.
7. **Connect users.** `./gt connect CLIENT` prints the setup (it uses `url:`).
   For Copilot Studio, paste `./gt prompt` into the agent's instructions and grade
   the model it runs there: `./gt eval --export copilot-studio` (docs/evals.md).

After that nobody needs to do anything. If the person has no GitHub Actions
minutes to spare, or wants the jobs on their own machine, `./gt host install` on
an always-on Linux or macOS box runs the same jobs (docs/operations.md).

### "Fix what ALERTS.md lists"

Each line is something the unattended jobs couldn't handle. Pull first. Fix the
cause, run the gates, and delete the line in the same commit:
- **a wiki page would not rewrite / a new source would not get a page:** the
  alert names the failed check. Write or fix the page yourself, following
  docs/wiki.md, from its sources.
- **facts would not update:** the alert names the value or check that failed.
  `./gt review --diff SOURCE` shows the change; update the rows so every value
  appears in the source, then `./gt review --done SOURCE`. If the source really
  dropped a fact, remove the row and its golden question's expected value.
- **the refresh failed / looks like an outage:** read the message. A site that's
  down needs nothing; the next run retries. A source that's really gone needs
  what cites it updated.
- **the live eval failed:** `./gt eval --tier live` shows which question. Fix the
  fact, the page, the synonyms, or the prompt (see "The assistant got X wrong").
- **Claude Code needs a login / pages wait for a rewrite that can't run:** the
  refresh host needs a working Claude credential (step 5 above).
- **the deployed server needs a redeploy:** rebuild and deploy the image (step 3).

### "Do the work in build/compile-packet.md"

Read the packet, do every item, run the gates, and summarize what you added and
anything in the sources you couldn't place.

### "The assistant got X wrong"

1. Add X to `evals/golden.yaml` first, and watch `./gt eval` fail (or, for
   model behavior, `./gt eval --tier live --only ID`).
2. Fix the cause upstream: a missing or wrong fact row, an alias, a page title,
   a synonym in `groundtruth.yaml`, or the prompt.
3. Watch it pass. Run the full gates.

### Working alongside the refresh host

If the project names a `host:` in `groundtruth.yaml`, the refresh host commits
to main on its own (docs/operations.md). Pull before you start. Its commits:
`refresh: ...` (re-captured sources), `facts: update ...` (facts it updated from
a changed source), `wiki: cover ...` (a page for a new source), `wiki: rewrite
...` (a stale page rewritten), and `alerts: ...`. Each one passed the gates and
a second model's check. Don't revert them; fix what's upstream instead.

`./gt rewrite` does by hand what the host does on a schedule: update facts,
cover new sources, rewrite stale pages. It needs a clean checkout (it reverts
every file but the ones it's updating) and the `claude` CLI. `./gt rewrite --list`
shows the queue without changing anything.

### Changing the code in `groundtruth/`

Keep it dependency-light and readable; every module starts with a docstring
explaining why it exists. Add a test in `tests/` for any behavior you change.
Don't add per-domain logic to the code: domains belong in project YAML.

## Don'ts

- Don't put facts in wiki prose (dates, money, durations, percentages, limits,
  phone numbers, emails). The checker knows the common formats, not every one:
  the rule applies to what it misses too. Don't use `<!-- fact-ok -->` to dodge
  it; it's for genuinely stable, illustrative values, with a reason.
- Don't write a fact value its cited source doesn't state, even one you're sure
  of. If the evidence doesn't say it, the fact doesn't exist yet.
- Don't run `./gt review --done` to make a check go green. It records a
  judgment: "I compared the facts and pages with the changed source and they're
  right." Nothing verifies that judgment for you.
- Don't edit `sources/`; re-run `./gt ingest`.
- Don't commit `build/`, `logs/`, `reports/`, or `evals/runs/*-tools.json`.
- Don't put a key or token in a tracked file (`ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `GT_MCP_API_KEY`...). They go in `.env` (gitignored)
  or in GitHub secrets; `.env.example` holds empty placeholders only.
- Don't ask the person to make a decision this file gives you a default for.
