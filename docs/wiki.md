# Wiki reference

The wiki holds explanations: how-tos, policies, "what is", "how do I". Being
approximately right is fine here; being exact is the facts layer's job. The
model reaches the wiki through two tools, `search_wiki` and `read_wiki_page`.

## A page

```markdown
---
title: Reset your password
page_type: process
last_compiled: 2026-01-15
compiled_by: you
sources:
  - sources/pages/password-help.md
status: published
tags: [password, login, sign in, locked]
---

# Reset your password

1. On the sign-in page, choose **Forgot password**.
2. Enter your work email and follow the link we send.

Using single sign-on? Reset it with your identity provider instead. See [[sso]].

## Sources & Lineage

| Source | Captured | URL |
|---|---|---|
| [[sources/pages/password-help.md]] | 2026-01-15 | https://example.com/help/password |
```

**Required frontmatter:** `title`, `page_type`, `sources`.
**Recommended:** `last_compiled` (drives staleness), `compiled_by`, `status`,
`tags` (search weighs them heavily).

**`page_type`** is one of `journey` (start-to-finish goal), `process` (one
task), `policy` (rules, plainly), `entity` (a program, place, or thing),
`comparison`, `faq`, `guide`, or `internal` (never shown to the model).

**Every page ends with `## Sources & Lineage`.** It's the audit trail, and
`./gt check` requires it.

**`status: draft`** keeps a page out of search and out of the source-review
gate until it's done. `./gt compile` writes drafts to `wiki/drafts/`; set
`status: published` when one is ready (move it to another folder if you like).

## The one rule: no facts in prose

`./gt check` fails if a wiki page contains any of these outside the frontmatter
and the lineage table:

| Kind | Examples |
|---|---|
| dates | `July 4, 2026`, `November 1`, `2026-07-04`, `7/4/2026` |
| times | `09:00`, `5 pm` |
| money | `$5`, `USD 39`, `5 dollars` |
| durations | `within 30 days`, `14-day trial`, `seven days` (approximate wording like "about two weeks" is fine) |
| percentages | `20%` |
| size limits | `2 GB`, `100 MiB` |
| limits | `up to 25`, `at most 3`, `allows 10`, `no more than 5` |
| phone numbers, emails | `555-0100`, `help@example.com` |

It recognizes the common ways of writing these, not every possible one. It's a
safety net, not a guarantee: the rule still applies to anything it misses.

Write around the fact and name the tool:

> Annual plans can be refunded within the refund window. The assistant looks up
> the exact window with `get_limit`.

If a value really is stable and illustrative, justify it on the same line or the
line above:

```markdown
<!-- fact-ok: illustrative example, not a live price -->
A typical coffee costs about $3.
```

Use it sparingly. The default is: put it in `facts/`.

## Organizing

- Put pages under `wiki/<type>s/` (`wiki/processes/reset-password.md`).
  The folder is for humans; search finds pages anywhere under `wiki/`.
- Link pages with `[[slug]]` (the file name without `.md`). Link evidence with
  `[[sources/pages/file.md]]`. Broken links fail `./gt check`.
- `wiki/index.md` is your table of contents. It isn't served to the model.
- `wiki/internal/` and pages with `audience: internal` are never served.

## Staleness

A page is stale when one of its sources changed since the last `./gt review
--done` (tracked by content hash in `sources.lock.yaml`, so same-day changes
count). `./gt check` fails, `./gt review` and the compile packet list the page,
and the weekly refresh puts it in its pull request. Set `GT_STALE_DAYS` to also
flag pages by age.

## Writing pages that search well

- Put the words people actually use in the `title` and `tags`.
- Lead with the answer; the first paragraph becomes the search snippet.
- When search misses, add a synonym in `groundtruth.yaml` under
  `search.synonyms` (`password: [login, sign, locked]`) and an eval question so it
  stays fixed. See [retrieval.md](retrieval.md).
