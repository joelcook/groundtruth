import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundtruth import search


class TestScan(unittest.TestCase):
    def test_card_query_ranks_card_page(self):
        hits = search.search_wiki("how do I get a library card", limit=3)
        self.assertTrue(hits[0]["path"].endswith("getting-a-library-card.md"))

    def test_fees_query_ranks_fines_page(self):
        hits = search.search_wiki("do I owe late fees", limit=3)
        self.assertTrue(hits[0]["path"].endswith("fines-and-fees.md"))

    def test_results_carry_sources(self):
        hits = search.search_wiki("ebooks", limit=1)
        self.assertTrue(hits[0]["sources"])

    def test_read_page_by_slug(self):
        page = search.read_wiki_page("fines-and-fees")
        self.assertIsNotNone(page)
        self.assertIn("fine-free", page["content"].lower())

    def test_read_missing_page(self):
        self.assertIsNone(search.read_wiki_page("does-not-exist"))

    def test_empty_query(self):
        self.assertEqual(search.search_wiki(""), [])


if __name__ == "__main__":
    unittest.main()
