"""Regression tests for the seventh independent review's findings."""
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path

from helpers import copy_project, write

from groundtruth import cli, compile as comp, facts, ingest, project, scaffold, server

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class TestFreshSessionsSeeTheLatest(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        facts.store(self.p).auto_reload = True
        self.tmp.cleanup()

    def test_initialize_reloads_first(self):
        from starlette.testclient import TestClient
        gate = server.Gate(self.p, production=True, allow_unvalidated=True)
        gate.refresh(force=True)
        with TestClient(server.http_app(self.p, gate), base_url="http://127.0.0.1:8765") as client:
            before = client.post("/mcp", json=INIT, headers=HEADERS).json()["result"]["instructions"]
            self.assertNotIn("ahoy", before)
            self.p.prompt_path.write_text(self.p.prompt_path.read_text() + "\nSay ahoy.\n")
            gate._checked = 0  # skip the once-a-second throttle instead of sleeping
            after = client.post("/mcp", json=INIT, headers=HEADERS).json()["result"]["instructions"]
            self.assertIn("ahoy", after)  # no tool call or health check needed first

    def test_background_watch_applies_updates(self):
        gate = server.Gate(self.p, allow_unvalidated=True)
        gate.refresh(force=True)
        srv = server.build_server(self.p, gate)
        gate.watch(every=0.05)
        try:
            self.p.prompt_path.write_text(self.p.prompt_path.read_text() + "\nSay ahoy.\n")
            deadline = time.time() + 5
            while "ahoy" not in srv._mcp_server.instructions and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn("ahoy", srv._mcp_server.instructions)
        finally:
            gate.stop()


class TestTables(unittest.TestCase):
    def test_table_rows_become_candidates_with_their_headers(self):
        with tempfile.TemporaryDirectory() as d:
            p = project.load(scaffold.init(Path(d) / "acme", "blank", activate=False))
            docs = write(Path(d) / "docs" / "limits.md", "# Plans\n\n| Plan | Monthly price (USD) | Project limit |\n"
                                                         "|---|---:|---:|\n| Starter | 19 | 5 |\n| Team | 49 | 25 |\n").parent
            with contextlib.redirect_stdout(io.StringIO()):
                ingest.run(p, [str(docs)])
            found = [text for _, kind, text in comp.candidates(p.pages_dir / "limits.md") if kind == "table row"]
            self.assertEqual(found, ["Plan: Starter; Monthly price (USD): 19; Project limit: 5",
                                     "Plan: Team; Monthly price (USD): 49; Project limit: 25"])
            comp.write_fact_proposals(p)
            self.assertIn('must_contain: ["19"]', comp.packet(p).read_text())


class TestSuggestionsKeepTheProject(unittest.TestCase):
    def test_every_suggestion_keeps_a_quoted_dash_p(self):
        with tempfile.TemporaryDirectory() as d:
            root = scaffold.init(Path(d) / "new assistant", "saas", activate=False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main(["-p", str(root), "check"])
            text = out.getvalue()
            self.assertIn(f"./gt -p '{root.resolve()}' examples --remove", text)
            self.assertNotRegex(text, r"\./gt (?!-p )")


if __name__ == "__main__":
    unittest.main()
