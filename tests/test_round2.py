"""Regression tests for the second independent review's findings."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import copy_project, write

from groundtruth import checks, cli, ingest, review, server


class TestLintCoverage(unittest.TestCase):
    def test_times_sizes_and_word_durations(self):
        def kinds(text):
            return [k for k, rx in checks.PATTERNS if rx.search(text)]
        self.assertIn("time", kinds("The office opens at 09:00."))
        self.assertIn("time", kinds("Open until 5 pm."))
        self.assertIn("size limit", kinds("Each export is limited to 100 MiB."))
        self.assertIn("duration", kinds("Download links expire after seven days."))
        for ok in ("It usually takes about two weeks.", "a couple of weeks", "a few days", "Python 3.12"):
            self.assertEqual(kinds(ok), [], ok)


class TestEvidence(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_source_is_an_error(self):
        (self.p.pages_dir / "programs.md").unlink()
        report = checks.run(self.p)
        self.assertIn("programs.md", "\n".join(report.errors.get("facts", [])))

    def test_same_name_documents_stay_separate(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d) / "a" / "policy.md", "# A\n\nDepartment A allows remote Fridays.\n")
            write(Path(d) / "b" / "policy.md", "# B\n\nDepartment B requires badges.\n")
            first = ingest.run(self.p, [str(Path(d) / "a"), str(Path(d) / "b")], save=False)
            self.assertEqual(len(first.new), 2)
            again = ingest.run(self.p, [str(Path(d) / "a"), str(Path(d) / "b")], save=False)
            self.assertEqual(len(again.unchanged), 2)
            texts = sorted(p.read_text() for p in self.p.pages_dir.glob("policy*.md"))
            self.assertTrue(any("Department A" in t for t in texts))
            self.assertTrue(any("Department B" in t for t in texts))

    def test_deleted_document_is_flagged_and_cited_one_fails_check(self):
        with tempfile.TemporaryDirectory() as d:
            doc = write(Path(d) / "hours.md", "# Hours\n\nThe help desk is staffed on weekdays.\n")
            ingest.run(self.p, [d], save=False)
            page = next(self.p.pages_dir.glob("hours.md"))
            fees = self.p.facts_dir / "fees.yaml"
            fees.write_text(fees.read_text().replace("    amount_usd: 25.00", "    amount_usd: 25.00\n"
                                                     "    source_path: sources/pages/hours.md"))
            review.mark_reviewed(self.p)
            self.assertTrue(checks.run(self.p).ok)
            doc.unlink()
            result = ingest.run(self.p, [d], save=False)
            self.assertEqual(result.removed, ["sources/pages/hours.md"])
            self.assertIn("removed_upstream", page.read_text())
            errs = "\n".join(checks.run(self.p).errors.get("sources", []))
            self.assertIn("removed upstream", errs)
            review.mark_reviewed(self.p)
            self.assertFalse(checks.run(self.p).ok)  # still cited: reviewing alone doesn't clear it

    def test_review_diff_without_git(self):
        src = self.p.pages_dir / "borrowing-policy.md"
        ingest.save_history(self.p, src)
        src.write_text(src.read_text() + "\nFINE-FREE (updated)\n")
        d = review.diff(self.p, "sources/pages/borrowing-policy.md")
        self.assertIn("+", d)
        self.assertIn("FINE-FREE (updated)", d)
        review.mark_reviewed(self.p)
        self.assertFalse(any((self.p.sources_dir / ".history").rglob("*.md")))  # pruned once reviewed


class TestServingIsGated(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()
        page = self.p.wiki_dir / "journeys" / "getting-a-library-card.md"
        page.write_text(page.read_text().replace("## Sources & Lineage", "Cards renew on 2026-11-01.\n\n"
                                                 "## Sources & Lineage", 1))
        server._check_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_reports_and_serve_refuses(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["-p", str(self.p.root), "build"]), 1)
        self.assertIn("won't serve", buf.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(server.run(self.p), 1)

    def test_health_reports_failing_checks(self):
        status = server.health(self.p)["checks"]
        self.assertFalse(status["ok"])
        self.assertIn("2026-11-01", status["first"])


class TestAskAndYaml(unittest.TestCase):
    def test_ask_json_and_friendly_yaml_errors(self):
        p, tmp = copy_project()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["-p", str(p.root), "ask", "--offline", "--json", "how", "much", "is", "color", "printing"])
            data = json.loads(buf.getvalue())
            self.assertEqual(data["facts"][0]["tool"], "get_fee")
            write(p.facts_dir / "bad.yaml", "key: id\nrows:\n  - id: a\n   name: b\n")
            errs = "\n".join(checks.run(p).errors["facts"])
            self.assertIn("line 4", errs)
            self.assertIn("indentation", errs)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
