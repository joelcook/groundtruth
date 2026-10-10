# {{NAME}}

This folder is your assistant: everything that makes it *yours* lives here.
The groundtruth code (and the `./gt` command) lives in `{{GT}}`. Run the
commands below from there; `-p {{HERE}}` points them at this folder.

| Folder | What goes in it | Who edits it |
|---|---|---|
| `facts/` | Anything exact: prices, dates, hours, phone numbers, limits. One YAML file per kind of thing. Each file says which tools the model gets. | You (or a coding agent) |
| `wiki/` | Explanations: how-tos, policies, "what is". Plain markdown with a sources table. No dates or dollar amounts in the prose. | You, `./gt compile`, or a coding agent |
| `sources/pages/` | The evidence everything above came from. | `./gt ingest` |
| `prompts/system_prompt.md` | Your assistant's instructions. MCP clients get them automatically; `./gt prompt` prints them ready to paste anywhere else. | You |
| `evals/golden.yaml` | Questions with known answers. Your launch gate. | You |

## Make it yours

```bash
cd {{GT}}
./gt -p {{HERE}} ingest https://your-site.example      # pull your pages into sources/pages/
./gt -p {{HERE}} compile                               # draft pages + a work order for Claude Code / Codex
./gt -p {{HERE}} check                                 # facts valid, no facts in prose, links resolve
./gt -p {{HERE}} eval                                  # free: tools return the right facts, search finds the pages
./gt -p {{HERE}} check --production                    # before you ship: no template examples left
./gt -p {{HERE}} ask "a question a real person asked"  # see what the assistant finds
./gt -p {{HERE}} connect claude-desktop --install      # talk to it in Claude
```

Everything the template shipped with is fictional and marked `example: true`.
Replace it with real facts, pages, and questions (the compile packet walks you
through it), then `./gt examples --remove` clears whatever's left. When ingest
brings in a changed version of a source your facts cite, `./gt check` fails
until you've re-checked them: `./gt review`, fix, `./gt review --done`.

The prompt's routing table (`{{FACT_TOOLS}}`) is generated from `facts/`. Every
miss after launch becomes a new eval question.

Full docs: the repo's `README.md` and `docs/`.
