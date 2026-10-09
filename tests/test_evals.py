import json
import unittest

from helpers import copy_project

from groundtruth import evals


class TestToolsTier(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def latest(self):
        return json.loads(sorted(self.p.runs_dir.glob("*-tools.json"))[-1].read_text())

    def test_example_passes(self):
        self.assertEqual(evals.run(self.p, "tools", quiet=True), 0)
        data = self.latest()
        self.assertEqual((data["passed"], data["total"], data["skipped"]), (12, 12, 3))

    def test_catches_wrong_data(self):
        fees = self.p.facts_dir / "fees.yaml"
        fees.write_text(fees.read_text().replace("amount_usd: 0.10", "amount_usd: 0.15"))
        self.assertEqual(evals.run(self.p, "tools", quiet=True), 1)
        failed = {r["id"]: r["reasons"] for r in self.latest()["results"] if not r["passed"]}
        self.assertIn("printing-bw-cost", failed)
        self.assertIn("0.10", failed["printing-bw-cost"][0])

    def test_catches_retrieval_regressions(self):
        page = self.p.wiki_dir / "journeys" / "using-the-elibrary-app.md"
        page.unlink()
        self.assertEqual(evals.run(self.p, "tools", quiet=True), 1)
        failed = {r["id"] for r in self.latest()["results"] if not r["passed"]}
        self.assertIn("borrow-ebooks", failed)

    def test_replay_regrades_the_recorded_run(self):
        self.assertEqual(evals.run(self.p, "replay", quiet=True), 0)


class TestGrading(unittest.TestCase):
    def test_contains_is_numeric_aware(self):
        self.assertTrue(evals.contains('{"amount_usd": 0.1}', "0.10"))
        self.assertTrue(evals.contains("It's $2.00", "2"))
        self.assertFalse(evals.contains("about 2.5 days", "25"))
        self.assertTrue(evals.contains("Fine-Free since 2024", "fine-free"))

    def test_grade_tool_and_text(self):
        item = {"category": "hard_fact", "must_call_tool": "get_fee", "must_contain": ["0.10"]}
        ok, _ = evals.grade(item, "It's $0.10 a page.", [{"name": "get_fee", "input": {}}], use_judge=False)
        self.assertTrue(ok)
        ok, why = evals.grade(item, "It's $0.10 a page.", [], use_judge=False)
        self.assertFalse(ok)
        self.assertIn("didn't call get_fee", why[0])

    def test_out_of_scope_must_not_call(self):
        item = {"category": "out_of_scope", "must_not_call_tool": "any", "must_contain": ["account"]}
        ok, _ = evals.grade(item, "I can't see your account.", [], use_judge=False)
        self.assertTrue(ok)
        ok, _ = evals.grade(item, "I can't see your account.", [{"name": "get_fee", "input": {}}], use_judge=False)
        self.assertFalse(ok)

    def test_required_page_read(self):
        item = {"category": "narrative", "must_cite_page": "fines-and-fees"}
        calls = [{"name": "read_wiki_page", "input": {"path_or_slug": "wiki/policies/fines-and-fees.md"}}]
        self.assertTrue(evals.grade(item, "", calls, use_judge=False)[0])
        self.assertFalse(evals.grade(item, "", [], use_judge=False)[0])


if __name__ == "__main__":
    unittest.main()
