import unittest

from helpers import copy_project, write

from groundtruth import checks

PAGE = """---
title: T
page_type: process
sources: [sources/pages/borrowing-policy.md]
---

# T

{body}

## Sources & Lineage

| Source | Captured | URL |
|---|---|---|
| [[sources/pages/borrowing-policy.md]] | 2026-06-10 | https://example.org |
"""


class TestChecks(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, body, name="processes/t.md"):
        return write(self.p.wiki_dir / name, PAGE.format(body=body))

    def test_example_is_clean(self):
        report = checks.run(self.p)
        self.assertTrue(report.ok, report.errors)

    def test_prose_lint_flags_dates_and_money(self):
        path = self.page("It costs $5 and closes on July 4, 2026. Also 2026-07-04 and 12/25/2026.")
        kinds = [k for _, k, _ in checks.hard_facts_in(path)]
        self.assertEqual(kinds.count("money"), 1)
        self.assertGreaterEqual(kinds.count("date"), 1)
        self.assertFalse(checks.run(self.p).ok)

    def test_lineage_table_and_frontmatter_are_ignored(self):
        path = self.page("Nothing exact here.")
        self.assertEqual(checks.hard_facts_in(path), [])

    def test_fact_ok_escape_hatch(self):
        path = self.page("<!-- fact-ok: illustrative, not a live price -->\nA coffee is about $3.")
        self.assertEqual(checks.hard_facts_in(path), [])

    def test_structure_errors(self):
        write(self.p.wiki_dir / "processes" / "bad.md", "---\ntitle: Bad\npage_type: nonsense\nsources: []\n---\n\n"
                                                         "See [[nowhere]].\n")
        errs = "\n".join(checks.run(self.p).errors["wiki"])
        self.assertIn("page_type 'nonsense'", errs)
        self.assertIn("Sources & Lineage", errs)
        self.assertIn("broken link [[nowhere]]", errs)

    def test_golden_wiring(self):
        g = self.p.golden_path
        g.write_text(g.read_text() + "\n- id: bad-tool\n  question: q\n  category: hard_fact\n"
                     "  must_call_tool: get_nothing\n- id: bad-args\n  question: q\n  category: hard_fact\n"
                     "  must_call_tool: get_fee\n  tool_args: {wrong: x}\n- id: bad-page\n  question: q\n"
                     "  category: narrative\n  must_cite_page: no-such-page\n")
        errs = "\n".join(checks.run(self.p).errors["evals"])
        self.assertIn("get_nothing", errs)
        self.assertIn("tool_args has 'wrong'", errs)
        self.assertIn("no-such-page", errs)


if __name__ == "__main__":
    unittest.main()
