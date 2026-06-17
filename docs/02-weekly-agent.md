# Pillar 2 — An agent that does weekly work

A knowledge base is only as good as its freshness. The hard part isn't running a
scraper — it's running an **autonomous, multi-step job on a schedule** that you
trust enough to leave alone. That means real state, real failure handling, and a
human in the loop at exactly one point: the merge.

`scripts/refresh_pipeline.sh` is that agent.

## What it does, in order

```
scrape → rebuild facts → reindex → recompile stale prose → gate → eval → draft PR
```

1. **Scrape** upstream sources into `sources/` (runs `scripts/scrape.py` if you
   provide one; otherwise treats the committed `sources/` as the evidence layer).
2. **Rebuild** `db/groundtruth.db` and the search index — deterministic, no LLM.
3. **Recompile** only the *stale* wiki pages (`recompile.py`), the one LLM step.
4. **Gate**: `check_wiki.py`, `lint_no_hardfacts.py`, the unit tests.
5. **Eval**: the Golden Set (tier chosen automatically — see below).
6. **PR**: push the branch and open a draft PR with a diff summary.

## Multi-step, autonomous, on a schedule

**Tool use.** The agent orchestrates real tools: a crawler, the deterministic
projector, an LLM compiler, linters, the eval harness, and `git`/`gh`. Each is a
checkable step with its own success criteria.

**State lives in git.** All work happens on a `refresh/<date>` branch. The branch
*is* the state: what changed, in what order, with what message. There is no
hidden scratch state to corrupt.

**Scheduling.** On macOS, `scripts/com.groundtruth.refresh.plist` runs it weekly
via launchd:

```bash
cp scripts/com.groundtruth.refresh.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.groundtruth.refresh.plist
```

On Linux, cron:

```cron
30 3 * * 1  cd /path/to/groundtruth && ./scripts/refresh_pipeline.sh --write --open-pr >> logs/refresh/cron.log 2>&1
```

…or a systemd timer if you want logging and dependency control.

## Failure modes (the part people skip)

| situation | behavior |
|---|---|
| worktree dirty at start | **refuse** — write-mode requires a clean tree |
| a gate or eval fails | **stop on the `refresh/<date>` branch** so a human can inspect exactly what the agent saw; do not return to main, do not open a PR |
| scrape changed nothing | **no-op**: no commits, no branch push, no PR, **$0** in API spend |
| success | commit, open a draft PR, **return to the original branch** |
| release evals requested but no API key | **fail loudly** rather than silently skipping the gate |

The dirty-tree refusal and the "stop on the branch" rule are what make it safe to
run unattended: the agent never destroys context, and a bad night leaves a
clean trail for a human to inspect.

## Idempotency & cost control

Two properties keep an always-on agent cheap and calm:

- **Change-gated.** The whole expensive tail (recompile, release evals, PR) only
  runs when scraping actually produced an upstream change. Most nights nothing
  changed, so the agent does nothing and spends nothing.
- **Auto-escalating evals.** The free deterministic replay (`--tier fast`) runs
  every time. The paid release-tier Golden Set runs *only* when the run committed
  content — so you pay for a full eval exactly when there's something new to
  validate, and never just to confirm "still fine."

## Try it

```bash
./scripts/refresh_pipeline.sh        # dry run — audits stale pages, runs every gate, writes nothing
```

On a fresh clone this reports no stale pages and green gates, then exits — the
no-op path, which is most real nights.
