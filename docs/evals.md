# Evals

`evals/golden.yaml` is a list of questions with the behavior each must produce.
It turns "the bot seems fine" into a number you can defend, and it's what keeps
a fixed problem fixed.

## A question

```yaml
- id: refund-window                     # unique, stable
  question: "How long do I have to ask for a refund?"
  category: hard_fact                   # hard_fact | narrative | out_of_scope | contradiction
  must_call_tool: get_limit             # the answer must come from this tool
  tool_args: {name: refund window}      # what the free tier calls it with
  must_contain: ["30"]                  # must appear in the answer (and, free tier, in the tool result)
  must_not_contain: ["60"]              # optional
  must_cite_page: refunds-and-cancellation   # optional: the wiki page that must be read/found
  expected_summary: >                   # narrative only: the rubric the live judge grades against
    ...
  notes: "why this question exists"     # optional, for humans
```

## The four categories

| Category | Tests | Pass means |
|---|---|---|
| `hard_fact` | The model looks facts up | It called `must_call_tool`, and the answer has `must_contain`. A right number from memory still fails: next month it won't be right. |
| `narrative` | The model finds the right explanation | It read `must_cite_page`; live, a judge grades it against `expected_summary`. |
| `out_of_scope` | The model refuses what it can't know | No tool calls (`must_not_call_tool: any`), and the answer redirects (`must_contain: ["account"]`). |
| `contradiction` | An old source and a new one disagree | It uses the current fact and never says the retired one (`must_not_contain`). |

## The tiers

```bash
./gt eval                    # tools tier (default): free, no model, seconds
./gt eval --tier live        # Claude + your real tools (needs ANTHROPIC_API_KEY)
./gt eval --tier release     # live, whole set, 100% required
./gt eval --tier replay      # re-grade the last live run (free)
./gt eval --only refund-window
```

**tools** (free) is the CI gate. For each question it:
- calls `must_call_tool` with `tool_args` and checks `must_contain` is in the
  result (numbers compare by value, so `"0.10"` matches `0.1`) and
  `must_not_contain` isn't;
- runs `search_wiki` on the question and checks `must_cite_page` comes back in
  the top five;
- for narrative questions, checks the page actually mentions `must_contain`;
- skips `out_of_scope` (that's model behavior; live covers it).

So it catches the regressions that matter most, wrong or missing data and
pages that stopped being findable, without spending a cent.

**live** runs each question through Claude with the exact tools your server
exposes and the system prompt in `prompts/system_prompt.md`, then grades the
tool calls and the answer. Narrative answers also go to an LLM judge (`--no-judge`
to skip). Each live run is saved in `evals/runs/` (commit a good one as your
baseline; `replay` re-grades it for free), and regressions since the last live
run are called out.

Models: `GT_MODEL` (default `claude-opus-5-5`) and `GT_JUDGE_MODEL`. **Evaluate
on the model you run in production**, or you're grading a different assistant
than your users talk to.

## Growing the set

1. Start with about ten questions you know cold, covering each category.
2. After launch, `./gt report` lists the **gaps**: searches and lookups that
   found nothing. Each is a question to add, after you add the fact or page.
3. Every time the assistant gets something wrong, add that question before you
   fix it. Watch it fail, fix it, watch it pass.

Real sets run 50 to 200 questions. `./gt check` verifies every question points at
a real tool, real parameters, and a real page.
