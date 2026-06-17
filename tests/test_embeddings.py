import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundtruth import embeddings


class TestHashingEmbedder(unittest.TestCase):
    def test_deterministic_and_normalized(self):
        e = embeddings.HashingEmbedder(dim=64)
        a = e.embed(["holds and renewals at the library"])[0]
        b = e.embed(["holds and renewals at the library"])[0]
        self.assertEqual(a, b)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in a)), 1.0, places=6)

    def test_overlap_scores_higher_than_disjoint(self):
        e = embeddings.HashingEmbedder(dim=128)
        q, near, far = e.embed([
            "library card registration",
            "how to register for a library card",
            "color printing prices per page",
        ])
        self.assertGreater(embeddings._cosine(q, near), embeddings._cosine(q, far))


class TestVectorStore(unittest.TestCase):
    def test_build_and_query(self):
        with tempfile.TemporaryDirectory() as d:
            old = embeddings.INDEX_PATH
            embeddings.INDEX_PATH = Path(d) / "idx.db"
            try:
                n = embeddings.build_index([
                    ("a", "borrowing holds and renewals"),
                    ("b", "free wifi and public computers"),
                    ("c", "summer reading program for kids"),
                ])
                self.assertEqual(n, 3)
                top = embeddings.query("how do renewals and holds work", limit=1)
                self.assertEqual(top[0][0], "a")
                self.assertTrue(embeddings.index_status()["exists"])
            finally:
                embeddings.INDEX_PATH = old

    def test_query_without_index_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            old = embeddings.INDEX_PATH
            embeddings.INDEX_PATH = Path(d) / "missing.db"
            try:
                self.assertEqual(embeddings.query("anything"), [])
            finally:
                embeddings.INDEX_PATH = old


if __name__ == "__main__":
    unittest.main()
