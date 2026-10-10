"""Regression tests for the issues an independent review (Codex) found."""
import asyncio
import json
import unittest
from unittest import mock

from helpers import TEMPLATES, copy_project, write

from groundtruth import checks, connect, evals, examples, observability, review, server


class TestSourceReview(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_changed_source_fails_check_until_reviewed(self):
        self.assertEqual(review.changes(self.p), [])
        src = self.p.pages_dir / "borrowing-policy.md"
        src.write_text(src.read_text() + "\nPrinting now costs more.\n")  # same day, new content
        pending = review.changes(self.p)
        self.assertEqual([c.source for c in pending], ["sources/pages/borrowing-policy.md"])
        self.assertIn("facts/fees.yaml", pending[0].facts)
        self.assertIn("wiki/policies/fines-and-fees.md", pending[0].wiki)
        report = checks.run(self.p)
        self.assertIn("sources", report.errors)
        review.mark_reviewed(self.p)
        self.assertTrue(checks.run(self.p).ok)

    def test_changed_source_marks_pages_stale(self):
        from groundtruth import compile as comp
        src = self.p.pages_dir / "borrowing-policy.md"
        src.write_text(src.read_text() + "\nchanged\n")
        stale = [self.p.rel(page) for page, _ in comp.stale_pages(self.p)]
        self.assertIn("wiki/policies/fines-and-fees.md", stale)
        packet = comp.packet(self.p).read_text()
        self.assertIn("Sources that changed since the last review", packet)
        self.assertIn("facts/fees.yaml", packet)

    def test_new_cited_source_needs_review(self):
        write(self.p.pages_dir / "new.md", "---\ntitle: New\n---\n\nnew\n")
        fees = self.p.facts_dir / "fees.yaml"
        fees.write_text(fees.read_text().replace("    amount_usd: 25.00", "    amount_usd: 25.00\n"
                                                 "    source_path: sources/pages/new.md"))
        self.assertEqual([(c.source, c.reason) for c in review.changes(self.p)],
                         [("sources/pages/new.md", "not yet reviewed")])

    def test_no_lock_is_an_error_once_sources_are_cited(self):
        review.lock_path(self.p).unlink()
        report = checks.run(self.p)
        self.assertFalse(report.ok)
        self.assertIn("sources.lock.yaml", report.errors["sources"][0])
        review.mark_reviewed(self.p)
        self.assertTrue(checks.run(self.p).ok)


class TestExamples(unittest.TestCase):
    def test_find_and_remove(self):
        p, tmp = copy_project(TEMPLATES / "saas")
        try:
            found = examples.find(p)
            self.assertEqual((len(found.facts), len(found.wiki), len(found.sources)), (3, 2, 4))
            self.assertIn("examples", checks.run(p).warnings)
            g = p.golden_path
            g.write_text(g.read_text() + "\n- id: mine\n  question: q\n  category: out_of_scope\n"
                         "  must_not_call_tool: any\n")
            examples.remove(p)
            self.assertEqual(examples.find(p).total, 0)
            self.assertEqual([i["id"] for i in checks.load_golden(p)], ["mine"])
            self.assertNotIn("[[reset-your-password]]", (p.wiki_dir / "index.md").read_text())
            self.assertFalse(any(p.facts_dir.glob("*.yaml")))
        finally:
            tmp.cleanup()

    def test_library_demo_has_no_examples(self):
        p, tmp = copy_project()
        try:
            self.assertEqual(examples.find(p).total, 0)
        finally:
            tmp.cleanup()


class TestPrompt(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_mcp_clients_get_the_project_prompt(self):
        prompt = self.p.prompt_path
        prompt.write_text(prompt.read_text() + "\nZEBRA-RULE: always say hello.\n")
        text = server.instructions(self.p)
        self.assertIn("ZEBRA-RULE", text)
        self.assertIn("| Fees and fines in US dollars. | `get_fee` |", text)
        self.assertNotIn("{{FACT_TOOLS}}", text)
        srv = server.build_server(self.p)
        self.assertEqual(srv._mcp_server.create_initialization_options().instructions, text)
        self.assertEqual(evals.system_prompt(self.p), text)

    def test_check_flags_unknown_tools_in_prompt(self):
        prompt = self.p.prompt_path
        prompt.write_text(prompt.read_text() + "\nFor parking, call `get_parking`.\n")
        errs = "\n".join(checks.run(self.p).errors.get("prompt", []))
        self.assertIn("get_parking", errs)


class TestStricterEvals(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_numbers_compare_by_value(self):
        self.assertFalse(evals.contains('{"price_usd": 49}', "9"))
        self.assertTrue(evals.contains('{"price_usd": 49}', "49"))
        self.assertTrue(evals.contains('{"amount_usd": 0.1}', "0.10"))

    def test_must_match(self):
        g = self.p.golden_path
        g.write_text(g.read_text() + "\n- id: color-typed\n  question: q\n  category: hard_fact\n"
                     "  must_call_tool: get_fee\n  tool_args: {fee_type: color printing}\n"
                     "  must_match: {fee_type: printing-color, amount_usd: 0.75}\n")
        self.assertEqual(evals.run(self.p, "tools", quiet=True), 1)
        data = json.loads(sorted(self.p.runs_dir.glob("*-tools.json"))[-1].read_text())
        failed = {r["id"]: r["reasons"] for r in data["results"] if not r["passed"]}
        self.assertIn("color-typed", failed)

    def test_golden_must_be_a_list(self):
        self.p.golden_path.write_text("id: x\nquestion: q\ncategory: hard_fact\n")
        errs = "\n".join(checks.run(self.p).errors.get("evals", []))
        self.assertIn("must be a list", errs)


class TestLint(unittest.TestCase):
    def test_new_fact_kinds(self):
        for text, kind in [("within 14 days", "duration"), ("a 14-day trial", "duration"), ("saves 20%", "percentage"),
                           ("up to 2 GB", "size limit"), ("call 555-0100", "phone number"),
                           ("email help@example.com", "email")]:
            self.assertIn(kind, [k for k, rx in checks.PATTERNS if rx.search(text)], text)
        for text in ("open 24/7", "step 2 of 3", "about two weeks"):
            self.assertEqual([k for k, rx in checks.PATTERNS if rx.search(text)], [], text)


class TestWatchdogAndProbes(unittest.TestCase):
    def test_empty_wiki_is_unhealthy(self):
        import shutil
        from groundtruth import watchdog
        p, tmp = copy_project()
        try:
            shutil.rmtree(p.wiki_dir)
            self.assertEqual(watchdog.run(p), 1)
        finally:
            tmp.cleanup()

    def test_probe_header_marks_requests(self):
        p, tmp = copy_project()
        seen = []

        async def app(scope, receive, send):
            seen.append(observability.PROBE.get())

        try:
            mw = server.GatewayMiddleware(app, p, "")
            for headers in ([(b"x-groundtruth-probe", b"1")], []):
                asyncio.run(mw({"type": "http", "path": "/mcp", "headers": headers}, None, None))
            self.assertEqual(seen, [True, False])
        finally:
            tmp.cleanup()


class TestConnectInstall(unittest.TestCase):
    def test_runs_the_client_cli(self):
        p, tmp = copy_project()
        try:
            with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
                    mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
                msg = connect.install("claude-code", p)
            cmd = run.call_args[0][0]
            self.assertEqual(cmd[:3], ["/usr/bin/claude", "mcp", "add"])
            self.assertIn("--stdio", cmd)
            self.assertIn("claude-code", msg)
            with self.assertRaises(ValueError) as ctx:
                connect.install("vscode", p)
            self.assertIn(".vscode/mcp.json", str(ctx.exception))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
