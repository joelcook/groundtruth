import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundtruth import evals


def call(name, **inp):
    return {"name": name, "input": inp}


class TestHardFactGrader(unittest.TestCase):
    def test_passes_when_right_tool_and_substring(self):
        item = {"category": "hard_fact", "must_call_tool": "get_fee", "must_contain": ["0.10"]}
        result = {"tool_calls": [call("get_fee", fee_type="printing")], "response": "It's $0.10 per page."}
        passed, reasons = evals.grade_hard_fact(item, result)
        self.assertTrue(passed, reasons)

    def test_fails_when_tool_not_called(self):
        # A right-looking answer with no tool call must FAIL — the whole point.
        item = {"category": "hard_fact", "must_call_tool": "get_fee", "must_contain": ["0.10"]}
        result = {"tool_calls": [], "response": "It's $0.10 per page."}
        passed, _ = evals.grade_hard_fact(item, result)
        self.assertFalse(passed)

    def test_must_not_contain(self):
        item = {"category": "hard_fact", "must_call_tool": "get_fee", "must_not_contain": ["0.25"]}
        result = {"tool_calls": [call("get_fee")], "response": "Late fines are 0.25 a day."}
        self.assertFalse(evals.grade_hard_fact(item, result)[0])


class TestOutOfScopeGuard(unittest.TestCase):
    def test_any_tool_call_fails(self):
        item = {"category": "out_of_scope", "must_not_call_tool": "any", "must_contain": ["account"]}
        bad = {"tool_calls": [call("get_contact")], "response": "check your account"}
        good = {"tool_calls": [], "response": "I can't see your account; check My Account."}
        self.assertFalse(evals.grade_hard_fact(item, bad)[0])
        self.assertTrue(evals.grade_hard_fact(item, good)[0])


class TestPageReadGrader(unittest.TestCase):
    def test_required_page_read(self):
        item = {"must_cite_page": "fines-and-fees"}
        hit = {"tool_calls": [call("read_wiki_page", path_or_slug="fines-and-fees")]}
        miss = {"tool_calls": [call("read_wiki_page", path_or_slug="getting-a-library-card")]}
        self.assertEqual(evals.grade_required_page_read(item, hit), [])
        self.assertTrue(evals.grade_required_page_read(item, miss))


class TestContradictionGrader(unittest.TestCase):
    def test_requires_assertions(self):
        passed, _ = evals.grade_contradiction({"category": "contradiction"}, {"tool_calls": [], "response": ""})
        self.assertFalse(passed)

    def test_full_contradiction_pass(self):
        item = {"category": "contradiction", "must_call_tool": "get_fee",
                "must_cite_page": "fines-and-fees", "must_contain": ["fine-free"],
                "must_not_contain": ["0.25"]}
        result = {"tool_calls": [call("get_fee", fee_type="overdue"),
                                 call("read_wiki_page", path_or_slug="fines-and-fees")],
                  "response": "We are fine-free now; that old fee is retired."}
        self.assertTrue(evals.grade_contradiction(item, result)[0])


class TestNarrativeFast(unittest.TestCase):
    def test_needs_page_and_substring(self):
        item = {"category": "narrative", "must_cite_page": "getting-a-library-card", "must_contain": ["address"]}
        ok = {"tool_calls": [call("read_wiki_page", path_or_slug="getting-a-library-card")],
              "response": "Bring proof of address."}
        self.assertTrue(evals.grade_narrative_fast(item, ok)[0])


if __name__ == "__main__":
    unittest.main()
