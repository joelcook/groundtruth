import unittest

from helpers import copy_project

from groundtruth import embeddings, search


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p, cls.tmp = copy_project()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def top(self, q):
        return search.search_wiki(q, 3, project=self.p)[0]["path"]

    def test_ranks_the_right_page(self):
        self.assertTrue(self.top("how do I get a library card").endswith("getting-a-library-card.md"))
        self.assertTrue(self.top("do I owe late fees").endswith("fines-and-fees.md"))

    def test_synonyms_come_from_config(self):
        self.assertIn("overdue", self.p.synonyms["fine"])
        self.assertTrue(self.top("is the internet free").endswith("printing-and-computers.md"))

    def test_results_carry_sources(self):
        self.assertTrue(search.search_wiki("ebooks", 1, project=self.p)[0]["sources"])

    def test_read_page(self):
        page = search.read_wiki_page("fines-and-fees", project=self.p)
        self.assertIn("fine-free", page["content"].lower())
        self.assertIsNone(search.read_wiki_page("does-not-exist", project=self.p))
        self.assertEqual(search.search_wiki("", project=self.p), [])

    def test_edits_are_picked_up(self):
        page = self.p.wiki_dir / "processes" / "printing-and-computers.md"
        page.write_text(page.read_text().replace("# Printing", "# Printing and Zebra Stripes", 1))
        search._page_cache.clear()
        self.assertIn("zebra", search.read_wiki_page("printing-and-computers", project=self.p)["content"].lower())


class TestEmbeddings(unittest.TestCase):
    def test_hashing_embedder(self):
        e = embeddings.HashingEmbedder(dim=128)
        q, near, far = e.embed(["library card registration", "register for a library card",
                                "color printing prices"])
        self.assertGreater(embeddings._cosine(q, near), embeddings._cosine(q, far))

    def test_index_round_trip(self):
        p, tmp = copy_project()
        try:
            self.assertEqual(embeddings.query(p, "anything"), [])
            n = search.build_search_index(p)
            self.assertEqual(n, 6)
            hits = embeddings.query(p, "borrowing holds and renewals", limit=1)
            self.assertTrue(hits[0][0].endswith("borrowing-holds-and-renewals.md"))
            self.assertTrue(embeddings.index_status(p)["exists"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
