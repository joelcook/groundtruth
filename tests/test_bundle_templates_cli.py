import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import TEMPLATES, copy_project

from groundtruth import bundle, checks, cli, evals, facts, server


class TestBundle(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_apply_and_reject(self):
        data = bundle.build(self.p).read_bytes()
        bundle._state["applied_sha"] = None
        self.assertTrue(bundle.apply_bytes(self.p, data).startswith("applied"))
        self.assertEqual(bundle.apply_bytes(self.p, data), "unchanged")
        res = server.call_tool(self.p, "get_hours", {"branch": "central", "day": "sunday"}, log=False)
        self.assertEqual(res["results"][0]["opens"], "13:00")
        bad = bytearray(data)
        bad[len(bad) // 2] ^= 0xFF
        with self.assertRaises(Exception):
            bundle.apply_bytes(self.p, bytes(bad))

    def test_refuses_a_bundle_that_loses_half_the_data(self):
        live = copy_project()
        small, small_tmp = live
        try:
            hours = small.facts_dir / "hours.yaml"
            lines = hours.read_text().splitlines()
            hours.write_text("\n".join(lines[: lines.index("rows:") + 3]) + "\n")
            data = bundle.build(small).read_bytes()
            facts.build(self.p)
            bundle._state["applied_sha"] = None
            with self.assertRaises(ValueError) as ctx:
                bundle.apply_bytes(self.p, data)
            self.assertIn("hours would drop", str(ctx.exception))
        finally:
            small_tmp.cleanup()


class TestTemplates(unittest.TestCase):
    def test_every_template_is_born_green(self):
        for tpl in sorted(p for p in TEMPLATES.iterdir() if (p / "groundtruth.yaml").exists()):
            with self.subTest(template=tpl.name):
                p, tmp = copy_project(tpl)
                try:
                    report = checks.run(p)
                    self.assertTrue(report.ok, report.errors)
                    self.assertEqual(evals.run(p, "tools", quiet=True), 0)
                finally:
                    tmp.cleanup()


class TestCli(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()
        self.pp = str(self.p.root)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["-p", self.pp, *args])
        return code, buf.getvalue()

    def test_check_eval_status(self):
        self.assertEqual(self.run_cli("check")[0], 0)
        self.assertEqual(self.run_cli("eval")[0], 0)
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("6 collection(s)", out)

    def test_tools_json(self):
        code, out = self.run_cli("tools", "--json")
        names = [t["name"] for t in json.loads(out)]
        self.assertIn("get_hours", names)
        self.assertIn("search_wiki", names)

    def test_ask_offline(self):
        code, out = self.run_cli("ask", "--offline", "is", "eastside", "open", "on", "sunday")
        self.assertIn("get_hours(branch='eastside', day='sunday')", out)

    def test_connect_snippets(self):
        code, out = self.run_cli("connect", "claude-code")
        self.assertIn("claude mcp add", out)
        self.assertIn(self.pp, out)

    def test_init_from_template(self):
        from groundtruth import project as project_mod
        before = project_mod.POINTER.read_text() if project_mod.POINTER.exists() else None
        with tempfile.TemporaryDirectory() as d:
            try:
                code, out = self.run_cli("init", str(Path(d) / "acme"), "--template", "saas", "--title", "Acme Help")
                self.assertEqual(code, 0, out)
                self.assertIn("check passes", out)
                self.assertIn("name: Acme Help", (Path(d) / "acme" / "groundtruth.yaml").read_text())
            finally:
                if before is None:
                    project_mod.POINTER.unlink(missing_ok=True)
                else:
                    project_mod.POINTER.write_text(before)


if __name__ == "__main__":
    unittest.main()
