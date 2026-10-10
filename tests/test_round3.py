"""Regression tests for the third independent review's findings."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from helpers import copy_project, write

from groundtruth import checks, compile as comp, evals, examples, facts, ingest, project, review, scaffold, server


class TestSourceIdentity(unittest.TestCase):
    """A real document that collided with a template file keeps one identity forever."""

    def test_changed_source_is_caught_after_template_removal(self):
        with tempfile.TemporaryDirectory() as d:
            p = project.load(scaffold.init(Path(d) / "acme", "saas", activate=False))
            docs = write(Path(d) / "docs" / "pricing.md", "# Pricing\n\nTeam plan: $39 a month.\n").parent
            with contextlib.redirect_stdout(io.StringIO()):
                ingest.run(p, [str(docs)])
            capture = ingest.capture_for(p, str((docs / "pricing.md").resolve()))
            self.assertIsNotNone(capture)
            self.assertNotEqual(capture.name, "pricing.md")  # the template owned that name
            examples.remove(p)
            review.mark_reviewed(p)
            write(docs / "pricing.md", "# Pricing\n\nTeam plan: $49 a month.\n")
            with contextlib.redirect_stdout(io.StringIO()):
                ingest.run(p, [str(docs)])
            self.assertEqual(ingest.capture_for(p, str((docs / "pricing.md").resolve())), capture)
            self.assertIn("$49", capture.read_text())
            self.assertEqual(len(list(p.pages_dir.glob("pricing*.md"))), 1)

    def test_two_captures_of_one_document_fail_check(self):
        p, tmp = copy_project()
        try:
            src = sorted(p.pages_dir.glob("*.md"))[0]
            write(p.pages_dir / "copy.md", src.read_text())
            self.assertTrue(any("same document" in e for e in checks.run(p).errors.get("sources", [])))
        finally:
            tmp.cleanup()


class TestLintFormats(unittest.TestCase):
    def test_codes_words_limits_and_month_days(self):
        def kinds(text):
            return {k for k, rx in checks.PATTERNS if rx.search(text)}
        self.assertIn("money", kinds("The Team plan costs USD 39 per month."))
        self.assertIn("limit", kinds("It allows up to 25 forms."))
        self.assertIn("date", kinds("Refund requests close on November 1."))
        self.assertEqual(kinds("Pick the plan that fits your team."), set())


class TestSavedIngestPaths(unittest.TestCase):
    def test_relative_folders_are_saved_relative_to_the_project(self):
        with tempfile.TemporaryDirectory() as d:
            p = project.load(scaffold.init(Path(d) / "a" / "acme", "blank", activate=False))
            write(Path(d) / "docs" / "faq.md", "# FAQ\n\nAsk us anything.\n")
            cwd = os.getcwd()
            try:
                os.chdir(d)
                with contextlib.redirect_stdout(io.StringIO()):
                    ingest.run(p, ["docs"])
            finally:
                os.chdir(cwd)
            saved = project.load(p.root).ingest_sources
            self.assertEqual(saved, [os.path.join("..", "..", "docs")])
            self.assertTrue((p.root / saved[0]).resolve().samefile(Path(d) / "docs"))


class TestCompileScaffolding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.p = project.load(scaffold.init(d / "acme", "saas", activate=False))
        docs = write(d / "docs" / "exports.md", "# Exporting your data\n\nLinks expire after seven days.\n"
                                                "Call 772-555-0100.\n").parent
        with contextlib.redirect_stdout(io.StringIO()):
            ingest.run(self.p, [str(docs)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_drafts_candidates_and_stubs(self):
        drafts = comp.write_drafts(self.p)
        self.assertEqual([d.name for d in drafts], ["exporting-your-data.md"])
        self.assertEqual(comp.write_drafts(self.p), [])  # idempotent
        self.assertTrue(checks.run(self.p).ok)  # a draft doesn't turn check red...
        self.assertNotIn("exporting", str([pg.rel_path for pg in server.search.load_pages(self.p)]))  # ...or serve
        packet = comp.packet(self.p).read_text()
        self.assertIn("draft page waiting for you: `wiki/drafts/exporting-your-data.md`", packet)
        self.assertIn("(duration): Links expire after seven days.", packet)
        self.assertIn("(phone number): Call 772-555-0100.", packet)
        self.assertIn("must_cite_page: exporting-your-data", packet)
        self.assertIn(f"./gt -p {comp._where(self.p)} check --production", packet)

    def test_published_draft_needs_review(self):
        draft = comp.write_drafts(self.p)[0]
        draft.write_text(draft.read_text().replace("status: draft", "status: published"))
        self.assertTrue(any("not yet reviewed" in e for e in checks.run(self.p).errors.get("sources", [])))


class TestEvalBreakdown(unittest.TestCase):
    def test_free_tier_says_what_it_proved(self):
        p, tmp = copy_project()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(evals.run(p), 0)
            out = buf.getvalue()
            self.assertRegex(out, r"data\s+\d+/\d+")
            self.assertRegex(out, r"retrieval\s+\d+/\d+")
            self.assertIn("not tested", out)
        finally:
            facts.store(p).auto_reload = True
            tmp.cleanup()


class TestBundleGate(unittest.TestCase):
    def test_bundle_refuses_template_examples(self):
        from groundtruth import cli
        with tempfile.TemporaryDirectory() as d:
            root = scaffold.init(Path(d) / "acme", "saas", activate=False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["-p", str(root), "bundle"]), 1)
            self.assertIn("check --production", out.getvalue())
            self.assertFalse((root / "build" / "groundtruth-data.tar.gz").exists())


if __name__ == "__main__":
    unittest.main()
