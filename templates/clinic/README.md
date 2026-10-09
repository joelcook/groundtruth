# {{NAME}}

This folder is your assistant. The code lives one level up; everything that
makes the assistant *yours* lives here, and `./gt` works on it.

| Folder | What goes in it | Who edits it |
|---|---|---|
| `facts/` | Anything exact: prices, dates, hours, phone numbers, limits. One YAML file per kind of thing. Each file says which tools the model gets. | You (or a coding agent) |
| `wiki/` | Explanations: how-tos, policies, "what is". Plain markdown with a sources table. No dates or dollar amounts in the prose. | You, `./gt compile`, or a coding agent |
| `sources/pages/` | The evidence everything above came from. | `./gt ingest` |
| `prompts/system_prompt.md` | Your assistant's instructions. Paste them into your chat platform too. | You |
| `evals/golden.yaml` | Questions with known answers. Your launch gate. | You |

## Make it yours

```bash
./gt ingest https://your-site.example     # pull your pages into sources/pages/
./gt compile                               # work order for Claude Code / Codex: pages + facts
./gt check                                 # facts valid, no facts in prose, links resolve
./gt eval                                  # free: tools return the right facts, search finds the pages
./gt ask "a question a real person asked"  # see what the assistant finds
./gt connect claude-desktop --install      # talk to it in Claude
```

Replace the example rows in `facts/` with real ones, rewrite the example wiki
pages, and swap the example questions in `evals/golden.yaml` for ten you know
the answers to. Every miss after launch becomes a new question.

Full docs: the repo's `README.md` and `docs/`.
