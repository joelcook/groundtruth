"""Regression tests for the fourth independent review's findings."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from helpers import copy_project, write

from groundtruth import checks, compile as comp, facts, ingest, project, review, scaffold, server


def errors(p, production=False):
    return "\n".join(e for v in checks.run(p, production=production).errors.values() for e in v)


class TestChecks(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        facts.store(self.p).auto_reload = True
        self.tmp.cleanup()

    def test_missing_review_lock_fails_when_anything_cites_a_source(self):
        review.lock_path(self.p).unlink()
        self.assertIn("no sources.lock.yaml", errors(self.p))

    def test_money_is_reported_in_full(self):
        self.assertEqual(checks.MONEY_RE.search("It costs USD 39.").group(0), "USD 39")
        self.assertEqual(checks.MONEY_RE.search("It costs $1,250.50 a year.").group(0), "$1,250.50")

    def test_impossible_dates_fail(self):
        f = self.p.facts_dir / "events.yaml"
        text = f.read_text()
        first = next(line for line in text.splitlines() if "date:" in line and "2027-" in line)
        old = first.split("2027-")[1][:5]
        f.write_text(text.replace(f"2027-{old}", "2027-02-30", 1))
        self.assertIn("isn't a real date", errors(self.p))

    def test_missing_source_suggests_the_closest_file(self):
        f = self.p.facts_dir / "fees.yaml"
        f.write_text(f.read_text().replace("sources/pages/borrowing-policy.md", "sources/pages/borowing-policy.md"))
        self.assertIn("did you mean sources/pages/borrowing-policy.md", errors(self.p))

    def test_draft_problems_are_warnings(self):
        write(self.p.wiki_dir / "drafts" / "wip.md", "---\ntitle: WIP\npage_type: guide\nsources: []\n"
              "status: draft\n---\n\n# WIP\n\nStarter costs USD 49.\n\n## Sources & Lineage\n")
        report = checks.run(self.p)
        self.assertTrue(report.ok, report.errors)
        self.assertIn("USD 49", "\n".join(w for v in report.warnings.values() for w in v))


class TestCompile(unittest.TestCase):
    def test_colon_titles_and_long_sources(self):
        with tempfile.TemporaryDirectory() as d:
            p = project.load(scaffold.init(Path(d) / "acme", "blank", activate=False))
            body = "# Acme Forms: pricing and limits\n\n" + "".join(f"Plan {i} costs USD {i * 10}.\n" for i in range(1, 71))
            docs = write(Path(d) / "docs" / "plans.md", body).parent
            with contextlib.redirect_stdout(io.StringIO()):
                ingest.run(p, [str(docs)])
            draft = comp.write_drafts(p)[0]
            meta, _ = checks.split_frontmatter(draft.read_text())
            self.assertEqual(meta["title"], "Acme Forms: pricing and limits")
            self.assertIn(str((docs / "plans.md").resolve()), draft.read_text())  # local origin in lineage
            self.assertTrue(checks.run(p).ok)
            packet = comp.packet(p).read_text()
            self.assertIn("(70 line(s);", packet)
            self.assertIn("...and 10 more line(s)", packet)
            self.assertNotIn("Pages whose sources changed", packet)


class TestGateRecovery(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()
        self.gate = server.Gate(self.p, allow_unvalidated=True)
        self.gate.refresh(force=True)

    def tearDown(self):
        facts.store(self.p).auto_reload = True
        self.tmp.cleanup()

    def refresh(self):
        self.gate._checked = 0
        with contextlib.redirect_stderr(io.StringIO()):
            self.gate.refresh()

    def test_deleting_the_bad_file_clears_the_rejection(self):
        bad = write(self.p.wiki_dir / "guides" / "oops.md", "no frontmatter here\n")
        self.refresh()
        self.assertTrue(self.gate.status()["update_rejected"])
        bad.unlink()
        self.refresh()
        self.assertFalse(self.gate.status()["update_rejected"])

    def test_broken_config_is_rejected(self):
        cfg = self.p.root / "groundtruth.yaml"
        good = cfg.read_text()
        cfg.write_text(good + "\nbroken: [\n")
        self.refresh()
        status = self.gate.status()
        self.assertTrue(status["update_rejected"])
        self.assertIn("config", status["first"])
        cfg.write_text(good)
        self.refresh()
        self.assertFalse(self.gate.status()["update_rejected"])


if __name__ == "__main__":
    unittest.main()
