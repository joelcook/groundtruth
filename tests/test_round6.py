"""Regression tests for the sixth independent review's findings."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from helpers import copy_project, write

from groundtruth import checks, cli, compile as comp, evals, facts, ingest, progress, project, scaffold, server


def ingest_quietly(p, target):
    with contextlib.redirect_stdout(io.StringIO()):
        ingest.run(p, [str(target)])


class TestProposals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.p = project.load(scaffold.init(self.d / "acme", "blank", activate=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_statements_keep_their_value(self):
        line = ("The Enterprise plan is for larger organizations that need single sign-on, audit logs, a dedicated "
                "success manager, custom data retention, priority routing for every ticket, and it costs USD 199 per month.")
        self.assertGreater(len(line), 200)
        ingest_quietly(self.p, write(self.d / "docs" / "enterprise.md", f"# Enterprise\n\n{line}\n").parent)
        proposal = comp.write_fact_proposals(self.p)[0]
        self.assertIn("USD 199 per month.", proposal.read_text())
        comp.packet(self.p)
        self.assertIn("USD 199 per month.", (self.p.build_dir / "fact-candidates.md").read_text())

    def test_untouched_proposals_follow_their_source_and_edited_ones_are_flagged(self):
        doc = write(self.d / "docs" / "catalog.md", "# Catalog\n\nService 01 costs USD 101.\n")
        ingest_quietly(self.p, doc.parent)
        proposal = comp.write_fact_proposals(self.p)[0]
        write(doc, "# Catalog\n\nService 01 costs USD 201.\n")
        ingest_quietly(self.p, doc.parent)
        self.assertEqual(comp.write_fact_proposals(self.p), [proposal])  # regenerated: nobody had edited it
        self.assertIn("USD 201", proposal.read_text())
        self.assertEqual(comp.stale_proposals(self.p), [])
        proposal.write_text(proposal.read_text().replace("Exact statements", "Prices"))  # a human edit
        write(doc, "# Catalog\n\nService 01 costs USD 301.\n")
        ingest_quietly(self.p, doc.parent)
        self.assertEqual(comp.write_fact_proposals(self.p), [])  # never overwrite someone's edits...
        self.assertEqual(comp.stale_proposals(self.p), [proposal])  # ...flag them instead
        self.assertIn("source changed after it was proposed", str(checks.run(self.p).warnings))

    def test_status_points_at_proposals_when_no_facts_are_live(self):
        for f in self.p.facts_dir.glob("*.yaml"):
            f.unlink()
        ingest_quietly(self.p, write(self.d / "docs" / "pricing.md", "# Pricing\n\nTeam costs USD 39.\n").parent)
        comp.write_fact_proposals(self.p)
        self.assertIn("rename it to facts/pricing.yaml", progress.next_action(self.p, progress.steps(self.p)))
        self.assertIn("rename it to facts/pricing.yaml", str(checks.run(self.p).errors["facts"]))


class TestGoldenProblems(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_eval_and_status_report_instead_of_crashing(self):
        for text in ("- id: broken\n  question: [\n", "- How much does Starter cost?\n"):
            write(self.p.golden_path, text)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(evals.run(self.p), 1)
            self.assertIn("./gt check", err.getvalue())
            steps = progress.steps(self.p)
            self.assertIn("can't be read", next(s for s in steps if "eval" in s.title).detail)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["-p", str(self.p.root), "status"]), 0)
            self.assertIn("evals ? question(s)", out.getvalue())


class TestLiveSync(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()
        self.gate = server.Gate(self.p, allow_unvalidated=True)
        self.gate.refresh(force=True)
        self.server = server.build_server(self.p, self.gate)

    def tearDown(self):
        facts.store(self.p).auto_reload = True
        self.tmp.cleanup()

    def tools(self):
        return {t.name for t in self.server._tool_manager.list_tools()}

    def test_prompt_edits_and_new_tools_go_live(self):
        prompt = self.p.prompt_path
        prompt.write_text(prompt.read_text() + "\nAlways greet people by saying ahoy.\n")
        write(self.p.facts_dir / "parking.yaml", "description: Parking.\nkey: lot\n"
              "source: {path: sources/pages/locations-and-hours.md}\nrows:\n  - {lot: north, spaces: '40'}\n")
        golden = self.p.golden_path
        golden.write_text(golden.read_text() + "\n- id: parking\n  question: \"Where can I park?\"\n"
                          "  category: hard_fact\n  must_call_tool: get_parking\n  tool_args: {query: north}\n"
                          "  must_contain: [\"40\"]\n")
        self.gate.refresh(force=True)
        self.assertTrue(self.gate.validated and not self.gate.rejected, self.gate.rejected)
        self.assertIn("ahoy", self.server._mcp_server.instructions)
        self.assertIn("get_parking", self.tools())
        (self.p.facts_dir / "parking.yaml").unlink()
        golden.write_text(golden.read_text().split("\n- id: parking")[0] + "\n")
        self.gate.refresh(force=True)
        self.assertNotIn("get_parking", self.tools())


class TestNextKeepsProject(unittest.TestCase):
    def test_ingest_suggestion_keeps_dash_p(self):
        with tempfile.TemporaryDirectory() as d:
            root = scaffold.init(Path(d) / "acme", "blank", activate=False)
            docs = write(Path(d) / "docs" / "faq.md", "# FAQ\n\nAsk us anything.\n").parent
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main(["-p", str(root), "ingest", str(docs)])
            self.assertIn(f"next: ./gt -p {root.resolve()} compile", out.getvalue())


if __name__ == "__main__":
    unittest.main()
