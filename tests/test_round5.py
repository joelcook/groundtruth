"""Regression tests for the fifth independent review's findings."""
import contextlib
import io
import json
import unittest

from helpers import copy_project, write

from groundtruth import checks, facts, progress, server, watchdog


class TestRound5(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        facts.store(self.p).auto_reload = True
        self.tmp.cleanup()

    def break_prose(self):
        page = self.p.wiki_dir / "journeys" / "getting-a-library-card.md"
        page.write_text(page.read_text().replace("## Sources & Lineage", "A card costs USD 39.\n\n## Sources & Lineage", 1))

    def test_stdio_refuses_unchecked_content(self):
        self.break_prose()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(server.run(self.p, stdio=True), 1)
        self.assertIn("Refusing", err.getvalue())

    def test_common_count_limits_are_flagged(self):
        kinds = {k for k, rx in checks.PATTERNS if rx.search("Starter includes 3 users and 5 active projects.")}
        self.assertIn("limit", kinds)

    def test_status_sends_pending_review_to_review(self):
        src = self.p.pages_dir / "borrowing-policy.md"
        src.write_text(src.read_text() + "\nA new paragraph.\n")
        action = progress.next_action(self.p, progress.steps(self.p))
        self.assertIn("review --diff", action)

    def test_status_survives_broken_golden_yaml(self):
        write(self.p.golden_path, "- id: [\n")
        steps = progress.steps(self.p)
        self.assertIn("check", progress.next_action(self.p, steps))

    def test_watchdog_probes_are_not_real_use(self):
        self.p.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.p.logs_dir / "tool_calls.jsonl").write_text(json.dumps(
            {"ts": "2026-10-10T00:00:00Z", "tool": "search_wiki", "ok": True, "probe": True}) + "\n")
        client = next(s for s in progress.steps(self.p) if "client" in s.title)
        self.assertFalse(client.done)

    def test_remote_watchdog_needs_no_project(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(watchdog.run(None, url="http://127.0.0.1:9/mcp"), 1)
        self.assertIn("watchdog: http://127.0.0.1:9/mcp", out.getvalue())


class TestFactProposals(unittest.TestCase):
    def test_compile_proposes_a_facts_file_you_can_rename_live(self):
        import tempfile
        from pathlib import Path
        from groundtruth import compile as comp, ingest, project, scaffold
        with tempfile.TemporaryDirectory() as d:
            p = project.load(scaffold.init(Path(d) / "acme", "saas", activate=False))
            docs = write(Path(d) / "docs" / "pricing.md", "# Pricing\n\nThe Team plan costs USD 39 per month.\n"
                                                           "Refunds close on November 1.\n").parent
            with contextlib.redirect_stdout(io.StringIO()):
                ingest.run(p, [str(docs)])
            proposed = comp.write_fact_proposals(p)
            self.assertEqual([x.name for x in proposed], ["_proposed-pricing.yaml"])  # no collision hash
            self.assertEqual(comp.write_fact_proposals(p), [])  # idempotent
            self.assertNotIn("get_pricing_fact", {t.name for c in facts.load(p) for t in c.tools})  # not live
            self.assertIn("proposed facts", "\n".join(checks.run(p).warnings.get("facts", [])))
            self.assertIn("tool_args: {topic: team plan costs}", comp.packet(p).read_text())
            proposed[0].rename(p.facts_dir / "pricing.yaml")
            facts.build(p)
            facts.store(p).refresh(force=True)
            hit = facts.store(p).call("get_pricing_fact", {"topic": "team plan"})
            self.assertIn("USD 39", hit["results"][0]["statement"])
            self.assertTrue(hit["results"][0]["source"].startswith("sources/pages/pricing"))
            self.assertFalse([s for s in comp.uncovered(p) if s.rel.startswith("sources/pages/pricing-")])  # covered


if __name__ == "__main__":
    unittest.main()
