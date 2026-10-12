"""Regression tests for the eighth review's findings (Codex and Grok): run unattended, ship nothing wrong."""
import io
import json
import os
import tarfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import copy_project, write

from groundtruth import (bundle, checks, compile as comp, evals, facts, grounding, ingest, observability,
                         server)


class TestGrounding(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_demo_is_grounded(self):
        self.assertEqual(grounding.problems(self.p), [])

    def test_a_value_the_source_doesnt_state_fails_production(self):
        fees = self.p.facts_dir / "fees.yaml"
        fees.write_text(fees.read_text().replace("amount_usd: 25.00", "amount_usd: 35.00"))
        problems = grounding.problems(self.p)
        self.assertEqual(len(problems), 1)
        self.assertIn("meeting-room", problems[0])
        self.assertTrue(checks.run(self.p).ok)  # a warning while you build...
        self.assertFalse(checks.run(self.p, production=True).ok)  # ...and a failure where it ships

    def test_values_match_by_meaning_not_spelling(self):
        idx = grounding.SourceIndex.of("Plans: USD 39.00 a month, 1,200 seats, $0.10 a page or 10 cents. "
                                       "Opens September 1, 2026 at 1 p.m.; noon on Fridays. Call (555) 010-0123.")
        for value in [39, 1200, 0.1, "2026-09-01", "13:00", "12:00", "555-010-0123"]:
            for kind, norm, raw in grounding.exact_values(value):
                self.assertTrue(idx.has(kind, norm), f"{value!r} ({kind})")
        for value in [40, "2026-09-02", "14:00", "555-010-9999"]:
            self.assertFalse(all(idx.has(k, n) for k, n, _ in grounding.exact_values(value)), value)

    def test_a_row_with_only_a_url_has_nothing_to_check_against(self):
        write(self.p.facts_dir / "links.yaml", "key: id\nsource: {url: https://example.org/x}\n"
              "tools: [{name: get_link, kind: lookup, description: Links.}]\nrows:\n  - {id: a, price_usd: 5}\n")
        self.assertTrue(any("cites no captured source" in p for p in grounding.problems(self.p)))


class FakeResponse(SimpleNamespace):
    pass


class FakeFetcher:
    pages: dict = {}

    def __init__(self, delay=0.25):
        pass

    def allowed(self, url):
        return True

    def get(self, url):
        status, final, html = self.pages.get(url, (404, url, ""))
        return FakeResponse(status_code=status, url=final, text=html, content=html.encode(),
                            headers={"content-type": "text/html"})


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_page_that_404s_during_a_crawl_is_flagged_removed(self):
        write(self.p.pages_dir / "gone.md", "---\nsource_url: https://ex.org/gone\ncaptured: 2026-01-01\n---\n\nOld.\n")
        FakeFetcher.pages = {}
        with mock.patch.object(ingest, "Fetcher", FakeFetcher):
            result = ingest.run(self.p, ["https://ex.org/gone"], save=False)
        self.assertEqual(result.removed, ["sources/pages/gone.md"])

    def test_a_redirect_off_the_site_is_never_captured(self):
        body = "<html><head><title>X</title></head><body><main>" + "Somewhere else entirely. " * 10 + "</main></body></html>"
        FakeFetcher.pages = {"https://ex.org/b": (200, "https://elsewhere.example/b", body)}
        with mock.patch.object(ingest, "Fetcher", FakeFetcher):
            result = ingest.run(self.p, ["https://ex.org/b"], save=False)
        self.assertEqual(result.new, [])
        self.assertTrue(any("off the site" in s for s in result.skipped))

    def test_local_documents_are_stored_relative_to_the_project(self):
        doc = write(Path(self.tmp.name) / "docs" / "hours.md", "# Hours\n\nOpen every day.\n")
        ingest.run(self.p, [str(doc.parent)], save=False)
        page = next(self.p.pages_dir.glob("hours*.md")).read_text()
        self.assertIn("source_path: ../docs/hours.md", page)
        self.assertNotIn(str(Path(self.tmp.name).resolve()), page)
        ingest.run(self.p, [str(doc.parent)], save=False)  # re-ingest matches it: no duplicate
        self.assertEqual(len(list(self.p.pages_dir.glob("hours*.md"))), 1)


class TestEvals(unittest.TestCase):
    def test_stale_values_compare_by_number(self):
        item = {"category": "contradiction", "must_not_contain": ["9"]}
        self.assertEqual(evals._grade_text(item, "The Team plan is $49."), [])  # 9 is not 49
        self.assertTrue(evals._grade_text(item, "It's $9 now."))

    def test_live_grading_checks_what_the_tool_returned_and_what_was_said(self):
        item = {"category": "hard_fact", "must_call_tool": "get_fee", "must_match": {"amount_usd": 0.5}}
        empty = [{"name": "get_fee", "input": {"fee_type": "nonsense"}, "result": {"count": 0, "results": []}}]
        ok, reasons = evals.grade(item, "It's $999.", empty, use_judge=False)
        self.assertFalse(ok)
        self.assertTrue(any("found nothing" in r for r in reasons))
        good = [{"name": "get_fee", "input": {"fee_type": "color"},
                 "result": {"count": 1, "results": [{"fee_type": "printing-color", "amount_usd": 0.5}]}}]
        self.assertFalse(evals.grade(item, "It's $999.", good, use_judge=False)[0])  # the answer must say it
        self.assertTrue(evals.grade(item, "Color printing is $0.50 a page.", good, use_judge=False)[0])


class TestFacts(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()

    def tearDown(self):
        self.tmp.cleanup()

    def bad_tool(self, filters: str) -> list[str]:
        write(self.p.facts_dir / "x.yaml", "key: id\nsource: {path: sources/pages/programs.md}\n"
              f"tools: [{{name: list_x, kind: list, filters: {filters}, description: X.}}]\n"
              "rows:\n  - {id: a, class: b, kind: c}\n")
        with self.assertRaises(facts.FactsError) as ctx:
            facts.load(self.p)
        return ctx.exception.problems

    def test_parameters_that_would_crash_the_server_are_rejected(self):
        self.assertTrue(any("reserved word" in p for p in self.bad_tool("[class]")))
        self.assertTrue(any("two parameters" in p for p in self.bad_tool("[{field: class, param: k}, {field: kind, param: k}]")))

    def test_an_exact_value_beats_a_prefix_and_ambiguity_is_flagged(self):
        write(self.p.facts_dir / "spots.yaml", "key: id\nsource: {path: sources/pages/programs.md}\n"
              "tools: [{name: list_spots, kind: list, filters: [area], description: Spots.}]\n"
              "rows:\n  - {id: a, area: central}\n  - {id: b, area: centerville}\n")
        exact = server.call_tool(self.p, "list_spots", {"area": "central"}, log=False)
        self.assertEqual([r["id"] for r in exact["results"]], ["a"])
        vague = server.call_tool(self.p, "list_spots", {"area": "cent"}, log=False)
        self.assertEqual(vague["ambiguous"], {"area": ["centerville", "central"]})

    def test_upcoming_puts_dated_rows_first(self):
        res = server.call_tool(self.p, "list_events", {"upcoming_only": False, "limit": 50}, log=False)
        dated = [bool(r.get("date")) for r in res["results"]]
        self.assertEqual(dated, sorted(dated, reverse=True))


class TestCompileNames(unittest.TestCase):
    def test_a_hyphenated_file_is_a_valid_collection(self):
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        write(p.facts_dir / "self-pay-prices.yaml", "key: id\nsource: {path: sources/pages/programs.md}\n"
              "tools: [{name: get_self_pay, kind: lookup, description: Prices.}]\nrows:\n  - {id: a, note: x}\n")
        self.assertIn("self_pay_prices", [c.name for c in facts.load(p)])

    def test_long_names_dont_loop_forever(self):
        p, tmp = copy_project()
        self.addCleanup(tmp.cleanup)
        name = "a-very-long-source-name-that-goes-on-and-on-and-on-and-on-for-ages-and-more"
        for i in range(3):
            write(p.pages_dir / f"{name}-{i}.md", f"---\ncaptured: 2026-10-01\n---\n\nIt costs $1{i} a month.\n")
        made = comp.write_fact_proposals(p)  # used to spin forever once truncation ate the suffix
        self.assertEqual(len(set(made)), 3)  # and never overwrite each other's proposal
        tools = [yaml_tool(m) for m in made]
        self.assertEqual(len(set(tools)), len(tools))
        self.assertTrue(all(len(t) <= 64 for t in tools))


def yaml_tool(path: Path) -> str:
    import yaml
    return yaml.safe_load(path.read_text())["tools"][0]["name"]


class TestBundles(unittest.TestCase):
    def setUp(self):
        self.p, self.tmp = copy_project()
        bundle._state.update(applied_sha=None, applied_created=None, last_error=None)

    def tearDown(self):
        self.tmp.cleanup()

    def members(self, data: bytes) -> dict:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            return {n: tar.extractfile(n).read() for n in tar.getnames()}

    def repack(self, files: dict) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, blob in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
        return buf.getvalue()

    def test_it_carries_the_prompt_but_not_drafts_or_staff_pages(self):
        write(self.p.wiki_dir / "internal" / "staff.md", "---\ntitle: Staff\npage_type: internal\nsources: []\n---\nx\n")
        write(self.p.wiki_dir / "drafts" / "wip.md", "---\ntitle: WIP\npage_type: guide\nsources: []\nstatus: draft\n---\nx\n")
        names = set(self.members(bundle.build(self.p).read_bytes()))
        self.assertIn("prompts/system_prompt.md", names)
        self.assertIn("config.json", names)
        self.assertFalse([n for n in names if "internal" in n or "drafts" in n])

    def test_it_rejects_what_it_shouldnt_trust(self):
        data = bundle.build(self.p).read_bytes()
        files = self.members(data)
        with self.assertRaisesRegex(ValueError, "doesn't list"):
            bundle.apply_bytes(self.p, self.repack({**files, "wiki/extra.md": b"x"}))
        manifest = json.loads(files["manifest.json"])
        manifest["counts"]["hours"] = 9999
        with self.assertRaisesRegex(ValueError, "don't match"):
            bundle.apply_bytes(self.p, self.repack({**files, "manifest.json": json.dumps(manifest).encode()}))
        self.assertTrue(bundle.apply_bytes(self.p, data).startswith("applied"))
        older = json.loads(files["manifest.json"])
        older["created"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "older"):
            bundle.apply_bytes(self.p, self.repack({**files, "manifest.json": json.dumps(older).encode()}))


class TestPrivacy(unittest.TestCase):
    def test_logs_scrub_secrets_at_any_depth(self):
        out = observability._redact({"query": "reach me at jo@corp.example, token=abc123xyz",
                                     "nested": {"password": "x", "list": ["sk-ant-api03-AAAABBBBCCCCDDDD"]}})
        flat = json.dumps(out)
        for secret in ("jo@corp.example", "abc123xyz", "sk-ant-api03"):
            self.assertNotIn(secret, flat)
        self.assertEqual(out["nested"]["password"], "***")

    def test_the_build_commit_comes_from_the_platform_too(self):
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "abc1234"}):
            os.environ.pop("GT_BUILD_COMMIT", None)
            self.assertEqual(server.build_commit(), "abc1234")


if __name__ == "__main__":
    unittest.main()
