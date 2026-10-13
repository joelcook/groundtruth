"""Grounding by row: a value has to be where its source describes that row, and rows obey their rules."""
import unittest

from helpers import TEMPLATES, copy_project, write

from groundtruth import checks, facts, grounding, project


def swap(path, old, new):
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


class TestPlacement(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name, page, rows, key="id", tools="[{name: get_%s, kind: lookup, description: Things.}]"):
        """A facts file `name` citing a captured page with this text."""
        write(self.p.root / f"sources/pages/{name}.md", f"---\nsource_url: https://example.org/{name}\n---\n\n{page}")
        write(self.p.facts_dir / f"{name}.yaml", f"key: {key}\nsource: {{path: sources/pages/{name}.md}}\n"
              f"tools: {tools % name if '%s' in tools else tools}\nrows:\n" + "".join(f"  - {r}\n" for r in rows))

    def test_a_value_from_the_wrong_row_is_caught(self):
        swap(self.p.facts_dir / "hours.yaml", 'id: central-sat, branch: central, day: saturday,  opens: "10:00"',
             'id: central-sat, branch: central, day: saturday,  opens: "13:00"')
        found = grounding.problems(self.p)
        self.assertEqual(len(found), 1, found)
        self.assertIn("row central-sat: opens has '13:00'", found[0])
        self.assertIn("gives for central-sun (\"Sunday | 1:00 p.m. – 5:00 p.m.\")", found[0])
        self.assertIn("not for this row (\"Saturday | 10:00 a.m. – 5:00 p.m.\")", found[0])
        self.assertTrue(checks.run(self.p).ok)  # a warning while you build...
        self.assertFalse(checks.run(self.p, production=True).ok)  # ...and a failure where it ships

    def test_headings_and_day_ranges_say_which_row_a_line_is_about(self):
        # Eastside's Saturday given Central's weekday opening: the line is under Central's heading, Mon to Thu
        hours = self.p.facts_dir / "hours.yaml"
        swap(hours, 'id: eastside-sat, branch: eastside, day: saturday,  opens: "10:00"',
             'id: eastside-sat, branch: eastside, day: saturday,  opens: "09:00"')
        found = grounding.problems(self.p)
        self.assertEqual(len(found), 1, found)
        self.assertIn("gives for central-", found[0])
        swap(hours, 'opens: "09:00"', 'opens: "10:00"')
        swap(self.p.facts_dir / "fees.yaml", "amount_usd: 0.10", "amount_usd: 0.50")
        self.assertIn("gives for printing-color", " ".join(grounding.problems(self.p)))

    def test_sentences_are_blocks_too(self):
        page = "# Prices\n\nOffice visit $120.00. Flu shot $35.00. Annual physical $150.00.\n"
        rows = ["{id: office-visit, amount_usd: 120}", "{id: flu-shot, amount_usd: 35}",
                "{id: annual-physical, amount_usd: 150}"]
        self.add("prices", page, rows)
        self.assertEqual(grounding.problems(self.p), [])
        self.add("prices", page, rows[:1] + ["{id: flu-shot, amount_usd: 120}"] + rows[2:])
        self.assertIn("row flu-shot: amount_usd has '120'", " ".join(grounding.problems(self.p)))

    def test_a_page_that_cant_tell_rows_apart_falls_back_to_the_whole_page(self):
        # One column per plan: every line names every plan, so no line belongs to just one.
        grid = ("# Pricing\n\n| | Starter | Team | Enterprise |\n|---|---|---|---|\n"
                "| Price | $9 | $29 | Contact sales |\n| Projects | 3 | Unlimited | Unlimited |\n")
        rows = ["{id: starter, price_usd: 9, projects: 3}", "{id: team, price_usd: 29, projects: unlimited}",
                "{id: enterprise, price_note: contact sales}"]
        self.add("plans", grid, rows)
        self.assertEqual(grounding.problems(self.p), [])
        # And a sentence that names no row is never "about another row".
        self.add("events2", "# Story time\n\nToddler Story Time meets every week.\nIt starts at 10:30 a.m.\n\n"
                 "Teen Game Night is on Fridays at 6:00 p.m.\n",
                 ["{id: toddler-story-time, start: '10:30'}", "{id: teen-game-night, start: '18:00'}"])
        self.assertEqual(grounding.problems(self.p), [])

    def test_the_templates_raise_no_false_alarms(self):
        for root in sorted(TEMPLATES.iterdir()):
            p = project.load(root)
            cols = facts.load(p)
            for c in cols:  # the templates are fictional, so problems() skips them: check them directly
                self.assertEqual(grounding.placement_problems(p, c, c.rows, cols, p.rel(c.file)), [], c.file)

    def test_words_spell_out_days(self):
        self.assertLessEqual({"monday", "tuesday", "wednesday", "thursday"}, grounding.words("Mon–Thu 9-5"))
        self.assertNotIn("friday", grounding.words("Monday to Thursday"))
        self.assertEqual({"saturday", "sunday", "monday"}, grounding.words("Sat through Mon"))
        self.assertEqual(set(grounding.WEEKDAYS[:5]), grounding.words("weekdays"))
        self.assertIn("copy", grounding.words("Black-and-white printing and copies"))

    def test_abbreviations_dont_end_sentences(self):
        self.assertEqual(grounding._sentences("Opens 9 a.m. Monday to Friday. Closed Sept. 1. Call us."),
                         ["Opens 9 a.m. Monday to Friday.", "Closed Sept. 1.", "Call us."])


class TestRules(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()
        self.hours = self.p.facts_dir / "hours.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def problems(self):
        try:
            facts.load(self.p)
        except facts.FactsError as exc:
            return exc.problems
        return []

    def test_the_demo_keeps_its_rule(self):
        self.assertEqual(self.problems(), [])
        self.assertIn("opens < closes", self.hours.read_text())

    def test_a_row_that_breaks_a_rule_fails_check(self):
        # 18:00 is on the page, on Lakeview's own line, so grounding can't see this; the rule can
        swap(self.hours, 'id: lakeview-tue, branch: lakeview, day: tuesday,   opens: "10:00"',
             'id: lakeview-tue, branch: lakeview, day: tuesday,   opens: "18:00"')
        self.assertEqual(self.problems(),
                         ["hours.yaml: row lakeview-tue breaks the rule `opens < closes` (opens is '18:00', "
                          "closes is '18:00')"])
        self.assertFalse(checks.run(self.p).ok)

    def test_rules_compare_dates_numbers_and_literals_and_skip_missing_fields(self):
        swap(self.hours, "rules:\n  - opens < closes", "rules: [opens < closes, 'opens >= 0', nope]")
        found = self.problems()
        self.assertTrue(any("rule 'nope' must compare two fields" in p for p in found), found)
        self.assertTrue(any("can't be checked against the rule `opens >= 0`" in p for p in found), found)
        self.assertFalse(any("closed" in p for p in found))  # closed days have no opens: nothing to compare
        self.assertEqual(facts._comparable("2026-09-01")[0], "date")
        self.assertLess(facts._comparable("9:30")[1], facts._comparable("17:00")[1])
        self.assertEqual(facts._comparable("1,200"), ("number", facts.Decimal(1200)))
        swap(self.hours, "rules: [opens < closes, 'opens >= 0', nope]", "rules: [opens < closing]")
        self.assertIn("names `closing`, but no row has that field", self.problems()[0])


if __name__ == "__main__":
    unittest.main()
