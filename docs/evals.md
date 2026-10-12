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
  must_match: {value: 30, unit: days}   # free tier: some result row has exactly these field values
  must_not_contain: ["60"]              # optional
  must_cite_page: refunds-and-cancellation   # optional: the wiki page that must be read/found
  expected_summary: >                   # narrative only: the rubric the live judge grades against
    ...
  expected_response: "..."              # optional: what a right answer says, for --export (below)
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
  result and `must_not_contain` isn't. A plain number compares by value against
  the numbers in the result: `"0.10"` matches `0.1`, and `"9"` does not match `49`;
- checks `must_match` (field: value pairs) against the result rows, which is the
  most precise check for prices and limits;
- runs `search_wiki` on the question and checks `must_cite_page` comes back in
  the top five;
- for narrative questions, checks the page actually mentions `must_contain`;
- skips `out_of_scope` (that's model behavior; live covers it).

So it catches the regressions that matter most, wrong or missing data and
pages that stopped being findable, without spending a cent.

**live** runs each question through Claude with the exact tools your server
exposes and the system prompt in `prompts/system_prompt.md`, then grades the
tool calls and the answer. It uses exactly the instructions MCP clients get
(`./gt prompt`). Narrative answers also go to an LLM judge (`--no-judge` to
skip). Each live run is saved in `evals/runs/` (commit a good one as your
baseline; `replay` re-grades it for free and skips questions added since), and
regressions since the last live run are called out.

No API key, but the `claude` CLI is logged in (a Claude plan, or
`CLAUDE_CODE_OAUTH_TOKEN`)? Then live runs through headless Claude Code,
connected to your project's MCP server over stdio with the server's
instructions and no other tools, so it reaches the facts exactly the way a
client does. The judge runs the same way.

A live answer also fails if it states an exact value (a price, date, time,
duration, percentage, limit, phone number, or email) that no tool returned: that
value came from memory. Values are matched by kind against the rows and pages the
tools returned, not the query they echo: a `window_days: 30` doesn't back up
"$30". Only a `contradiction` question may repeat a value from the question (to
correct the rumor it asks about). The `/chat` page runs the same check on every
answer before a visitor sees it.

Models: `GT_MODEL` (default `claude-opus-5-5`) and `GT_JUDGE_MODEL`. **Evaluate
on the model you run in production**, or you're grading a different assistant
than your users talk to.

## Grading a model that isn't Claude

If your users talk to the assistant through Copilot Studio, the model they get
is whatever that agent runs, not the one `--tier live` grades. Grade that one
where it runs:

```bash
./gt eval --export copilot-studio     # build/copilot-studio-testset.csv
```

In Copilot Studio, open your agent's evaluation, create a test set, and upload
the file. Each question becomes a row graded by meaning against what a right
answer does. That comes from the question's `expected_response` if it has one,
otherwise from its `expected_summary`, `must_contain`, and `must_not_contain`.
Copilot Studio can't import tool-use checks, so keep `./gt eval` as the gate
for "did it look the fact up". It also takes at most 100 questions per test
set. Re-export whenever you change `golden.yaml`.

Its judge can mark a correct refusal ("I can't see your account; check the
portal") as unanswered. Review those against the question by hand rather than
weakening the refusal to raise the score.

## Growing the set

1. Start with about ten questions you know cold, covering each category.
2. After launch, `./gt report` lists the **gaps**: searches and lookups that
   found nothing. Each is a question to add, after you add the fact or page.
3. Every time the assistant gets something wrong, add that question before you
   fix it. Watch it fail, fix it, watch it pass.

Real sets run 50 to 200 questions. `./gt check` verifies every question points at
a real tool, real parameters, and a real page.
