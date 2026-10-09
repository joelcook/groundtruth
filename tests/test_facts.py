import time
import unittest

from helpers import copy_project, write

from groundtruth import facts


class TestLibraryFacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p, cls.tmp = copy_project()
        cls.store = facts.store(cls.p)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, tool, **args):
        return self.store.call(tool, args)

    def test_build_counts(self):
        counts = facts.build(self.p)
        self.assertEqual(counts["hours"], 21)
        self.assertEqual(counts["fees"], 5)
        self.assertFalse(facts.is_stale(self.p))

    def test_lookup_is_forgiving_and_exact(self):
        r = self.call("get_fee", fee_type="late fees")
        self.assertEqual(r["results"][0]["fee_type"], "overdue-fine")
        self.assertEqual(r["results"][0]["amount_usd"], 0.0)
        r = self.call("get_fee", fee_type="black and white printing")
        self.assertEqual(r["results"][0]["amount_usd"], 0.10)

    def test_every_row_carries_its_source(self):
        r = self.call("get_contact", department="youth services")
        self.assertEqual(r["results"][0]["phone"], "555-0103")
        self.assertTrue(r["results"][0]["source_url"].startswith("https://"))

    def test_list_filters_resolve_refs_and_abbreviations(self):
        r = self.call("get_hours", branch="main", day="sat")
        self.assertEqual(r["count"], 1)
        self.assertEqual((r["results"][0]["branch"], r["results"][0]["opens"]), ("central", "10:00"))
        r = self.call("get_hours", branch="lakeview", day="monday")
        self.assertTrue(r["results"][0]["closed"])

    def test_wildcards(self):
        names = {row["id"] for row in self.call("list_programs", audience="teens")["results"]}
        self.assertIn("summer-reading", names)  # audience "all" matches every audience
        self.assertNotIn("homework-help", names)

    def test_upcoming_only(self):
        rows = self.call("list_events", upcoming_only=False)["results"]
        self.assertEqual(len(rows), 6)
        upcoming = self.call("list_events")["results"]
        today = time.strftime("%Y-%m-%d")
        self.assertTrue(all(not r.get("date") or r["date"] >= today for r in upcoming))

    def test_no_match_gives_a_hint(self):
        r = self.call("get_fee", fee_type="parking")
        self.assertEqual(r["count"], 0)
        self.assertIn("printing-bw", r["hint"])

    def test_suggest_extracts_filters(self):
        best = self.store.suggest("what time does the central library open on saturday?")[0]
        self.assertEqual(best["tool"], "get_hours")
        self.assertEqual(best["args"], {"branch": "central", "day": "saturday"})


class TestRebuildAndValidation(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_editing_yaml_is_picked_up_without_a_restart(self):
        store = facts.store(self.p)
        self.assertEqual(store.call("get_fee", {"fee_type": "meeting room"})["results"][0]["amount_usd"], 25.0)
        path = self.p.facts_dir / "fees.yaml"
        path.write_text(path.read_text().replace("amount_usd: 25.00", "amount_usd: 30.00"))
        store._checked_at = 0  # skip the 1-second throttle
        self.assertEqual(store.call("get_fee", {"fee_type": "meeting room"})["results"][0]["amount_usd"], 30.0)

    def _problems(self, yaml_text):
        write(self.p.facts_dir / "zzz.yaml", yaml_text)
        with self.assertRaises(facts.FactsError) as ctx:
            facts.load(self.p)
        return "\n".join(ctx.exception.problems)

    def test_missing_key_and_duplicates(self):
        msg = self._problems("source: {url: https://x}\ntools: [{name: get_zzz, description: d}]\n"
                             "rows:\n  - {id: a}\n  - {id: a}\n  - {name: b}\n")
        self.assertIn("duplicate id", msg)
        self.assertIn("missing its key field", msg)

    def test_every_row_needs_a_source(self):
        msg = self._problems("tools: [{name: get_zzz, description: d}]\nrows:\n  - {id: a}\n")
        self.assertIn("has no source", msg)

    def test_reserved_param_names(self):
        msg = self._problems("source: {url: https://x}\ntools: [{name: get_zzz, arg: limit, description: d}]\n"
                             "rows:\n  - {id: a}\n")
        self.assertIn("reserved", msg)

    def test_comma_in_braces_hint(self):
        msg = self._problems("source: {url: https://x}\ntools: [{name: get_zzz, description: d}]\n"
                             "rows:\n  - {id: a, address: 1 Main St, Suite 2}\n")
        self.assertIn("put that value in quotes", msg)

    def test_unknown_ref_and_tool_collisions(self):
        msg = self._problems("source: {url: https://x}\ntools:\n  - {name: get_fee, description: d}\n"
                             "  - name: list_zzz\n    kind: list\n    description: d\n"
                             "    filters: [{field: id, ref: nope}]\nrows:\n  - {id: a}\n")
        self.assertIn("two collections define the tool 'get_fee'", msg)
        self.assertIn("unknown collection 'nope'", msg)

    def test_bad_dates(self):
        msg = self._problems("source: {url: https://x}\ndate_field: date\ntools: [{name: get_zzz, description: d}]\n"
                             "rows:\n  - {id: a, date: next tuesday}\n")
        self.assertIn("must be YYYY-MM-DD", msg)


if __name__ == "__main__":
    unittest.main()
