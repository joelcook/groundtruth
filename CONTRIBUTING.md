# Contributing

Thanks for helping. groundtruth stays useful by staying small and honest.

1. `./gt demo` to get set up, then `python -m unittest discover -s tests`.
2. Keep the code dependency-light and readable. Every module explains why it
   exists in its docstring.
3. Domain logic belongs in project YAML, never in `groundtruth/`.
4. Add a test for any behavior you change, and run `./gt -p examples/library check`
   and `./gt -p examples/library eval`. CI runs both on every template too.
5. New templates go in `templates/NAME/` and must pass `./gt check` and
   `./gt eval` out of the box (a test enforces it).

Bug reports are welcome. Include `./gt doctor` output.
