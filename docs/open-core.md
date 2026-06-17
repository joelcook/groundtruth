# Open core: what's free, what's paid, and why

groundtruth is open on purpose. This page draws the line between the **open core**
(this repo, MIT, free forever) and the **commercial layer** (services + accelerators).
If you're evaluating whether to build it yourself or hire help, this tells you
exactly what you get for free and where paying buys you months.

## The principle

> **If copying it costs you nothing and helps the project get adopted, it's open.
> If it compresses months of expert, domain-specific work into a download, it's paid.**

The architecture was never the moat — it's derivative of public ideas (Karpathy's
LLM-wiki, MCP, the facts-vs-narrative split). The scarce things are *execution,
judgment, a curated corpus, a real eval set, and keeping it correct over time.*
The repo is the funnel; the scarce work is the business.

## What's open (this repo — MIT, forever)

The whole **engine** and a working example. Enough to learn the pattern, run it
end to end, and build your own if you have the time and the chops.

| Open | Where |
|---|---|
| The framework: facts DB, two-tier search, MCP server, observability | `groundtruth/` |
| The embedding store **interface** + an offline reference embedder | `groundtruth/embeddings.py` |
| The eval **harness machinery** (graders, tiers, regression tracking) | `groundtruth/evals.py` |
| The runnable public-library example + a 15-item Golden Set | `wiki/`, `db/`, `evals/golden.yaml` |
| The refresh-pipeline **skeleton** + generic linters | `scripts/` |
| All concept docs and the "bring your own data" playbook | `docs/`, `CLAUDE.md` |

**Trust commitment:** the open core stays open. No rug-pulls, no relicensing of
what's already MIT, no crippling the free path to upsell. Adoption *is* the
marketing, so kneecapping it would be self-defeating.

## What's paid

### Services — the core of the business

| Offer | The pain it removes | Shape |
|---|---|---|
| **Done-for-you build** | "We don't have the time or the in-house skill." | Fixed-scope engagement: your schema, sources, tools, wiki, prompt, evals, deployed MCP endpoint. |
| **AI-honesty audit** | "We already shipped a bot — is it lying to users?" | Point the methodology at an existing assistant; deliver a findings report + an eval set they keep. |
| **Governance retainer** *(recurring)* | "It worked at launch, then drifted." | Run the refresh loop, grow the Golden Set from real misses, keep prod-model == eval-model, watch regressions. |
| **Training & workshops** | "We want to own this ourselves." | Hands-on, this repo as the lab; your team learns to build and maintain grounded systems. |

### Accelerators — compress the months (sold with services; products later)

These are exactly the high-leverage, domain-heavy assets the open repo deliberately
*doesn't* include, because they're where the real time goes:

| Accelerator | Why it's worth paying for |
|---|---|
| **Connector / scraper library** | Prebuilt ingest for common sources (catalogs, CMS, Confluence/Notion, Zendesk, SharePoint, sitemaps). The example ships hand-authored YAML; real sources are the work. |
| **Vertical schema templates** | The hard "what goes in the DB vs. the wiki" judgment, pre-made for edu / gov / healthcare / SaaS / legal. Get the split wrong and the whole guarantee leaks. |
| **Curated eval banks** | Per-vertical Golden Sets seeded from real question logs — the craft-heavy artifact almost nobody builds well. |
| **Managed hosting** *(recurring)* | The deployed, authed, monitored MCP endpoint + scheduled refresh, with an SLA. (The open repo gives you a Dockerfile; this gives you uptime.) |
| **Scale + ops kit** | sqlite-vec / pgvector recipes, production embedder integrations, CI gates, alerting, and the observability **dashboards** (the open repo logs tool calls; this turns them into a scoreboard). |
| **Recompile + governance playbooks** | Tuned LLM authoring prompts and review workflows that keep recompiled prose lint-clean and on-voice. |

## The boundary, per pillar

| Pillar | Open core | Commercial layer |
|---|---|---|
| **1 · Retrieval** | schema pattern, query layer, lexical scanner, embedder interface + offline embedder | connectors, vertical schema templates, production embedders, vector-DB scaling, the curated corpus itself |
| **2 · Weekly agent** | the pipeline skeleton + scheduling templates | reliable managed runs, real scrapers, alerting, dashboards, SLA |
| **3 · Eval harness** | the harness, graders, tiers, a 15-item example set | vertical eval banks, judge tuning, log-mining to grow the set, regression review as a service |

The simple read: **the repo is the machine; the paid layer is the fuel, the
operations, and the expert who runs it.**

## Why this helps the services business (instead of cannibalizing it)

- **The 80% is domain work the repo can't do for you** — modeling your facts,
  ingesting your sources, curating, writing the eval set from real misses. The
  repo shrinks the *boilerplate*, never the billable judgment.
- **It proves competence**, the hardest thing to sell in consulting. "The person
  who wrote groundtruth" is who an institution calls.
- **It de-risks the buyer** — they can see exactly what they're getting before a
  contract, which shortens sales cycles.
- **It makes teaching cheaper to deliver** — the repo is the curriculum; people
  pay for the guided path, feedback, and accountability.

The one risk to manage isn't piracy — it's **anchoring** (polish can make the
work look trivial). Counter it by keeping the hard parts visible in the docs and
selling the scarce thing (your time, judgment, accountability), not the abundant
thing (the code, now free).

## What was deliberately left out of the public repo

These exist in production-grade builds and are the natural first paid accelerators:
real source scrapers, observability **dashboards/scoreboards**, one-command cloud
deploy + managed refresh, and a large curated corpus with a 50–200 item Golden Set.
Their absence here is the boundary, drawn on purpose.
