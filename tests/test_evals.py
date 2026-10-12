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
        self.assertEqual((data["passed"], data["total"], data["skipped"]), (15, 15, 3))

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


class TestCopilotStudioExport(unittest.TestCase):
    """Grade the production model where it runs: the golden set as a Copilot Studio test set."""

    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_question_becomes_a_row_graded_by_meaning(self):
        import csv
        out, n = evals.export_copilot_studio(self.p)
        rows = list(csv.DictReader(out.open()))
        self.assertEqual(list(rows[0]), evals.COPILOT_COLUMNS)
        self.assertEqual(len(rows), n)
        self.assertEqual({r["Testing method"] for r in rows}, {"Compare meaning"})
        self.assertTrue(str(out).startswith(str(self.p.build_dir)))  # generated: never committed

    def test_expected_responses_come_from_the_checks(self):
        fact = {"category": "hard_fact", "must_call_tool": "get_fee", "must_contain": ["0.10"]}
        self.assertIn('includes "0.10"', evals.expected_response(fact))
        self.assertNotIn("get_fee", evals.expected_response(fact))  # the judge only sees the answer
        refusal = {"category": "out_of_scope", "must_contain": ["account"]}
        self.assertIn("Declines", evals.expected_response(refusal))
        stale = {"category": "contradiction", "expected_summary": "We're fine-free.", "must_not_contain": ["$10"]}
        self.assertEqual(evals.expected_response(stale), 'We\'re fine-free. It does NOT say "$10".')
        self.assertEqual(evals.expected_response({**fact, "expected_response": "Ten cents a page."}),
                         "Ten cents a page.")

    def test_copilot_studio_limits(self):
        long = "x" * (evals.COPILOT_MAX_QUESTION + 1)
        self.p.golden_path.write_text(f"- id: long\n  question: {long}\n  category: narrative\n")
        with self.assertRaises(ValueError):
            evals.export_copilot_studio(self.p)



class TestInventedValuesKeepUnits(unittest.TestCase):
    """A value only counts as backed up when a tool returned it with the same meaning: unit, currency, kind."""

    CASES = [
        ({"duration_days": 30}, "Loans last 30 weeks.", ["30 weeks"]),
        ({"duration_days": 30}, "Loans last 30 days.", []),
        ({"amount_usd": 30}, "It costs £30.", ["£30"]),
        ({"amount_usd": 30}, "It costs $30.", []),
        ({"window_days": 30}, "Borrow up to 30 books.", ["up to 30"]),
        ({"max_items": 30}, "You can borrow up to 30 books.", []),
        ({"loan_days": 30}, "You can borrow for up to 30 days.", []),
        ({"duration_days": 7}, "Loans last seven days.", []),
        ({"closes": "12:00"}, "It closes at noon.", []),
        ({"note": "Copies are 10 cents each."}, "Copies are $10.", ["$10"]),
        ({"note": "Copies are 10 cents each."}, "Copies are $0.10 each.", []),
        ({"note": "Loans last twenty-one days."}, "Loans last one day.", ["one day"]),
        ({"note": "Loans last 21 days."}, "Loans last twenty-one days.", []),
        ({"yearly_fee": 30}, "Loans last 30 days.", ["30 days"]),
        ({"value": 2, "unit": "GB per file"}, "Uploads up to 2 GB.", []),
        ({"value": 2, "unit": "GB per file"}, "Uploads up to 2 MB.", ["2 MB"]),
        ({"max_items": 2}, "Uploads up to 2 GB.", ["2 GB"]),
        ({"value": 30, "unit": "days"}, "Refunds within 30 days.", []),
        ({"loanPeriodDays": 14}, "Loans last 14 days.", []),
        ({"window_days": 30}, "The fee is $30.", ["$30"]),
        ({"price": 30, "currency": "USD", "trial_days": 14, "seat_limit": 3}, "The plan costs $14.", ["$14"]),
        ({"price": 30, "currency": "USD", "trial_days": 14, "seat_limit": 3},
         "The plan costs $30, with a 14-day trial and up to 3 seats.", []),
        ({"price": 30, "currency": "EUR"}, "It costs $30.", ["$30"]),
        ({"durationDays": 30}, "Loans last 30 weeks.", ["30 weeks"]),
        ({"value": "30", "unit": "days"}, "Borrow up to 30 books.", ["up to 30"]),
    ]

    def test_cases(self):
        for row, answer, invented in self.CASES:
            calls = [{"name": "get_x", "input": {"query": "$30 30 weeks"}, "result": {"results": [row], "count": 1}}]
            with self.subTest(row=row, answer=answer):
                self.assertEqual(evals.invented_values("q", answer, calls), invented)


if __name__ == "__main__":
    unittest.main()
