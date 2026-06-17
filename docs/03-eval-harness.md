# Pillar 3 — An eval harness for an AI system

This is the most undervalued skill in applied AI. Almost nobody builds a real
eval harness, which means almost everyone ships on gut feel and finds out about
regressions from users. An eval set is the artifact that turns *"the bot seems
fine"* into a number you can defend, gate releases on, and watch over time.

`evals/golden.yaml` + `groundtruth/evals.py` are that artifact here.

## Grade the path, not just the prose

The defining idea: for a hard fact, **the right answer for the wrong reason is a
failure.** If you ask "how much is printing?" and the model replies "$0.10" from
its training data instead of calling `get_fee`, the text looks perfect — and it
will be wrong the moment the price changes. So the `hard_fact` grader asserts the
**tool call** (`must_call_tool: get_fee`), not just the substring. You are
testing the *retrieval path*, which is the thing that has to keep working.

This is why the harness runs the question through a real agent loop with the
exact MCP tool surface the production server exposes (derived from the server, so
there's one source of truth) — not a mock.

## Four categories, four failure modes

| category | asserts | catches |
|---|---|---|
| `hard_fact` | `must_call_tool` (+ `must_contain` / `must_not_contain`) | facts answered from memory instead of a tool |
| `narrative` | LLM judge vs. `expected_summary` (offline: required page read) | unhelpful or off-rubric explanations |
| `out_of_scope` | `must_not_call_tool` + a refusal substring | pretending to know personal data; wasted tool calls |
| `contradiction` | cite the current source; `must_not_contain` the stale fact | repeating retired info when sources disagree |

## Tiers

| tier | what runs | cost | when |
|---|---|---|---|
| `fast` | replay the latest recorded run; deterministic checks only | $0, no key | every commit / nightly |
| `full` | live model + LLM judge | API spend | before merge, on changes |
| `release` | `full`, whole set, **100% required** | API spend | the ship gate |

```bash
python scripts/run_evals.py --tier fast        # offline, deterministic
python scripts/run_evals.py --tier release     # live; must be 100% to ship
```

`fast` works on a fresh clone because a recorded baseline run is committed; it
re-grades the recorded transcripts with the deterministic checkers. Run `--tier
full` once with a key and the newest real run becomes the replay + regression
source.

## Regression tracking

Each run writes an artifact to `evals/runs/`. The runner diffs against the last
model run and prints any **regressions** (was passing, now failing). A regression
in the Golden Set is a release blocker — that's the whole point of having one.

## The judge, and the model-mirrors-production rule

- Narrative answers are graded by an LLM judge (`EVAL_JUDGE_MODEL`, a strict
  separate model) against a rubric, with deterministic checks (required page
  read, forbidden substrings) gating it first so the judge is only asked when the
  cheap checks pass.
- `EVAL_MODEL` **must be the model you run in production.** If you eval on a
  different model than your users talk to, your green dashboard is measuring a
  chatbot that doesn't exist.

## How to grow it (the actual discipline)

1. Seed ~10 questions you know the answer to cold — at least one per category.
2. Ship. Watch real conversations (this is what `groundtruth/observability.py`
   logs are for).
3. Every time the bot fumbles, add that exact question as a new row, with the
   assertion that would have caught it. Fix the system until it passes.
4. Never delete a row to make the number go up.

A Golden Set that grows from real misses becomes, within weeks, the single most
valuable file in the repo: an executable specification of what "correct" means
for your assistant.
