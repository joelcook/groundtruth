import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundtruth import db


class TestFacts(unittest.TestCase):
    def test_branches(self):
        self.assertEqual(len(db.list_branches()), 3)

    def test_printing_fee_exact(self):
        rows = db.get_fee("printing")
        bw = [r for r in rows if r["fee_type"] == "printing-bw"]
        self.assertEqual(bw[0]["amount_usd"], 0.10)

    def test_fine_free(self):
        overdue = db.get_fee("overdue")[0]
        self.assertEqual(overdue["amount_usd"], 0.0)
        self.assertIn("fine-free", overdue["notes"].lower())

    def test_saturday_hours(self):
        h = db.get_hours("central", "saturday")["hours"]["saturday"]
        self.assertFalse(h["closed"])
        self.assertEqual(h["opens"], "10:00")

    def test_lakeview_closed_monday(self):
        self.assertTrue(db.get_hours("lakeview", "monday")["hours"]["monday"]["closed"])

    def test_story_time_recurring(self):
        hits = db.get_event("story time")
        self.assertTrue(any("Wednesday" in e["date_text"] for e in hits))

    def test_program_registration_flag(self):
        self.assertTrue(db.get_program("summer reading")["registration_required"])

    def test_contact_phone(self):
        c = db.get_contact("youth")
        self.assertEqual(c[0]["phone"], "555-0103")

    def test_provenance_present(self):
        # Every fact carries a way to trace it back to a source.
        for r in db.get_fee():
            self.assertIn("source_url", r)


if __name__ == "__main__":
    unittest.main()
