# Maplewood Public Library (demo)

A complete, small example: six facts files (branches, hours, events, programs,
fees, contacts), six wiki pages, and a 15-question golden set covering all four
eval categories, including a contradiction between a retired 2019 fee flyer and
the current fine-free policy.

```bash
./gt demo
./gt -p examples/library ask "is the eastside branch open on sunday?"
./gt -p examples/library eval
```

Start your own with `./gt init NAME --template library` to copy it, or pick a
closer template with `./gt init --list`.
